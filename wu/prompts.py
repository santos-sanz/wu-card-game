from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Sequence

from .game import Move, Rules, fmt, json_number


def rules_text(rules: Rules) -> str:
    parts = [
        "Use the operators +, -, * and / only.",
        "A step takes two numbers that are currently available and replaces them with the result.",
    ]
    if rules.must_use_all:
        parts.append("Use every card exactly once; the last remaining number must equal the target.")
    if rules.allow_reuse:
        parts.append("Operands are not consumed and may be reused.")
    if rules.integer_only:
        parts.append("Every intermediate result must be an integer; division must be exact.")
    return " ".join(parts)


def build_text_messages(
    hand: Sequence[int],
    target: int,
    rules: Rules,
    feedback: str | None = None,
) -> list[dict[str, str]]:
    system = (
        "You solve a Countdown-style arithmetic card game. "
        "You must answer with a single JSON object and nothing else. "
        "No markdown, no code fences, no explanation."
    )
    user = f"""Cards: {list(hand)}
Target: {target}

Rules:
- {rules_text(rules)}

Return exactly this JSON shape:
{{"steps": [{{"left": <number>, "op": "<+|-|*|/>", "right": <number>, "result": <number>}}]}}

Each step consumes the two numbers `left` and `right` from the currently available numbers and introduces `result`.
The list of steps must finish with exactly one available number equal to the target.
Do not invent numbers: every `left` and `right` must be a card or a result produced by an earlier step.
Compute every result correctly; it is checked by a verifier."""
    if feedback:
        user += f"\n\nYour previous answer was rejected: {feedback}\nReturn a corrected JSON object."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


@dataclass
class ParsedText:
    steps: list[dict[str, Any]] | None
    error: str | None
    payload: dict[str, Any] | None
    raw_content: str


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_text_response(content: str) -> ParsedText:
    if not content or not content.strip():
        return ParsedText(None, "empty response", None, content)
    payload = _extract_json_object(content)
    if payload is None:
        return ParsedText(None, "response is not valid JSON", None, content)
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return ParsedText(None, "response must contain a `steps` array", payload, content)
    if not steps:
        return ParsedText(None, "`steps` is empty", payload, content)
    cleaned: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return ParsedText(None, f"step {index} is not an object", payload, content)
        missing = [key for key in ("left", "op", "right", "result") if key not in step]
        if missing:
            return ParsedText(None, f"step {index} is missing {', '.join(missing)}", payload, content)
        if step["op"] not in ("+", "-", "*", "/"):
            return ParsedText(None, f"step {index} has invalid operator {step['op']!r}", payload, content)
        cleaned.append(step)
    return ParsedText(cleaned, None, payload, content)


def build_decisions_request(
    model: str,
    work: Sequence[tuple[str, tuple[Fraction, ...], list[dict], list[Move]]],
    target: int,
    rules: Rules,
    history_by_state: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build one Decisions API request for every state in the beam.

    `work` items are (state_id, values, moves, history). Each state gets one
    choice question whose criteria are all legal moves.
    """
    history_by_state = history_by_state or {}
    states = []
    questions: dict[str, Any] = {}
    for state_id, values, moves, history in work:
        states.append(
            {
                "id": state_id,
                "numbers": [json_number(v) for v in values],
                "steps_so_far": history,
                "step_number": len(history) + 1,
                "steps_remaining": len(values) - 1 if rules.must_use_all else None,
            }
        )
        criteria = {f"m{index:02d}": move.text(values) for index, move in enumerate(moves)}
        questions[f"{state_id}_move"] = {
            "type": "choice",
            "instructions": (
                "Choose the single best next arithmetic move to reach the target. "
                "You must use every card exactly once, all intermediate results must be integers, "
                "and the final remaining number must equal the target. "
                "Prefer moves that keep the remaining numbers able to reach the target."
            ),
            "criteria": criteria,
        }
    return {
        "model": model,
        "state": {
            "game": "Countdown-style arithmetic card game",
            "target": target,
            "rules": {
                "operators": ["+", "-", "*", "/"],
                "must_use_all": rules.must_use_all,
                "allow_reuse": rules.allow_reuse,
                "integer_only_intermediates": rules.integer_only,
            },
            "rules_text": rules_text(rules),
            "states": states,
        },
        "questions": questions,
    }
