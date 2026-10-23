from __future__ import annotations

import json
from fractions import Fraction

from wu.game import Rules, solve
from wu.prompts import build_decisions_request, build_text_messages, parse_text_response


def test_parse_text_response_accepts_fenced_json() -> None:
    content = "```json\n" + json.dumps({"steps": [{"left": 2, "op": "*", "right": 8, "result": 16}]}) + "\n```"
    parsed = parse_text_response(content)
    assert parsed.error is None
    assert parsed.steps and parsed.steps[0]["result"] == 16


def test_parse_text_response_rejects_markdown_prose() -> None:
    parsed = parse_text_response("Sure! 2 * 8 = 16")
    assert parsed.error is not None


def test_parse_text_response_requires_all_fields() -> None:
    parsed = parse_text_response(json.dumps({"steps": [{"left": 2, "op": "*", "right": 8}]}))
    assert parsed.error is not None
    assert "missing" in parsed.error


def test_text_prompt_contains_rules_and_schema() -> None:
    messages = build_text_messages([2, 8, 9, 3, 12, 12], 163, Rules())
    prompt = messages[-1]["content"]
    assert "Use every card exactly once" in prompt
    assert '"steps"' in prompt
    assert "163" in prompt


def test_decisions_request_enumerates_every_move() -> None:
    values = tuple(sorted(Fraction(card) for card in [2, 8, 9, 3, 12, 12]))
    from wu.game import candidate_moves

    moves = candidate_moves(values, Rules())
    payload = build_decisions_request(
        "baseten/mammoth-1:free",
        [("s0", values, moves, [])],
        163,
        Rules(),
    )
    assert payload["model"] == "baseten/mammoth-1:free"
    question = payload["questions"]["s0_move"]
    assert question["type"] == "choice"
    assert len(question["criteria"]) == len(moves)
    assert payload["state"]["states"][0]["numbers"]


def test_solver_solution_replays_in_prompt_shape() -> None:
    solution = solve([2, 8, 9, 3, 12, 12], 163, Rules())
    assert solution is not None
    payload = {"steps": [step.to_dict() for step in solution]}
    parsed = parse_text_response(json.dumps(payload))
    assert parsed.error is None
    assert len(parsed.steps or []) == len(solution)
