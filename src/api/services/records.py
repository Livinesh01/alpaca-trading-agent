"""Append-only JSONL helpers shared by API persistence services.

Backtest and evaluation reports are persisted here as immutable historical
records; nothing in this module executes trades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append one record as a JSON line; creates the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read every valid JSONL record; malformed lines are skipped silently."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records