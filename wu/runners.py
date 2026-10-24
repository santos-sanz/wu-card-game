from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Protocol, Sequence

from .clients import NamClient, OpenRouterDecisionsClient, ProviderError
from .config import HEURISTIC_MODEL
from .game import (
    Move,
    Rules,
    Step,
    Verification,
    apply_move,
    candidate_moves,
    fmt,
    heuristic_state_score,
    json_number,
    solve,
    to_fraction,
    verify_steps,
)
from .models import RaceConfig, now_iso
from .prompts import build_decisions_request, build_text_messages, parse_text_response


def trace(lane: dict[str, Any], message: str, **data: Any) -> None:
    entry = {"at": now_iso(), "message": message}
    if data:
        entry["data"] = data
    lane["trace"].append(entry)
    if len(lane["trace"]) > 240:
        del lane["trace"][:-240]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _finish_lane(lane: dict[str, Any], started: float, status: str) -> None:
    lane["status"] = status
    lane["finished_at"] = now_iso()
    lane["elapsed_ms"] = _elapsed_ms(started)


def _solution_payload(verification: Verification, source: str, extra: dict | None = None) -> dict[str, Any]:
    payload = {
        "steps": [step.to_dict() for step in verification.steps],
        "expression": verification.final_expression,
        "final_values": [json_number(v) for v in verification.final_values],
        "source": source,
    }
    if extra:
        payload.update(extra)
    return payload


def _accumulate(lane: dict[str, Any], prompt: int, completion: int, total: int, cost: float) -> None:
    lane["tokens"]["prompt"] += prompt
    lane["tokens"]["completion"] += completion
    lane["tokens"]["total"] += total
    lane["cost"] = round(lane["cost"] + cost, 8)


async def run_text_lane(config: RaceConfig, lane: dict[str, Any]) -> None:
    client = NamClient()
    started = time.perf_counter()
    lane["started_at"] = now_iso()
    lane["status"] = "running"
    trace(lane, f"opening lane with {config.text_model}")

    feedback: str | None = None
    for attempt in range(1, config.text_max_attempts + 1):
        messages = build_text_messages(config.hand, config.target, config.rules, feedback)
        trace(lane, f"attempt {attempt}/{config.text_max_attempts}: requesting full solution")
        try:
            call = await client.complete(config.text_model, messages)
        except ProviderError as exc:
            lane["error"] = str(exc)
            trace(lane, f"provider error: {exc}")
            _finish_lane(lane, started, "error")
            return
        except Exception as exc:  # pragma: no cover - defensive
            lane["error"] = f"unexpected error: {exc}"
            trace(lane, lane["error"])
            _finish_lane(lane, started, "error")
            return

        lane["calls"] += 1
        _accumulate(lane, call.prompt_tokens, call.completion_tokens, call.total_tokens, call.cost)
        lane["raw"].append(
            {
                "attempt": attempt,
                "request": {"model": config.text_model, "messages": messages},
                "response": call.raw,
            }
        )

        response_text = call.content or ""
        used_reasoning = False
        if not response_text.strip() and call.reasoning:
            response_text = call.reasoning
            used_reasoning = True
        parsed = parse_text_response(response_text)
        if parsed.error:
            verification = Verification(
                ok=False,
                error=parsed.error,
                arithmetic_errors=[],
                steps=[],
                final_values=[],
                final_expression=None,
            )
        else:
            verification = verify_steps(
                config.hand,
                config.target,
                parsed.steps or [],
                config.rules,
                require_result=True,
            )

        lane["verification"] = verification.to_dict()
        lane["attempts"].append(
            {
                "attempt": attempt,
                "latency_ms": round(call.latency_ms, 1),
                "tokens": call.total_tokens,
                "ok": verification.ok,
                "error": verification.error,
                "content": call.content[:4000],
                "reasoning": (call.reasoning or "")[:4000],
                "used_reasoning": used_reasoning,
            }
        )

        if verification.ok:
            lane["solution"] = _solution_payload(
                verification,
                source="text",
                extra={"attempt": attempt, "model_latency_ms": round(call.latency_ms, 1)},
            )
            trace(lane, f"solved in {round((time.perf_counter() - started) * 1000)} ms", attempt=attempt)
            _finish_lane(lane, started, "solved")
            return

        feedback = verification.error or parsed.error or "invalid solution"
        if not call.content.strip() and call.reasoning:
            feedback = f"{feedback}. Your message content was empty; put the JSON object in the final answer."
        trace(lane, f"attempt {attempt} rejected: {feedback}")

    _finish_lane(lane, started, "unsolved")


@dataclass
class WorkItem:
    state_id: str
    values: tuple[Fraction, ...]
    history: list[Step]
    moves: list[Move]
    score: float = 0.0


@dataclass
class Selection:
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0
    raw: dict[str, Any] | None = None


class MovePolicy(Protocol):
    async def select(
        self,
        work: Sequence[WorkItem],
        target: int,
        rules: Rules,
        config: RaceConfig,
    ) -> Selection: ...


class OpenRouterPolicy:
    def __init__(self, model: str):
        self.model = model
        self.client = OpenRouterDecisionsClient()

    async def select(
        self,
        work: Sequence[WorkItem],
        target: int,
        rules: Rules,
        config: RaceConfig,
    ) -> Selection:
        payload = build_decisions_request(
            self.model,
            [
                (
                    item.state_id,
                    item.values,
                    item.moves,
                    [step.to_dict()["text"] for step in item.history],
                )
                for item in work
            ],
            target,
            rules,
        )
        call = await self.client.decide(payload)
        probabilities: dict[str, dict[str, float]] = {}
        for item in work:
            answer = call.answers.get(f"{item.state_id}_move")
            if not isinstance(answer, dict):
                answer = {}
            descriptions = {move.text(item.values): f"m{index:02d}" for index, move in enumerate(item.moves)}
            raw: dict[str, Any] = {}
            raw_probabilities = answer.get("probabilities")
            if not isinstance(raw_probabilities, dict):
                raw_probabilities = {}
            for key, value in raw_probabilities.items():
                mapped = key if key in descriptions.values() else descriptions.get(str(key), key)
                try:
                    raw[mapped] = float(value)
                except (TypeError, ValueError):
                    continue
            choice = answer.get("choice")
            if isinstance(choice, str):
                mapped_choice = choice if choice in descriptions.values() else descriptions.get(choice, choice)
                if mapped_choice not in raw:
                    raw[mapped_choice] = max(raw.values(), default=1.0)
            if not raw:
                raw = {f"m{index:02d}": 1.0 for index in range(len(item.moves))}
            probabilities[item.state_id] = _normalise(raw)
        return Selection(
            probabilities=probabilities,
            calls=1,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            total_tokens=call.total_tokens,
            cost=call.cost,
            latency_ms=call.latency_ms,
            raw={"request": payload, "response": call.raw},
        )


class HeuristicPolicy:
    async def select(
        self,
        work: Sequence[WorkItem],
        target: int,
        rules: Rules,
        config: RaceConfig,
    ) -> Selection:
        target_f = to_fraction(target)
        probabilities: dict[str, dict[str, float]] = {}
        for item in work:
            scores: dict[str, float] = {}
            for index, move in enumerate(item.moves):
                next_values = apply_move(item.values, move)
                score = heuristic_state_score(next_values, target_f)
                scores[f"m{index:02d}"] = -score
            if not scores:
                continue
            # Softmax over negative heuristic distances.
            peak = max(scores.values())
            weights = {key: math.exp((value - peak) / 12.0) for key, value in scores.items()}
            probabilities[item.state_id] = _normalise(weights)
        return Selection(probabilities=probabilities)


def _normalise(values: dict[str, float]) -> dict[str, float]:
    cleaned = {key: max(0.0, value) for key, value in values.items() if math.isfinite(value)}
    total = sum(cleaned.values())
    if total <= 0:
        if not cleaned:
            return {}
        share = 1.0 / len(cleaned)
        return {key: share for key in cleaned}
    return {key: value / total for key, value in cleaned.items()}


def _shuffle_moves(moves: list[Move], seed: int, values: Sequence[Fraction], depth: int) -> list[Move]:
    rng = random.Random(seed + depth * 1009 + sum(int(v) for v in values if v.denominator == 1))
    shuffled = list(moves)
    rng.shuffle(shuffled)
    return shuffled


def _state_is_solved(values: Sequence[Fraction], target: Fraction, rules: Rules) -> bool:
    if rules.must_use_all:
        return len(values) == 1 and values[0] == target
    return target in values


async def run_decisions_lane(
    config: RaceConfig,
    lane: dict[str, Any],
    policy: MovePolicy,
    source: str = "decisions",
) -> None:
    """Model-guided depth-first search with exact reachability pruning.

    The Decisions model ranks the legal moves at every expanded state. The
    harness then tries moves in that order and uses the exact solver only as a
    boolean ``is this successor still solvable?`` oracle to prune dead ends.
    This guarantees a solution for a solvable hand while keeping the model in
    charge of move ordering.
    """
    started = time.perf_counter()
    lane["started_at"] = now_iso()
    lane["status"] = "running"
    trace(lane, f"opening lane with {lane['model']}")

    target = to_fraction(config.target)
    initial_values = tuple(sorted(to_fraction(card) for card in config.hand))
    max_expansions = max(1, config.max_expansions)
    stats = {
        "expansions": 0,
        "pruned": 0,
        "solver_checks": 0,
        "model_top_valid": 0,
        "backtracks": 0,
    }
    reach_cache: dict[tuple[Fraction, ...], bool] = {}
    failed: set[tuple[Fraction, ...]] = set()
    path: list[dict[str, Any]] = []

    def reachable(values: tuple[Fraction, ...]) -> bool:
        if values in reach_cache:
            return reach_cache[values]
        stats["solver_checks"] += 1
        ok = solve(list(values), config.target, config.rules) is not None
        reach_cache[values] = ok
        return ok

    def set_beam() -> None:
        lane["beam"] = [
            {
                "numbers": [json_number(v) for v in item["values"]],
                "history": [step.to_dict()["text"] for step in item["history"]],
                "score": round(float(item.get("score") or 0.0), 4),
            }
            for item in reversed(path)
        ][:6]
        lane["live_steps"] = (
            [step.to_dict() for step in path[-1]["history"]] if path else []
        )

    lane["beam"] = [
        {"numbers": [json_number(v) for v in initial_values], "history": [], "score": 0.0}
    ]
    lane["live_steps"] = []

    if _state_is_solved(initial_values, target, config.rules):
        verification = verify_steps(config.hand, config.target, [], config.rules, require_result=True)
        lane["solution"] = _solution_payload(verification, source=source, extra=stats)
        lane["verification"] = verification.to_dict()
        trace(lane, "target already present")
        _finish_lane(lane, started, "solved")
        return

    trace(
        lane,
        f"guided search: up to {max_expansions} expansions, exact pruning on",
        call_expected=1 if source == "decisions" else 0,
    )

    async def dfs(values: tuple[Fraction, ...], history: list[Step]) -> list[Step] | None:
        if _state_is_solved(values, target, config.rules):
            return history
        if values in failed:
            return None
        if stats["expansions"] >= max_expansions:
            trace(lane, f"budget reached at {values and [json_number(v) for v in values]}")
            return None

        stats["expansions"] += 1
        moves = candidate_moves(values, config.rules)
        if not moves:
            failed.add(values)
            return None
        moves = _shuffle_moves(moves, config.seed, values, len(history) + 1)

        path.append({"values": values, "history": list(history), "score": 0.0})
        set_beam()
        trace(
            lane,
            f"expand {stats['expansions']}: [{', '.join(fmt(v) for v in values)}] -> {len(moves)} moves",
            call_expected=1 if source == "decisions" else 0,
        )

        work = [WorkItem(state_id="s0", values=values, history=history, moves=moves)]
        try:
            selection = await policy.select(work, config.target, config.rules, config)
        except ProviderError as exc:
            lane["error"] = str(exc)
            trace(lane, f"provider error: {exc}")
            _finish_lane(lane, started, "error")
            return None
        except Exception as exc:  # pragma: no cover - defensive
            lane["error"] = f"unexpected error: {exc}"
            trace(lane, lane["error"])
            _finish_lane(lane, started, "error")
            return None

        if selection.calls:
            lane["calls"] += selection.calls
            _accumulate(
                lane,
                selection.prompt_tokens,
                selection.completion_tokens,
                selection.total_tokens,
                selection.cost,
            )
            if selection.raw is not None:
                lane["raw"].append(
                    {
                        "depth": len(history),
                        "numbers": [json_number(v) for v in values],
                        "request": selection.raw.get("request"),
                        "response": selection.raw.get("response"),
                    }
                )
            trace(
                lane,
                f"decisions call returned in {round(selection.latency_ms)} ms",
                prompt_tokens=selection.prompt_tokens,
                total_tokens=selection.total_tokens,
            )

        probabilities = selection.probabilities.get("s0") or {}
        ranked: list[tuple[float, float, int, Move, tuple[Fraction, ...]]] = []
        for index, move in enumerate(moves):
            probability = float(probabilities.get(f"m{index:02d}", 0.0))
            next_values = apply_move(values, move)
            ranked.append(
                (probability, heuristic_state_score(next_values, target), index, move, next_values)
            )
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))

        if ranked:
            top_probability, _, _, top_move, top_values = ranked[0]
            top_ok = _state_is_solved(top_values, target, config.rules) or reachable(top_values)
            if top_ok:
                stats["model_top_valid"] += 1
            else:
                trace(lane, f"model top move is a dead end: {top_move.text()} (p={top_probability:.3f})")

        try:
            for probability, _, _, move, next_values in ranked:
                if stats["expansions"] >= max_expansions:
                    break
                step = Step(left=move.left, op=move.op, right=move.right, result=move.result)
                if _state_is_solved(next_values, target, config.rules):
                    return history + [step]
                if config.rules.must_use_all and len(next_values) == 1:
                    continue
                if not reachable(next_values):
                    stats["pruned"] += 1
                    trace(lane, f"prune {move.text()} (p={probability:.3f})")
                    continue
                path[-1]["score"] = probability
                result = await dfs(next_values, history + [step])
                if result is not None:
                    return result
                stats["backtracks"] += 1

            if stats["expansions"] < max_expansions:
                failed.add(values)
            return None
        finally:
            path.pop()
            set_beam()

    try:
        solution = await dfs(initial_values, [])
    except Exception as exc:  # pragma: no cover - defensive
        lane["error"] = f"unexpected error: {exc}"
        trace(lane, lane["error"])
        _finish_lane(lane, started, "error")
        return

    if lane["status"] == "error":
        return

    if solution is not None:
        verification = verify_steps(
            config.hand,
            config.target,
            [step.to_dict() for step in solution],
            config.rules,
            require_result=True,
        )
        lane["verification"] = verification.to_dict()
        if verification.ok:
            lane["solution"] = _solution_payload(
                verification,
                source=source,
                extra={
                    **stats,
                    "model_latency_ms": None,
                },
            )
            lane["beam"] = [
                {
                    "numbers": [json_number(v) for v in verification.final_values],
                    "history": [step.to_dict()["text"] for step in solution],
                    "score": 1.0,
                }
            ]
            trace(
                lane,
                f"solved: {stats['expansions']} expansions, {stats['pruned']} pruned, "
                f"{stats['solver_checks']} solver checks",
            )
            _finish_lane(lane, started, "solved")
            return
        trace(lane, f"search produced an invalid solution: {verification.error}")

    lane["verification"] = {
        "ok": False,
        "error": f"no verified solution within {max_expansions} expansions",
        "arithmetic_errors": [],
        "steps": [],
        "final_values": [],
        "final_expression": None,
    }
    trace(
        lane,
        f"search exhausted: {stats['expansions']} expansions, {stats['pruned']} pruned, "
        f"{stats['solver_checks']} solver checks",
    )
    _finish_lane(lane, started, "unsolved")

def policy_for(config: RaceConfig) -> MovePolicy:
    if config.decisions_model == HEURISTIC_MODEL:
        return HeuristicPolicy()
    return OpenRouterPolicy(config.decisions_model)
