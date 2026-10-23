from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

OPS = ("+", "-", "*", "/")
OP_SYMBOL = {"+": "+", "-": "−", "*": "×", "/": "÷"}


def to_fraction(value: object) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    if isinstance(value, str):
        return Fraction(value.strip())
    raise ValueError(f"not a number: {value!r}")


def fmt(value: Fraction | int) -> str:
    value = to_fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def json_number(value: Fraction) -> int | str:
    value = to_fraction(value)
    if value.denominator == 1:
        return value.numerator
    return fmt(value)


@dataclass(frozen=True)
class Rules:
    must_use_all: bool = True
    allow_reuse: bool = False
    integer_only: bool = True

    def to_dict(self) -> dict:
        return {
            "must_use_all": self.must_use_all,
            "allow_reuse": self.allow_reuse,
            "integer_only": self.integer_only,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Rules":
        data = data or {}
        return cls(
            must_use_all=bool(data.get("must_use_all", True)),
            allow_reuse=bool(data.get("allow_reuse", False)),
            integer_only=bool(data.get("integer_only", True)),
        )


@dataclass(frozen=True)
class Step:
    left: Fraction
    op: str
    right: Fraction
    result: Fraction

    def to_dict(self) -> dict:
        return {
            "left": json_number(self.left),
            "op": self.op,
            "right": json_number(self.right),
            "result": json_number(self.result),
            "text": f"{fmt(self.left)} {OP_SYMBOL.get(self.op, self.op)} {fmt(self.right)} = {fmt(self.result)}",
        }


@dataclass(frozen=True)
class Move:
    i: int
    j: int
    op: str
    result: Fraction
    left: Fraction
    right: Fraction

    def text(self, values: Sequence[Fraction] | None = None) -> str:
        return f"{fmt(self.left)} {OP_SYMBOL.get(self.op, self.op)} {fmt(self.right)} = {fmt(self.result)}"


@dataclass
class Verification:
    ok: bool
    error: str | None
    arithmetic_errors: list[str]
    steps: list[Step]
    final_values: list[Fraction]
    final_expression: str | None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "arithmetic_errors": self.arithmetic_errors,
            "steps": [step.to_dict() for step in self.steps],
            "final_values": [json_number(v) for v in self.final_values],
            "final_expression": self.final_expression,
        }


def apply_op(left: Fraction, right: Fraction, op: str) -> Fraction:
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise ZeroDivisionError("division by zero")
        return left / right
    raise ValueError(f"unsupported operator: {op}")


def _op_results(left: Fraction, right: Fraction) -> Iterable[tuple[str, Fraction, Fraction, Fraction]]:
    """Yield (op, left_operand, right_operand, result) including reversed non-commutative ops."""
    yield "+", left, right, left + right
    yield "*", left, right, left * right
    yield "-", left, right, left - right
    yield "-", right, left, right - left
    if right != 0:
        yield "/", left, right, left / right
    if left != 0:
        yield "/", right, left, right / left


def candidate_moves(values: Sequence[Fraction], rules: Rules | None = None) -> list[Move]:
    """Every legal pair/operator move from a multiset of values."""
    rules = rules or Rules()
    values = tuple(to_fraction(v) for v in values)
    moves: list[Move] = []
    seen: set[tuple[str, str, str]] = set()
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            for op, op_left, op_right, result in _op_results(values[i], values[j]):
                if rules.integer_only and result.denominator != 1:
                    continue
                # Deduplicate transitions that are identical from the model's point of view.
                key = (fmt(op_left), op, fmt(op_right))
                if key in seen:
                    continue
                seen.add(key)
                moves.append(Move(i=i, j=j, op=op, result=result, left=op_left, right=op_right))
    moves.sort(key=lambda m: (fmt(m.left), m.op, fmt(m.right), fmt(m.result)))
    return moves


def apply_move(values: Sequence[Fraction], move: Move) -> tuple[Fraction, ...]:
    values = list(to_fraction(v) for v in values)
    left, right = values.pop(move.i), values.pop(move.j - 1 if move.j > move.i else move.j)
    return tuple(sorted(values + [move.result]))


def verify_steps(
    hand: Sequence[int | float | str],
    target: int | float | str,
    steps: Sequence[Step | dict],
    rules: Rules | None = None,
    require_result: bool = True,
) -> Verification:
    rules = rules or Rules()
    target_f = to_fraction(target)
    state: list[tuple[Fraction, str]] = [(to_fraction(card), fmt(card)) for card in hand]
    parsed: list[Step] = []
    arithmetic_errors: list[str] = []
    used_steps = 0

    for index, raw in enumerate(steps, start=1):
        if isinstance(raw, Step):
            step = raw
            stated_result: Fraction | None = raw.result
        else:
            try:
                left = to_fraction(raw["left"])
                right = to_fraction(raw["right"])
                op = str(raw["op"])
            except (KeyError, TypeError, ValueError) as exc:
                return Verification(False, f"step {index}: malformed step ({exc})", arithmetic_errors, parsed, [v for v, _ in state], None)
            if op not in OPS:
                return Verification(False, f"step {index}: unsupported operator {op!r}", arithmetic_errors, parsed, [v for v, _ in state], None)
            stated_raw = raw.get("result")
            stated_result = None if stated_raw is None else to_fraction(stated_raw)
            if stated_result is None and require_result:
                return Verification(False, f"step {index}: missing result", arithmetic_errors, parsed, [v for v, _ in state], None)
            step = Step(left=left, op=op, right=right, result=stated_result if stated_result is not None else Fraction(0))

        if step.op not in OPS:
            return Verification(False, f"step {index}: unsupported operator {step.op!r}", arithmetic_errors, parsed, [v for v, _ in state], None)

        left_pos = _find_value(state, step.left)
        if left_pos is None:
            available = ", ".join(fmt(v) for v, _ in state)
            return Verification(False, f"step {index}: {fmt(step.left)} is not available (available: {available})", arithmetic_errors, parsed, [v for v, _ in state], None)
        left_value, left_expr = state[left_pos]
        if not rules.allow_reuse:
            state.pop(left_pos)
        else:
            left_value, left_expr = state[left_pos]

        right_pos = _find_value(state, step.right)
        if right_pos is None:
            available = ", ".join(fmt(v) for v, _ in state)
            return Verification(False, f"step {index}: {fmt(step.right)} is not available (available: {available})", arithmetic_errors, parsed, [v for v, _ in state], None)
        right_value, right_expr = state[right_pos]
        if not rules.allow_reuse:
            state.pop(right_pos)
        else:
            right_value, right_expr = state[right_pos]

        try:
            computed = apply_op(left_value, right_value, step.op)
        except ZeroDivisionError:
            return Verification(False, f"step {index}: division by zero", arithmetic_errors, parsed, [v for v, _ in state], None)
        if rules.integer_only and computed.denominator != 1:
            return Verification(False, f"step {index}: intermediate result {fmt(computed)} is not an integer", arithmetic_errors, parsed, [v for v, _ in state], None)

        if stated_result is not None and stated_result != computed:
            arithmetic_errors.append(
                f"step {index}: stated {fmt(stated_result)} but actual {fmt(computed)}"
            )
        parsed.append(Step(left=left_value, op=step.op, right=right_value, result=computed))
        expression = f"({left_expr} {step.op} {right_expr})"
        state.append((computed, expression))
        used_steps += 1

    if arithmetic_errors:
        return Verification(False, arithmetic_errors[0], arithmetic_errors, parsed, [v for v, _ in state], None)

    final_values = [v for v, _ in state]
    if rules.must_use_all:
        if len(final_values) != 1:
            return Verification(False, f"must use all cards: {len(final_values)} values remain", arithmetic_errors, parsed, final_values, None)
        if final_values[0] != target_f:
            return Verification(False, f"final value {fmt(final_values[0])} does not equal target {fmt(target_f)}", arithmetic_errors, parsed, final_values, None)
        expression = state[0][1]
    else:
        if target_f not in final_values:
            return Verification(False, f"target {fmt(target_f)} is not among final values", arithmetic_errors, parsed, final_values, None)
        expression = next(expr for value, expr in state if value == target_f)

    if used_steps == 0 and len(hand) > 0 and rules.must_use_all and len(hand) > 1:
        return Verification(False, "no steps provided", arithmetic_errors, parsed, final_values, None)

    expression = expression[1:-1] if expression.startswith("(") and expression.endswith(")") else expression
    return Verification(True, None, arithmetic_errors, parsed, final_values, expression)


def _find_value(state: list[tuple[Fraction, str]], value: Fraction) -> int | None:
    for index, (candidate, _) in enumerate(state):
        if candidate == value:
            return index
    return None


def solve(
    hand: Sequence[int | float | str],
    target: int | float | str,
    rules: Rules | None = None,
    stats: dict | None = None,
) -> list[Step] | None:
    """Exhaustive expression-tree search. Returns one verified reference solution."""
    rules = rules or Rules()
    if rules.allow_reuse:
        raise ValueError("the exact solver does not support allow_reuse")

    target_f = to_fraction(target)
    start = tuple(sorted(to_fraction(card) for card in hand))
    seen: set[tuple[Fraction, ...]] = set()
    nodes = 0

    def dfs(values: tuple[Fraction, ...]) -> tuple[Step, ...] | None:
        nonlocal nodes
        nodes += 1
        if not rules.must_use_all and target_f in values:
            return ()
        if len(values) == 1:
            return () if values[0] == target_f else None
        if values in seen:
            return None
        seen.add(values)

        candidates: list[tuple[Fraction, Move]] = []
        for move in candidate_moves(values, rules):
            candidates.append((abs(move.result - target_f), move))
        candidates.sort(key=lambda item: (item[0], item[1].op, fmt(item[1].result)))

        for _, move in candidates:
            next_values = apply_move(values, move)
            sub = dfs(next_values)
            if sub is not None:
                return (Step(left=move.left, op=move.op, right=move.right, result=move.result),) + sub
        return None

    result = dfs(start)
    if stats is not None:
        stats["nodes"] = nodes
        stats["seen"] = len(seen)
    return list(result) if result is not None else None


def solve_many(hands: Iterable[Sequence[int]], target: int, rules: Rules | None = None):
    for hand in hands:
        yield list(hand), solve(hand, target, rules)


def generate_hand(rng: random.Random, count: int = 6, deck: Sequence[int] | None = None, copies: int = 4) -> list[int]:
    deck = list(deck or range(1, 14))
    pool = [value for value in deck for _ in range(copies)]
    return sorted(rng.sample(pool, count))


def heuristic_state_score(values: Sequence[Fraction], target: Fraction) -> float:
    """Cheap, solver-free ranking used only for the harness baseline and beam pruning."""
    values = [to_fraction(v) for v in values]
    if not values:
        return 1e9
    closest = min(abs(value - target) for value in values)
    spread = max(values) - min(values)
    return float(closest) + 0.25 * len(values) + 0.001 * float(spread)
