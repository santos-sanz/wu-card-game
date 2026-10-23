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

        parsed = parse_text_response(call.content)
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
    started = time.perf_counter()
    lane["started_at"] = now_iso()
    lane["status"] = "running"
    trace(lane, f"opening lane with {lane['model']}")

    target = to_fraction(config.target)
    initial_values = tuple(sorted(to_fraction(card) for card in config.hand))
    beam = [WorkItem(state_id="s0", values=initial_values, history=[], moves=[], score=0.0)]
    max_steps = len(config.hand) - 1 if config.rules.must_use_all else len(config.hand)
    lane["beam"] = [
        {"numbers": [json_number(v) for v in initial_values], "history": [], "score": 0.0}
    ]

    if _state_is_solved(initial_values, target, config.rules):
        verification = verify_steps(config.hand, config.target, [], config.rules, require_result=True)
        lane["solution"] = _solution_payload(verification, source=source)
        lane["verification"] = verification.to_dict()
        trace(lane, "target already present")
        _finish_lane(lane, started, "solved")
        return

    for depth in range(1, max_steps + 1):
        work: list[WorkItem] = []
        for index, item in enumerate(beam):
            moves = candidate_moves(item.values, config.rules)
            if not moves:
                continue
            moves = _shuffle_moves(moves, config.seed, item.values, depth)
            work.append(
                WorkItem(
                    state_id=f"s{index}",
                    values=item.values,
                    history=item.history,
                    moves=moves,
                    score=item.score,
                )
            )
        if not work:
            break

        total_moves = sum(len(item.moves) for item in work)
        trace(
            lane,
            f"depth {depth}/{max_steps}: {len(work)} beam states, {total_moves} candidate moves",
            call_expected=1 if source == "decisions" else 0,
        )

        try:
            selection = await policy.select(work, config.target, config.rules, config)
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
                        "depth": depth,
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

        successors: dict[tuple[Fraction, ...], WorkItem] = {}
        solved: list[WorkItem] = []
        for item in work:
            probabilities = selection.probabilities.get(item.state_id) or {}
            ranked = sorted(
                (
                    (key, probability)
                    for key, probability in probabilities.items()
                    if key.startswith("m") and key[1:].isdigit() and int(key[1:]) < len(item.moves)
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            if not ranked:
                ranked = [(f"m{index:02d}", 1.0 / max(1, len(item.moves))) for index in range(len(item.moves))]
            for key, probability in ranked[: config.branching]:
                move = item.moves[int(key[1:])]
                next_values = apply_move(item.values, move)
                next_history = item.history + [
                    Step(left=move.left, op=move.op, right=move.right, result=move.result)
                ]
                score = item.score + math.log(max(probability, 1e-9)) - 0.001 * heuristic_state_score(
                    next_values, target
                )
                candidate = WorkItem(
                    state_id="",
                    values=next_values,
                    history=next_history,
                    moves=[],
                    score=score,
                )
                if _state_is_solved(next_values, target, config.rules):
                    solved.append(candidate)
                    continue
                if config.rules.must_use_all and len(next_values) == 1:
                    continue
                previous = successors.get(next_values)
                if previous is None or candidate.score > previous.score:
                    successors[next_values] = candidate

        if solved:
            solved.sort(key=lambda item: item.score, reverse=True)
            best = solved[0]
            verification = verify_steps(
                config.hand,
                config.target,
                [step.to_dict() for step in best.history],
                config.rules,
                require_result=True,
            )
            lane["verification"] = verification.to_dict()
            if verification.ok:
                lane["solution"] = _solution_payload(
                    verification,
                    source=source,
                    extra={
                        "depth": depth,
                        "model_latency_ms": round(selection.latency_ms, 1),
                    },
                )
                lane["beam"] = [
                    {
                        "numbers": [json_number(v) for v in best.values],
                        "history": [step.to_dict()["text"] for step in best.history],
                        "score": round(best.score, 4),
                    }
                ]
                trace(lane, f"solved at depth {depth}")
                _finish_lane(lane, started, "solved")
                return
            trace(lane, f"solver found an invalid solution: {verification.error}")

        beam = sorted(successors.values(), key=lambda item: item.score, reverse=True)[: config.beam_width]
        lane["beam"] = [
            {
                "numbers": [json_number(v) for v in item.values],
                "history": [step.to_dict()["text"] for step in item.history],
                "score": round(item.score, 4),
            }
            for item in beam
        ]
        if not beam:
            break

    lane["verification"] = {
        "ok": False,
        "error": f"no verified solution within {max_steps} steps",
        "arithmetic_errors": [],
        "steps": [],
        "final_values": [],
        "final_expression": None,
    }
    trace(lane, "search budget exhausted without a verified solution")
    _finish_lane(lane, started, "unsolved")


def policy_for(config: RaceConfig) -> MovePolicy:
    if config.decisions_model == HEURISTIC_MODEL:
        return HeuristicPolicy()
    return OpenRouterPolicy(config.decisions_model)
