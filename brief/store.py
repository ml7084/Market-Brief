"""Brief records on disk: data/briefs/<date>.json, data/latest.json, data/index.json."""

from __future__ import annotations

import json
from pathlib import Path

from . import config


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")


def latest(data_dir: Path | None = None) -> dict | None:
    data_dir = data_dir or config.DATA_DIR
    path = data_dir / "latest.json"
    return json.loads(path.read_text()) if path.exists() else None


def latest_before(date: str, data_dir: Path | None = None) -> dict | None:
    """Most recent brief dated strictly before `date` (YYYY-MM-DD)."""
    data_dir = data_dir or config.DATA_DIR
    earlier = sorted(p for p in (data_dir / "briefs").glob("*.json") if p.stem < date)
    return json.loads(earlier[-1].read_text()) if earlier else None


def save(record: dict, data_dir: Path | None = None) -> None:
    data_dir = data_dir or config.DATA_DIR
    _write(data_dir / "briefs" / f"{record['date']}.json", record)
    _write(data_dir / "latest.json", record)
    index_path = data_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    index = [e for e in index if e["date"] != record["date"]]
    index.append({"date": record["date"], "title": record["brief"]["notification_title"]})
    _write(index_path, sorted(index, key=lambda e: e["date"], reverse=True))
