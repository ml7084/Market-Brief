"""Run gating and persisted state. Standard library only, so the workflow can
decide to skip a run before installing any dependencies."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

STATE_FILE = "state.json"


@dataclass
class State:
    # UTC time the previous brief's news collection started; the next brief
    # covers everything published after it.
    last_news_cutoff: str | None = None
    # Local calendar date of the last brief that was sent.
    last_brief_date: str | None = None


def load_state(data_dir: Path | None = None) -> State:
    data_dir = data_dir or config.DATA_DIR
    path = data_dir / STATE_FILE
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    return State(**{k: raw.get(k) for k in State.__dataclass_fields__})


def save_state(state: State, data_dir: Path | None = None) -> None:
    data_dir = data_dir or config.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / STATE_FILE).write_text(json.dumps(asdict(state), indent=2) + "\n")


def local_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(config.TIMEZONE))


def should_run(state: State, now: datetime | None = None, force: bool = False) -> tuple[bool, str]:
    """Return (run?, reason)."""
    if force:
        return True, "forced"
    local = local_now(now)
    send_after = time.fromisoformat(config.SEND_AFTER)
    if local.time() < send_after:
        return False, f"{local:%H:%M} {config.TIMEZONE} is before send time {config.SEND_AFTER}"
    if state.last_brief_date == local.date().isoformat():
        return False, f"brief for {local.date()} already sent"
    return True, "due"


def news_window(state: State, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(since, until) in UTC for the news this brief should cover."""
    until = now or datetime.now(timezone.utc)
    floor = until - timedelta(hours=config.MAX_LOOKBACK_HOURS)
    if state.last_news_cutoff:
        since = datetime.fromisoformat(state.last_news_cutoff) - timedelta(minutes=config.OVERLAP_MINUTES)
    else:
        since = until - timedelta(hours=config.FIRST_RUN_LOOKBACK_HOURS)
    return max(since, floor), until
