from __future__ import annotations

import asyncio
import random
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config
from .clients import list_decision_models, list_nam_models, provider_status
from .game import Rules, generate_hand, solve
from .race import analyze_hand, manager

FALLBACK_TEXT_MODELS = [
    "deepseek-v4-flash",
    "qwen3.6",
    "gemma4",
    "qwen3.8-flash",
    "glm5.3-flash",
    "mimo-v2.6-flash",
]


class AnalyzeRequest(BaseModel):
    hand: list[int] = Field(default_factory=lambda: list(config.DEFAULT_HAND))
    target: int = config.DEFAULT_TARGET
    rules: dict[str, Any] | None = None


class RaceRequest(BaseModel):
    hand: list[int] = Field(default_factory=lambda: list(config.DEFAULT_HAND))
    target: int = config.DEFAULT_TARGET
    rules: dict[str, Any] | None = None
    text_model: str = config.DEFAULT_TEXT_MODEL
    decisions_model: str = config.DEFAULT_DECISIONS_MODEL
    text_max_attempts: int = 1
    beam_width: int = 4
    branching: int = 2
    include_baseline: bool = False
    seed: int = 0

    def to_config(self) -> dict[str, Any]:
        return self.model_dump()


app = FastAPI(title="Wu Race Control", version=__version__)
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    status_task = provider_status()
    nam_task = list_nam_models()
    decisions_task = list_decision_models()
    status, nam_result, decisions_result = await asyncio.gather(
        status_task, nam_task, decisions_task, return_exceptions=True
    )

    if isinstance(status, Exception):
        status = {
            "nam": {"ok": False, "detail": str(status)},
            "openrouter": {"ok": False, "detail": str(status)},
        }
    if isinstance(nam_result, Exception):
        nam_models = [{"id": model, "owned_by": ""} for model in FALLBACK_TEXT_MODELS]
        nam_error = str(nam_result)
    else:
        nam_models = nam_result or [{"id": model, "owned_by": ""} for model in FALLBACK_TEXT_MODELS]
        nam_error = None
    if isinstance(decisions_result, Exception):
        decision_models, decision_error = [], str(decisions_result)
    else:
        decision_models, decision_error = decisions_result

    dataset_count = 0
    dataset_path = config.DATA_DIR / "hands.jsonl"
    if dataset_path.exists():
        dataset_count = sum(1 for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip())

    return {
        "version": __version__,
        "keys": status,
        "nam_models": nam_models,
        "nam_error": nam_error,
        "decision_models": decision_models,
        "decision_error": decision_error,
        "heuristic_model": config.HEURISTIC_MODEL,
        "defaults": {
            "hand": config.DEFAULT_HAND,
            "target": config.DEFAULT_TARGET,
            "text_model": config.DEFAULT_TEXT_MODEL,
            "decisions_model": config.DEFAULT_DECISIONS_MODEL,
            "rules": Rules().to_dict(),
            "text_max_attempts": 1,
            "beam_width": 4,
            "branching": 2,
            "include_baseline": False,
        },
        "samples": [
            {"name": "Podcast hand", "hand": [2, 8, 9, 3, 12, 12], "target": 163},
            {"name": "Warm-up", "hand": [3, 3, 8, 8, 9, 12], "target": 163},
        ],
        "dataset": {"count": dataset_count, "path": str(dataset_path.relative_to(config.ROOT))},
    }


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    if not (2 <= len(request.hand) <= 6):
        raise HTTPException(status_code=422, detail="hand must contain 2 to 6 cards")
    rules = Rules.from_dict(request.rules)
    if rules.allow_reuse:
        raise HTTPException(status_code=422, detail="allow_reuse is not supported by the exact solver")
    return analyze_hand(sorted(request.hand), request.target, rules)


@app.get("/api/hands/random")
async def random_hand(
    target: int = config.DEFAULT_TARGET,
    solvable: bool = True,
    rules: str | None = None,
) -> dict[str, Any]:
    del rules
    rules_obj = Rules()
    rng = random.Random(secrets.randbits(64))
    for _ in range(3000):
        hand = generate_hand(rng)
        solution = solve(hand, target, rules_obj)
        if (solution is not None) == solvable:
            return {
                "hand": hand,
                "target": target,
                "rules": rules_obj.to_dict(),
                "solvable": solution is not None,
                "solution": [step.to_dict() for step in solution] if solution else None,
            }
    raise HTTPException(status_code=503, detail="could not deal a matching hand; try another target")


@app.post("/api/races")
async def create_race(request: RaceRequest) -> dict[str, Any]:
    try:
        race = manager.create(request.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    manager.start(race["id"])
    return manager.get(race["id"]) or race


@app.get("/api/races")
async def list_races(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"races": manager.history(limit)}


@app.get("/api/races/{race_id}")
async def get_race(race_id: str) -> dict[str, Any]:
    race = manager.get(race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")
    return race


@app.post("/api/races/{race_id}/cancel")
async def cancel_race(race_id: str) -> dict[str, Any]:
    if race_id not in manager.races:
        raise HTTPException(status_code=404, detail="race not found")
    cancelled = manager.cancel(race_id)
    return {"cancelled": cancelled, "race": manager.get(race_id)}


@app.get("/api/export/{race_id}")
async def export_race(race_id: str) -> FileResponse:
    path = config.RESULTS_DIR / "races" / f"{race_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="saved race not found")
    return FileResponse(path, media_type="application/json", filename=f"wu-race-{race_id}.json")


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("wu.server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    run()
