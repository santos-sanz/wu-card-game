from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DEFAULT_HAND, DEFAULT_TARGET
from .game import Rules, generate_hand, solve


def build_dataset(
    count: int = 50,
    seed: int = 7,
    target: int = DEFAULT_TARGET,
    rules: Rules | None = None,
    solvable_only: bool = True,
) -> list[dict[str, Any]]:
    rules = rules or Rules()
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()

    podcast = tuple(sorted(DEFAULT_HAND))
    solution = solve(podcast, target, rules)
    if solution is not None:
        records.append(_record("hand-0001", list(podcast), target, rules, solution))
        seen.add(podcast)

    attempts = 0
    while len(records) < count and attempts < count * 400:
        attempts += 1
        hand = tuple(generate_hand(rng))
        if hand in seen:
            continue
        solution = solve(hand, target, rules)
        if solvable_only and solution is None:
            continue
        seen.add(hand)
        records.append(_record(f"hand-{len(records) + 1:04d}", list(hand), target, rules, solution))
    return records


def _record(
    hand_id: str,
    hand: list[int],
    target: int,
    rules: Rules,
    solution,
) -> dict[str, Any]:
    return {
        "id": hand_id,
        "hand": sorted(hand),
        "target": target,
        "rules": rules.to_dict(),
        "solvable": solution is not None,
        "steps": len(solution) if solution else None,
        "solution": [step.to_dict() for step in solution] if solution else None,
    }


def save_dataset(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or DATA_DIR / "hands.jsonl"
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
