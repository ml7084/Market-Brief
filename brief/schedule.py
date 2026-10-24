"""Run planning and persisted state. Standard library only, so the workflow can
decide what to do before installing any dependencies.

The workflow wakes up every 15 minutes. Each wake-up asks `plan()`:
  generate - from PREPARE_MINUTES before SEND_AT (local), once per local day
  notify   - from SEND_AT, once the day's brief exists and hasn't been sent
  skip     - otherwise
so the notification lands at SEND_AT in whatever time zone BRIEF_TIMEZONE says.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
FALLBACK_TIMEZONE = "America/New_York"


@dataclass
class State:
    # UTC time the previous brief's news collection started; the next brief
    # covers everything published after it.
    last_news_cutoff: str | None = None
    # Local calendar date of the last brief that was generated.
    last_brief_date: str | None = None
    # Local calendar date of the last brief that was pushed to the phone.
    last_notified_date: str | None = None
    # Failed generation attempts on `failures_date` (local date).
    failures_date: str | None = None
    failures: int = 0


def load_state(data_dir: Path | None = None) -> State:
    data_dir = data_dir or config.DATA_DIR
    path = data_dir / STATE_FILE
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    return State(**{k: raw[k] for k in State.__dataclass_fields__ if k in raw})


def save_state(state: State, data_dir: Path | None = None) -> None:
    data_dir = data_dir or config.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / STATE_FILE).write_text(json.dumps(asdict(state), indent=2) + "\n")


def zone() -> ZoneInfo:
    try:
        return ZoneInfo(config.TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown BRIEF_TIMEZONE %r; using %s", config.TIMEZONE, FALLBACK_TIMEZONE)
        return ZoneInfo(FALLBACK_TIMEZONE)


def local_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(zone())


def _times() -> tuple[time, time]:
    send_at = time.fromisoformat(config.SEND_AT)
    prepare = datetime.combine(datetime.min, send_at) - timedelta(minutes=config.PREPARE_MINUTES)
    return prepare.time(), send_at


def failures_today(state: State, local: datetime) -> int:
    return state.failures if state.failures_date == local.date().isoformat() else 0


def plan(state: State, now: datetime | None = None, force: bool = False) -> tuple[str, str]:
    """Return (action, reason) where action is generate | notify | skip."""
    local = local_now(now)
    today = local.date().isoformat()
    prepare_at, send_at = _times()
    stamp = f"{local:%H:%M} {local.tzinfo}"
    if force:
        return "generate", "forced"
    if state.last_brief_date != today:
        if local.time() < prepare_at:
            return "skip", f"{stamp}: preparing starts at {prepare_at:%H:%M}"
        if failures_today(state, local) >= config.MAX_ATTEMPTS_PER_DAY:
            return "skip", f"{stamp}: gave up after {config.MAX_ATTEMPTS_PER_DAY} failed attempts today"
        return "generate", f"{stamp}: preparing today's brief"
    if state.last_notified_date != today:
        if local.time() < send_at:
            return "skip", f"{stamp}: brief ready, sending at {send_at:%H:%M}"
        return "notify", f"{stamp}: sending today's brief"
    return "skip", f"{stamp}: today's brief already sent"


def notify_due(state: State, now: datetime | None = None, force: bool = False) -> bool:
    local = local_now(now)
    today = local.date().isoformat()
    if state.last_brief_date != today:
        return False
    if force:  # manual run: send straight away, even if today's was already sent
        return True
    return state.last_notified_date != today and local.time() >= _times()[1]


def news_window(state: State, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(since, until) in UTC for the news this brief should cover."""
    until = now or datetime.now(timezone.utc)
    floor = until - timedelta(hours=config.MAX_LOOKBACK_HOURS)
    if state.last_news_cutoff:
        since = datetime.fromisoformat(state.last_news_cutoff) - timedelta(minutes=config.OVERLAP_MINUTES)
    else:
        since = until - timedelta(hours=config.FIRST_RUN_LOOKBACK_HOURS)
    return max(since, floor), until
