"""Price snapshot: Yahoo Finance charts, CoinGecko for crypto, and the official
US Treasury par yield curve. Every source is best-effort; the brief notes gaps."""

from __future__ import annotations

import csv
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from . import config

log = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
TREASURY_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)


@dataclass
class Quote:
    symbol: str
    label: str
    group: str
    kind: str  # price | yield
    last: float
    change_1d: float | None  # % for prices, basis points for yields
    change_1w: float | None
    change_1m: float | None
    as_of: str  # ISO timestamp (UTC) of the last price


def _change(kind: str, last: float, ref: float | None) -> float | None:
    if ref is None or ref == 0:
        return None
    if kind == "yield":
        return round((last - ref) * 100, 1)
    return round((last / ref - 1) * 100, 2)


def quote_from_chart(payload: dict, symbol: str, label: str, group: str, kind: str) -> Quote | None:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    r = result[0]
    meta = r.get("meta", {})
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    bars = [(ts, c) for ts, c in zip(r.get("timestamp") or [], closes) if c is not None]
    last = meta.get("regularMarketPrice") or (bars[-1][1] if bars else None)
    if last is None:
        return None
    market_time = meta.get("regularMarketTime") or (bars[-1][0] if bars else 0)
    scale = 0.1 if kind == "yield" and last > 20 else 1.0  # guard against x10-quoted yield indices
    last *= scale
    # The final daily bar is "today" (possibly partial); compare against the bars before it.
    prior = [c * scale for _, c in bars]
    if bars and _day(bars[-1][0]) == _day(market_time):
        prior = prior[:-1]

    def ref(n: int) -> float | None:
        return prior[-n] if len(prior) >= n else None

    return Quote(
        symbol, label, group, kind, round(last, 4),
        _change(kind, last, ref(1)), _change(kind, last, ref(5)), _change(kind, last, ref(21)),
        datetime.fromtimestamp(market_time, tz=timezone.utc).isoformat(timespec="minutes"),
    )


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _yahoo(session: requests.Session, symbol: str, label: str, group: str, kind: str) -> Quote | None:
    try:
        resp = session.get(YAHOO_CHART.format(symbol=quote(symbol)), timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return quote_from_chart(resp.json(), symbol, label, group, kind)
    except Exception as exc:
        log.warning("quote failed: %s: %s", symbol, exc)
        return None


def _coingecko(session: requests.Session) -> list[Quote]:
    headers = {}
    if key := os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = key
    params = {
        "vs_currency": "usd",
        "ids": ",".join(cid for cid, _, _ in config.CRYPTO),
        "price_change_percentage": "24h,7d,30d",
    }
    resp = session.get(COINGECKO_MARKETS, params=params, headers=headers, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    labels = {cid: label for cid, label, _ in config.CRYPTO}
    quotes = []
    for coin in resp.json():
        def pct(field: str) -> float | None:
            value = coin.get(field)
            return None if value is None else round(value, 2)

        quotes.append(Quote(
            coin["symbol"].upper(), labels.get(coin["id"], coin["name"]), "crypto", "price",
            coin["current_price"],
            pct("price_change_percentage_24h_in_currency"),
            pct("price_change_percentage_7d_in_currency"),
            pct("price_change_percentage_30d_in_currency"),
            coin.get("last_updated", "")[:16] + "+00:00" if coin.get("last_updated") else "",
        ))
    order = {label: n for n, (_, label, _) in enumerate(config.CRYPTO)}
    return sorted(quotes, key=lambda q: order.get(q.label, 99))


def parse_treasury_csv(text: str) -> dict | None:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    latest, previous = rows[0], rows[1] if len(rows) > 1 else None
    tenors = ["3 Mo", "6 Mo", "1 Yr", "2 Yr", "5 Yr", "10 Yr", "30 Yr"]

    def num(row, tenor):
        try:
            return float(row[tenor])
        except (KeyError, TypeError, ValueError):
            return None

    curve = {t: num(latest, t) for t in tenors}
    prev = {t: num(previous, t) for t in tenors} if previous else {}
    out = {
        "date": latest.get("Date"),
        "curve": curve,
        "change_bp": {t: round((curve[t] - prev[t]) * 100, 1) for t in tenors if curve.get(t) is not None and prev.get(t) is not None},
    }
    if curve.get("10 Yr") is not None and curve.get("2 Yr") is not None:
        out["spread_2s10s_bp"] = round((curve["10 Yr"] - curve["2 Yr"]) * 100, 1)
    if curve.get("10 Yr") is not None and curve.get("3 Mo") is not None:
        out["spread_3m10y_bp"] = round((curve["10 Yr"] - curve["3 Mo"]) * 100, 1)
    return out


def _treasury(session: requests.Session) -> dict | None:
    year = datetime.now(timezone.utc).year
    try:
        resp = session.get(TREASURY_CSV.format(year=year), timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return parse_treasury_csv(resp.text)
    except Exception as exc:
        log.warning("treasury curve failed: %s", exc)
        return None


def snapshot() -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    with ThreadPoolExecutor(max_workers=8) as pool:
        quotes = list(pool.map(lambda i: _yahoo(session, i.symbol, i.label, i.group, i.kind), config.INSTRUMENTS))
        curve_future = pool.submit(_treasury, session)
        try:
            crypto = _coingecko(session)
        except Exception as exc:
            log.warning("coingecko failed (%s); falling back to Yahoo", exc)
            fallback = [(sym, label) for _, label, sym in config.CRYPTO if sym]
            crypto = list(pool.map(lambda s: _yahoo(session, s[0], s[1], "crypto", "price"), fallback))
        curve = curve_future.result()

    all_quotes = [q for q in quotes + crypto if q]
    missing = [i.label for i, q in zip(config.INSTRUMENTS, quotes) if q is None]
    log.info("markets: %d quotes, %d missing", len(all_quotes), len(missing))
    return {
        "quotes": [asdict(q) for q in all_quotes],
        "treasury_curve": curve,
        "missing": missing,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }
