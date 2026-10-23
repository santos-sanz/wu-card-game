from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from .clients import ProviderError
from .config import RESULTS_DIR
from .game import Rules
from .models import RaceConfig, new_lane
from .runners import HeuristicPolicy, policy_for, run_decisions_lane, run_text_lane


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 1)


def _lane_metrics(lanes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for kind, lane in lanes.items():
        solved_times = [lane["elapsed_ms"]] if lane["status"] == "solved" and lane["elapsed_ms"] else []
        metrics[kind] = {
            "model": lane["model"],
            "provider": lane["provider"],
            "status": lane["status"],
            "calls": lane["calls"],
            "tokens": lane["tokens"],
            "cost": lane["cost"],
            "elapsed_ms": lane["elapsed_ms"],
            "attempts": len(lane.get("attempts", [])),
            "invalid_attempts": sum(1 for attempt in lane.get("attempts", []) if not attempt.get("ok")),
            "solution": lane.get("solution"),
            "error": lane.get("error"),
            "solved_times": solved_times,
        }
    return metrics


def _aggregate(hand_results: list[dict[str, Any]], kinds: list[str]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    for kind in kinds:
        lanes = [result["lanes"][kind] for result in hand_results if kind in result["lanes"]]
        if not lanes:
            continue
        solved = [lane for lane in lanes if lane["status"] == "solved"]
        times = [lane["elapsed_ms"] for lane in solved if lane["elapsed_ms"] is not None]
        all_times = [lane["elapsed_ms"] for lane in lanes if lane["elapsed_ms"] is not None]
        calls = [lane["calls"] for lane in lanes]
        prompt_tokens = [lane["tokens"]["prompt"] for lane in lanes]
        completion_tokens = [lane["tokens"]["completion"] for lane in lanes]
        aggregate[kind] = {
            "runs": len(lanes),
            "solved": len(solved),
            "solved_rate": round(len(solved) / len(lanes), 3) if lanes else 0,
            "errors": sum(1 for lane in lanes if lane["status"] == "error"),
            "unsolved": sum(1 for lane in lanes if lane["status"] == "unsolved"),
            "median_solved_ms": round(statistics.median(times), 1) if times else None,
            "mean_solved_ms": round(statistics.mean(times), 1) if times else None,
            "p95_solved_ms": _percentile(times, 0.95),
            "mean_elapsed_ms": round(statistics.mean(all_times), 1) if all_times else None,
            "mean_calls": round(statistics.mean(calls), 2) if calls else 0,
            "mean_prompt_tokens": round(statistics.mean(prompt_tokens), 1) if prompt_tokens else 0,
            "mean_completion_tokens": round(statistics.mean(completion_tokens), 1) if completion_tokens else 0,
            "total_cost": round(sum(lane["cost"] for lane in lanes), 8),
        }
    return aggregate


async def run_benchmark(
    records: list[dict[str, Any]],
    text_model: str,
    decisions_model: str,
    text_max_attempts: int = 2,
    max_expansions: int = 120,
    include_baseline: bool = False,
    seed: int = 7,
    output_dir: Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    output_dir = output_dir or RESULTS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y%m%d-%H%M%S")
    hand_results: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        config = RaceConfig.from_dict(
            {
                "hand": record["hand"],
                "target": record["target"],
                "rules": record.get("rules"),
                "text_model": text_model,
                "decisions_model": decisions_model,
                "text_max_attempts": text_max_attempts,
                "max_expansions": max_expansions,
                "include_baseline": include_baseline,
                "seed": seed + index,
            }
        )
        text_lane = new_lane("text", config.text_model, "Nam")
        decisions_lane = new_lane(
            "decisions",
            config.decisions_model,
            "Harness" if config.decisions_model == "harness/heuristic" else "OpenRouter",
        )
        lanes = {"text": text_lane, "decisions": decisions_lane}
        baseline_lane = None
        if include_baseline and config.decisions_model != "harness/heuristic":
            baseline_lane = new_lane("baseline", "harness/heuristic", "Harness")
            lanes["baseline"] = baseline_lane

        tasks = [
            run_text_lane(config, text_lane),
            run_decisions_lane(config, decisions_lane, policy_for(config), source="decisions"),
        ]
        if baseline_lane is not None:
            tasks.append(run_decisions_lane(config, baseline_lane, HeuristicPolicy(), source="baseline"))
        await asyncio.gather(*tasks)

        eligible = [
            lane for lane in (text_lane, decisions_lane) if lane["status"] == "solved" and lane["elapsed_ms"]
        ]
        winner = min(eligible, key=lambda lane: lane["elapsed_ms"])["kind"] if eligible else None
        result = {
            "id": record.get("id", f"hand-{index + 1}"),
            "hand": config.hand,
            "target": config.target,
            "solvable": record.get("solvable"),
            "reference_solution": record.get("solution"),
            "winner": winner,
            "lanes": lanes,
        }
        hand_results.append(result)
        if verbose:
            print(
                f"[{index + 1}/{len(records)}] {config.hand} target={config.target} "
                f"text={text_lane['status']}({text_lane['elapsed_ms']}ms) "
                f"decisions={decisions_lane['status']}({decisions_lane['elapsed_ms']}ms) "
                f"winner={winner}"
            )

    kinds = ["text", "decisions"] + (["baseline"] if include_baseline else [])
    aggregate = _aggregate(hand_results, kinds)
    output = {
        "started_at": started_at,
        "config": {
            "text_model": text_model,
            "decisions_model": decisions_model,
            "text_max_attempts": text_max_attempts,
            "max_expansions": max_expansions,
            "include_baseline": include_baseline,
            "seed": seed,
        },
        "aggregate": aggregate,
        "hands": hand_results,
    }

    json_path = output_dir / f"benchmark-{started_at}.json"
    md_path = output_dir / f"benchmark-{started_at}.md"
    json_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown(output), encoding="utf-8")
    output["json_path"] = str(json_path)
    output["md_path"] = str(md_path)
    if verbose:
        print()
        print(_markdown(output))
        print(f"saved: {json_path}")
        print(f"saved: {md_path}")
    return output


def _markdown(output: dict[str, Any]) -> str:
    agg = output["aggregate"]
    config = output["config"]
    lines = [
        "# Wu card game benchmark",
        "",
        f"- Text model (Nam): `{config['text_model']}`",
        f"- Decisions model (OpenRouter): `{config['decisions_model']}`",
        f"- Guided search budget: {config['max_expansions']} expansions per hand",
        f"- Attempts per hand (text): {config['text_max_attempts']}",
        "",
        "| lane | solved | rate | median solved | mean solved | p95 | mean calls | mean tokens | cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for kind in ("text", "decisions", "baseline"):
        if kind not in agg:
            continue
        item = agg[kind]
        lines.append(
            "| {kind} | {solved}/{runs} | {rate:.0%} | {median} | {mean} | {p95} | {calls} | {tokens} | {cost} |".format(
                kind=kind,
                solved=item["solved"],
                runs=item["runs"],
                rate=item["solved_rate"],
                median=f"{item['median_solved_ms']} ms" if item["median_solved_ms"] else "—",
                mean=f"{item['mean_solved_ms']} ms" if item["mean_solved_ms"] else "—",
                p95=f"{item['p95_solved_ms']} ms" if item["p95_solved_ms"] else "—",
                calls=item["mean_calls"],
                tokens=round(item["mean_prompt_tokens"] + item["mean_completion_tokens"]),
                cost=item["total_cost"],
            )
        )
    lines.extend(["", "## Hands", "", "| hand | target | text | decisions | winner |", "| --- | ---: | --- | --- | --- |"])
    for result in output["hands"]:
        text = result["lanes"]["text"]
        decisions = result["lanes"]["decisions"]
        lines.append(
            "| {hand} | {target} | {text_status} / {text_ms} ms | {dec_status} / {dec_ms} ms | {winner} |".format(
                hand=" ".join(str(card) for card in result["hand"]),
                target=result["target"],
                text_status=text["status"],
                text_ms=text["elapsed_ms"] if text["elapsed_ms"] is not None else "—",
                dec_status=decisions["status"],
                dec_ms=decisions["elapsed_ms"] if decisions["elapsed_ms"] is not None else "—",
                winner=result["winner"] or "—",
            )
        )
    return "\n".join(lines) + "\n"


def run_from_file(
    dataset_path: Path,
    limit: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    from .dataset import load_dataset

    records = load_dataset(dataset_path)
    if limit:
        records = records[:limit]
    return asyncio.run(run_benchmark(records, **kwargs))
