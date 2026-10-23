from __future__ import annotations

import json

from wu.dataset import build_dataset
from wu.game import Rules


def test_dataset_includes_podcast_hand() -> None:
    records = build_dataset(count=3, seed=1, target=163, rules=Rules(), solvable_only=True)
    assert records[0]["hand"] == [2, 3, 8, 9, 12, 12]
    assert records[0]["target"] == 163
    assert records[0]["solvable"] is True
    assert records[0]["solution"]


def test_dataset_records_are_json_serializable() -> None:
    records = build_dataset(count=2, seed=2, target=163, rules=Rules(), solvable_only=True)
    json.dumps(records)


def test_unsolvable_dataset_can_be_generated() -> None:
    records = build_dataset(count=2, seed=3, target=163, rules=Rules(), solvable_only=False)
    assert len(records) >= 2
    assert any(record["solvable"] for record in records)
