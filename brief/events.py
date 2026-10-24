"""Corporate earnings and economic data: what was reported since the last brief
and what's scheduled next. Free, keyless sources; every fetch is best-effort."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)

NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
NASDAQ_ECONOMIC = "https://api.nasdaq.com/api/calendar/economicevents?date={day}"
FOREXFACTORY_WEEK = "https://nfs.faireconomy.media/ff_calendar_{week}.json"
NY = ZoneInfo("America/New_York")
TIMING = {"time-pre-market": "before open", "time-after-hours": "after close"}


def _text(value) -> str:
    value = re.sub(r"\s+", " ", str(value or "").replace("&nbsp;", " ")).strip()
    return "" if value.upper() in ("N/A", "NA", "--", "-") else value


def _money(value) -> float | None:
    digits = re.sub(r"[^0-9.]", "", str(value or ""))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def parse_earnings(payload: dict, day: str) -> list[dict]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for row in rows:
        cap = _money(row.get("marketCap"))
        if not cap or cap < config.EARNINGS_MIN_MARKET_CAP:
            continue
        out.append({
            "date": day,
            "symbol": _text(row.get("symbol")),
            "name": _text(row.get("name")),
            "market_cap": cap,
            "time": TIMING.get(row.get("time"), "time not given"),
            "quarter": _text(row.get("fiscalQuarterEnding")),
            "eps": _text(row.get("eps")),
            "eps_forecast": _text(row.get("epsForecast")),
            "surprise_pct": _text(row.get("surprise")),
            "last_year_eps": _text(row.get("lastYearEPS")),
        })
    out.sort(key=lambda r: r["market_cap"], reverse=True)
    return out[: config.EARNINGS_PER_DAY]


def parse_economic(payload: dict, day: str) -> list[dict]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for row in rows:
        if _text(row.get("country")) != "United States":
            continue
        out.append({
            "date": day,
            "time_gmt": _text(row.get("gmt")),
            "event": _text(row.get("eventName")),
            "actual": _text(row.get("actual")),
            "consensus": _text(row.get("consensus")),
            "previous": _text(row.get("previous")),
        })
    return out


def parse_forexfactory(payload: list, start: datetime, end: datetime) -> list[dict]:
    out = []
    for row in payload or []:
        impact, country = row.get("impact"), row.get("country")
        if country == "USD":
            if impact not in config.ECON_US_IMPACTS:
                continue
        elif not (country in config.ECON_OTHER_COUNTRIES and impact == "High"):
            continue
        try:
            when = datetime.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        if start <= when <= end:
            out.append({"when": when.isoformat(), "country": country, "event": _text(row.get("title")),
                        "impact": impact, "forecast": _text(row.get("forecast")), "previous": _text(row.get("previous"))})
    return sorted(out, key=lambda r: r["when"])


def _get_json(session: requests.Session, url: str):
    try:
        resp = session.get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("calendar fetch failed: %s: %s", url, exc)
        return None


def _days(first: date, last: date) -> list[str]:
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)
            if (first + timedelta(days=n)).weekday() < 5]


def collect(since: datetime, until: datetime) -> dict:
    """Earnings and economic data around the brief's window (dates in New York time)."""
    today = until.astimezone(NY).date()
    first = max(since.astimezone(NY).date(), today - timedelta(days=4))
    past_days = _days(first, today - timedelta(days=1))
    future_days = _days(today, today + timedelta(days=config.EARNINGS_DAYS_AHEAD))

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    })
    weeks = ["thisweek"] + (["nextweek"] if today.weekday() >= 3 else [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        earnings = dict(zip(past_days + future_days,
                            pool.map(lambda d: _get_json(session, NASDAQ_EARNINGS.format(day=d)), past_days + future_days)))
        econ_days = past_days + [today.isoformat()]
        economic = dict(zip(econ_days, pool.map(lambda d: _get_json(session, NASDAQ_ECONOMIC.format(day=d)), econ_days)))
        ff = [r for payload in pool.map(lambda w: _get_json(session, FOREXFACTORY_WEEK.format(week=w)), weeks)
              if isinstance(payload, list) for r in payload]

    reported = [r for d in past_days for r in parse_earnings(earnings.get(d), d)]
    # Today's pre-market reporters may already have numbers out by the time the brief runs.
    todays = parse_earnings(earnings.get(today.isoformat()), today.isoformat())
    reported += [r for r in todays if r["eps"]]
    upcoming = [r for r in todays if not r["eps"]]
    upcoming += [r for d in future_days if d != today.isoformat() for r in parse_earnings(earnings.get(d), d)]

    released = [r for d in econ_days for r in parse_economic(economic.get(d), d) if r["actual"]]
    horizon = until + timedelta(days=7)
    result = {
        "earnings_reported": reported,
        "earnings_upcoming": upcoming,
        "econ_released": released,
        "econ_upcoming": parse_forexfactory(ff, until, horizon),
    }
    log.info("events: %s", {k: len(v) for k, v in result.items()})
    return result
