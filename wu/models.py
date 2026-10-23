from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DEFAULT_DECISIONS_MODEL, DEFAULT_HAND, DEFAULT_TARGET, DEFAULT_TEXT_MODEL
from .game import Rules


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RaceConfig:
    hand: list[int]
    target: int = DEFAULT_TARGET
    rules: Rules = field(default_factory=Rules)
    text_model: str = DEFAULT_TEXT_MODEL
    decisions_model: str = DEFAULT_DECISIONS_MODEL
    text_max_attempts: int = 1
    beam_width: int = 4
    branching: int = 2
    include_baseline: bool = False
    seed: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RaceConfig":
        hand = data.get("hand", DEFAULT_HAND)
        if not isinstance(hand, list) or not (2 <= len(hand) <= 6):
            raise ValueError("hand must be a list of 2 to 6 card values")
        try:
            cards = [int(card) for card in hand]
        except (TypeError, ValueError) as exc:
            raise ValueError("hand values must be integers") from exc
        if any(card <= 0 for card in cards):
            raise ValueError("hand values must be positive")
        target = int(data.get("target", DEFAULT_TARGET))
        if target <= 0:
            raise ValueError("target must be positive")
        attempts = int(data.get("text_max_attempts", 1))
        if not (1 <= attempts <= 5):
            raise ValueError("text_max_attempts must be between 1 and 5")
        beam_width = int(data.get("beam_width", 4))
        if not (1 <= beam_width <= 8):
            raise ValueError("beam_width must be between 1 and 8")
        branching = int(data.get("branching", 2))
        if not (1 <= branching <= 5):
            raise ValueError("branching must be between 1 and 5")
        return cls(
            hand=sorted(cards),
            target=target,
            rules=Rules.from_dict(data.get("rules")),
            text_model=str(data.get("text_model") or DEFAULT_TEXT_MODEL),
            decisions_model=str(data.get("decisions_model") or DEFAULT_DECISIONS_MODEL),
            text_max_attempts=attempts,
            beam_width=beam_width,
            branching=branching,
            include_baseline=bool(data.get("include_baseline", False)),
            seed=int(data.get("seed", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hand": self.hand,
            "target": self.target,
            "rules": self.rules.to_dict(),
            "text_model": self.text_model,
            "decisions_model": self.decisions_model,
            "text_max_attempts": self.text_max_attempts,
            "beam_width": self.beam_width,
            "branching": self.branching,
            "include_baseline": self.include_baseline,
            "seed": self.seed,
        }


def new_lane(kind: str, model: str, provider: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "model": model,
        "provider": provider,
        "status": "waiting",
        "started_at": None,
        "finished_at": None,
        "elapsed_ms": None,
        "calls": 0,
        "tokens": {"prompt": 0, "completion": 0, "total": 0},
        "cost": 0.0,
        "solution": None,
        "verification": None,
        "error": None,
        "attempts": [],
        "trace": [],
        "raw": [],
        "beam": [],
    }
