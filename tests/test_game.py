from __future__ import annotations

import json
import random
from fractions import Fraction

from wu.game import (
    Rules,
    apply_move,
    candidate_moves,
    generate_hand,
    solve,
    verify_steps,
)


def test_podcast_hand_is_solvable_and_verified() -> None:
    hand = [2, 8, 9, 3, 12, 12]
    solution = solve(hand, 163, Rules())
    assert solution is not None
    verification = verify_steps(hand, 163, [step.to_dict() for step in solution])
    assert verification.ok, verification.error
    assert verification.final_values == [Fraction(163)]


def test_verifier_rejects_wrong_arithmetic() -> None:
    hand = [2, 8, 9, 3, 12, 12]
    steps = [
        {"left": 2, "op": "*", "right": 8, "result": 17},
        {"left": 9, "op": "/", "right": 3, "result": 3},
        {"left": 3, "op": "+", "right": 16, "result": 19},
        {"left": 12, "op": "*", "right": 12, "result": 144},
        {"left": 144, "op": "+", "right": 19, "result": 163},
    ]
    verification = verify_steps(hand, 163, steps)
    assert not verification.ok
    assert verification.arithmetic_errors


def test_verifier_rejects_missing_card() -> None:
    verification = verify_steps(
        [2, 8],
        10,
        [{"left": 2, "op": "+", "right": 9, "result": 11}],
        require_result=True,
    )
    assert not verification.ok
    assert "not available" in (verification.error or "")


def test_verifier_handles_reversed_operands() -> None:
    verification = verify_steps(
        [2, 8],
        6,
        [{"left": 8, "op": "-", "right": 2, "result": 6}],
        Rules(must_use_all=True),
    )
    assert verification.ok, verification.error


def test_verifier_rejects_non_integer_intermediate() -> None:
    verification = verify_steps(
        [2, 8],
        4,
        [{"left": 2, "op": "/", "right": 8, "result": "1/4"}],
        Rules(must_use_all=True, integer_only=True),
    )
    assert not verification.ok


def test_candidate_moves_include_reversed_subtraction() -> None:
    values = (Fraction(2), Fraction(8))
    moves = candidate_moves(values, Rules())
    texts = {move.text(values) for move in moves}
    assert "8 − 2 = 6" in texts
    assert "2 − 8 = -6" in texts


def test_apply_move_removes_two_operands() -> None:
    values = (Fraction(2), Fraction(8), Fraction(3))
    move = next(move for move in candidate_moves(values, Rules()) if move.text(values) == "8 − 2 = 6")
    assert apply_move(values, move) == (Fraction(3), Fraction(6))


def test_generate_hand_is_reproducible() -> None:
    first = generate_hand(random.Random(42))
    second = generate_hand(random.Random(42))
    assert first == second
    assert len(first) == 6


def test_solver_finds_unsolvable_hand_quickly() -> None:
    # A deliberately constrained state: 4 cards and a far target.
    hand = [1, 1, 1, 1]
    assert solve(hand, 100, Rules()) is None


def test_solution_json_is_serializable() -> None:
    solution = solve([2, 8, 9, 3, 12, 12], 163, Rules())
    assert solution is not None
    json.dumps([step.to_dict() for step in solution])
