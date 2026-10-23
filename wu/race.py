from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import HEURISTIC_MODEL, RESULTS_DIR
from .game import Rules, solve
from .models import RaceConfig, new_lane, now_iso
from .runners import HeuristicPolicy, policy_for, run_decisions_lane, run_text_lane


RACES_DIR = RESULTS_DIR / "races"


def analyze_hand(
    hand: list[int],
    target: int,
    rules: Rules,
    stats: dict | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    solution = solve(hand, target, rules, stats=stats)
    return {
        "hand": hand,
        "target": target,
        "rules": rules.to_dict(),
        "solvable": solution is not None,
        "solution": [step.to_dict() for step in solution] if solution else None,
        "steps": len(solution) if solution else None,
        "nodes": stats.get("nodes"),
    }


def _public_lane(lane: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy(lane)
    public.pop("raw", None)
    public["trace"] = public.get("trace", [])[-80:]
    for attempt in public.get("attempts", []):
        attempt["content"] = (attempt.get("content") or "")[:2500]
        attempt["reasoning"] = (attempt.get("reasoning") or "")[:1200]
    return public


def _public_race(race: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy({key: value for key, value in race.items() if key != "lanes"})
    public["lanes"] = {key: _public_lane(lane) for key, lane in race["lanes"].items()}
    return public


def _summary(race: dict[str, Any]) -> dict[str, Any]:
    lanes = race["lanes"]
    result: dict[str, Any] = {
        "winner": race.get("winner"),
        "text_ms": lanes["text"].get("elapsed_ms"),
        "decisions_ms": lanes["decisions"].get("elapsed_ms"),
        "baseline_ms": lanes.get("baseline", {}).get("elapsed_ms"),
        "text_status": lanes["text"].get("status"),
        "decisions_status": lanes["decisions"].get("status"),
        "baseline_status": lanes.get("baseline", {}).get("status"),
        "delta_ms": None,
        "speedup": None,
    }
    text_ms = lanes["text"].get("elapsed_ms")
    decisions_ms = lanes["decisions"].get("elapsed_ms")
    if (
        text_ms
        and decisions_ms
        and lanes["text"].get("status") == "solved"
        and lanes["decisions"].get("status") == "solved"
    ):
        result["delta_ms"] = round(decisions_ms - text_ms, 1)
        if decisions_ms > 0:
            result["speedup"] = round(text_ms / decisions_ms, 3)
    return result


class RaceManager:
    def __init__(self) -> None:
        self.races: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        RACES_DIR.mkdir(parents=True, exist_ok=True)

    def create(self, raw_config: dict[str, Any]) -> dict[str, Any]:
        config = RaceConfig.from_dict(raw_config)
        race_id = uuid.uuid4().hex[:12]
        ground_truth = analyze_hand(config.hand, config.target, config.rules)
        decisions_provider = "Harness" if config.decisions_model == HEURISTIC_MODEL else "OpenRouter"
        race: dict[str, Any] = {
            "id": race_id,
            "status": "created",
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "config": config.to_dict(),
            "ground_truth": ground_truth,
            "lanes": {
                "text": new_lane("text", config.text_model, "Nam"),
                "decisions": new_lane("decisions", config.decisions_model, decisions_provider),
            },
            "winner": None,
            "events": [],
            "summary": None,
        }
        if config.include_baseline and config.decisions_model != HEURISTIC_MODEL:
            race["lanes"]["baseline"] = new_lane("baseline", HEURISTIC_MODEL, "Harness")
        self.races[race_id] = race
        return _public_race(race)

    def start(self, race_id: str) -> None:
        race = self.races[race_id]
        if race["status"] != "created":
            return
        task = asyncio.create_task(self._run(race_id))
        self.tasks[race_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(race_id, None))

    def cancel(self, race_id: str) -> bool:
        task = self.tasks.get(race_id)
        if task is None:
            return False
        task.cancel()
        race = self.races.get(race_id)
        if race:
            race["status"] = "cancelled"
            race["finished_at"] = now_iso()
            for lane in race["lanes"].values():
                if lane["status"] in ("waiting", "running"):
                    lane["status"] = "cancelled"
        return True

    async def _run(self, race_id: str) -> None:
        race = self.races[race_id]
        race["status"] = "running"
        race["started_at"] = now_iso()
        race["events"].append({"at": race["started_at"], "message": "race started"})
        config = RaceConfig.from_dict(race["config"])
        lanes = race["lanes"]
        tasks = [
            run_text_lane(config, lanes["text"]),
            run_decisions_lane(config, lanes["decisions"], policy_for(config), source="decisions"),
        ]
        if "baseline" in lanes:
            tasks.append(
                run_decisions_lane(config, lanes["baseline"], HeuristicPolicy(), source="baseline")
            )
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            race["status"] = "cancelled"
            race["finished_at"] = now_iso()
            self._save(race)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            race["status"] = "error"
            race["error"] = str(exc)
            race["finished_at"] = now_iso()
            self._save(race)
            return

        eligible = [
            lane
            for key, lane in lanes.items()
            if key in ("text", "decisions") and lane["status"] == "solved" and lane["elapsed_ms"]
        ]
        if eligible:
            fastest = min(eligible, key=lambda lane: lane["elapsed_ms"])
            race["winner"] = fastest["kind"]
            if len(eligible) > 1:
                first, second = sorted(eligible, key=lambda lane: lane["elapsed_ms"])[:2]
                if abs(first["elapsed_ms"] - second["elapsed_ms"]) < 1:
                    race["winner"] = "tie"
        race["status"] = "done"
        race["finished_at"] = now_iso()
        race["summary"] = _summary(race)
        race["events"].append(
            {
                "at": race["finished_at"],
                "message": f"race finished: {race['winner'] or 'no winner'}",
            }
        )
        self._save(race)

    def _save(self, race: dict[str, Any]) -> None:
        path = RACES_DIR / f"{race['id']}.json"
        path.write_text(json.dumps(race, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, race_id: str) -> dict[str, Any] | None:
        race = self.races.get(race_id)
        if race is not None:
            return _public_race(race)
        path = RACES_DIR / f"{race_id}.json"
        if path.exists():
            return _public_race(json.loads(path.read_text(encoding="utf-8")))
        return None

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for race in self.races.values():
            summaries[race["id"]] = {
                "id": race["id"],
                "status": race["status"],
                "created_at": race["created_at"],
                "started_at": race["started_at"],
                "finished_at": race["finished_at"],
                "winner": race.get("winner"),
                "config": race["config"],
                "summary": race.get("summary"),
            }
        if RACES_DIR.exists():
            for path in RACES_DIR.glob("*.json"):
                if path.stem in summaries:
                    continue
                try:
                    race = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                summaries[race["id"]] = {
                    "id": race["id"],
                    "status": race.get("status"),
                    "created_at": race.get("created_at"),
                    "started_at": race.get("started_at"),
                    "finished_at": race.get("finished_at"),
                    "winner": race.get("winner"),
                    "config": race.get("config"),
                    "summary": race.get("summary"),
                }
        ordered = sorted(
            summaries.values(),
            key=lambda item: item.get("started_at") or item.get("created_at") or "",
            reverse=True,
        )
        return ordered[:limit]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


manager = RaceManager()
