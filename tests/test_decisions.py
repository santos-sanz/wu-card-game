from __future__ import annotations

import asyncio
from fractions import Fraction

import pytest

from wu.clients import DecisionsCall
from wu.game import Rules, solve
from wu.models import RaceConfig, new_lane
from wu.runners import OpenRouterPolicy, run_decisions_lane


class OracleDecisionsClient:
    """Fake OpenRouter endpoint: answers with a move from the exact solver."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, payload, timeout=180.0):
        self.calls += 1
        answers = {}
        for state in payload["state"]["states"]:
            state_id = state["id"]
            question = payload["questions"][f"{state_id}_move"]
            values = [Fraction(value) for value in state["numbers"]]
            rules = Rules.from_dict(
                {
                    "must_use_all": payload["state"]["rules"]["must_use_all"],
                    "allow_reuse": payload["state"]["rules"]["allow_reuse"],
                    "integer_only": payload["state"]["rules"]["integer_only_intermediates"],
                }
            )
            solution = solve(values, payload["state"]["target"], rules)
            target_text = solution[0].to_dict()["text"] if solution else None
            choice = None
            for key, description in question["criteria"].items():
                if target_text and description == target_text:
                    choice = key
                    break
            if choice is None:
                choice = next(iter(question["criteria"]))
            answers[f"{state_id}_move"] = {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: 0.99},
                "confidence": 0.99,
            }
        return DecisionsCall(
            model=payload["model"],
            answers=answers,
            provider="fake",
            latency_ms=12.0,
            prompt_tokens=900,
            completion_tokens=0,
            total_tokens=900,
            cost=0.0,
            raw={"answers": answers},
        )


@pytest.mark.asyncio
async def test_decisions_lane_solves_with_oracle_policy() -> None:
    client = OracleDecisionsClient()
    policy = OpenRouterPolicy("fake/decisions")
    policy.client = client
    config = RaceConfig.from_dict(
        {
            "hand": [2, 8, 9, 3, 12, 12],
            "target": 163,
            "decisions_model": "fake/decisions",
            "beam_width": 4,
            "branching": 2,
        }
    )
    lane = new_lane("decisions", config.decisions_model, "OpenRouter")
    await run_decisions_lane(config, lane, policy, source="decisions")
    assert lane["status"] == "solved", lane.get("error") or lane.get("verification")
    assert lane["solution"]["expression"]
    assert lane["calls"] == 5
    assert client.calls == 5
    assert lane["verification"]["ok"] is True
