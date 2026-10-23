from __future__ import annotations

import asyncio
import json

import pytest

from wu.clients import TextCall
from wu.game import Rules, solve
from wu.race import manager


class FakeNamClient:
    def __init__(self, *args, **kwargs):
        pass

    async def complete(self, model, messages, **kwargs):
        solution = solve([2, 8, 9, 3, 12, 12], 163, Rules())
        assert solution is not None
        content = json.dumps({"steps": [step.to_dict() for step in solution]})
        return TextCall(
            model=model,
            content=content,
            reasoning=None,
            latency_ms=4.0,
            prompt_tokens=120,
            completion_tokens=30,
            total_tokens=150,
            cost=0.0,
            raw={"choices": [{"message": {"content": content}}]},
        )


@pytest.mark.asyncio
async def test_full_race_with_heuristic_decisions(monkeypatch) -> None:
    monkeypatch.setattr("wu.runners.NamClient", FakeNamClient)
    manager.races.clear()
    race = manager.create(
        {
            "hand": [2, 8, 9, 3, 12, 12],
            "target": 163,
            "decisions_model": "harness/heuristic",
            "text_model": "fake-text",
            "text_max_attempts": 1,
            "beam_width": 4,
            "branching": 2,
            "include_baseline": False,
            "seed": 4,
        }
    )
    manager.start(race["id"])
    result = None
    for _ in range(400):
        await asyncio.sleep(0.02)
        result = manager.get(race["id"])
        if result and result["status"] in ("done", "error", "cancelled"):
            break
    assert result is not None
    assert result["status"] == "done"
    assert result["lanes"]["text"]["status"] == "solved"
    assert result["lanes"]["text"]["solution"]["expression"]
    assert result["lanes"]["decisions"]["status"] in ("solved", "unsolved")
    assert result["lanes"]["decisions"]["elapsed_ms"] is not None
    assert result["winner"] in ("text", "decisions", "tie")
