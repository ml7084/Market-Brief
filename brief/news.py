"""Collect headlines from RSS feeds published inside the brief's time window."""

from __future__ import annotations

import calendar
import html
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

from . import config

log = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    category: str
    published: datetime  # UTC
    summary: str = ""
    id: str = ""  # short reference ("n12") assigned after ranking, cited by the model


def google_news_url(query: str, days: int) -> str:
    q = quote_plus(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _clean(text: str, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def parse_feed(content: bytes | str, source: str, category: str) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    google = "news.google.com" in (parsed.feed.get("link") or "") or source.startswith("Google News")
    items = []
    for entry in parsed.entries:
        published = _published(entry)
        title = _clean(entry.get("title", ""), 220)
        if not published or not title:
            continue
        item_source = source
        summary = _clean(entry.get("summary", ""), 300)
        if google:
            # Google News titles look like "Headline - Publisher"; summaries just repeat them.
            src = entry.get("source", {}).get("title")
            if src and title.endswith(f" - {src}"):
                title = title[: -len(src) - 3]
            item_source = src or "Google News"
            summary = ""
        items.append(NewsItem(title, entry.get("link", ""), item_source, category, published, summary))
    return items


def _fetch(session: requests.Session, url: str, source: str, category: str) -> list[NewsItem]:
    try:
        resp = session.get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return parse_feed(resp.content, source, category)
    except Exception as exc:  # one dead feed must never sink the brief
        log.warning("feed failed: %s (%s): %s", source, url, exc)
        return []


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:90]


def select(items: list[NewsItem], since: datetime, until: datetime) -> list[NewsItem]:
    """Keep in-window items, drop duplicates, cap per category, newest first."""
    # The same story often arrives from a direct feed and from Google News;
    # keep whichever copy carries a summary.
    best: dict[str, NewsItem] = {}
    for item in items:
        if not (since <= item.published <= until):
            continue
        key = _title_key(item.title)
        if key not in best or (item.summary and not best[key].summary):
            best[key] = item

    seen_urls: set[str] = set()
    per_category: dict[str, int] = {}
    kept: list[NewsItem] = []
    for item in sorted(best.values(), key=lambda i: i.published, reverse=True):
        if item.url and item.url in seen_urls:
            continue
        if per_category.get(item.category, 0) >= config.MAX_ITEMS_PER_CATEGORY:
            continue
        seen_urls.add(item.url)
        per_category[item.category] = per_category.get(item.category, 0) + 1
        kept.append(item)
        if len(kept) >= config.MAX_ITEMS_TOTAL:
            break
    for n, item in enumerate(kept, 1):
        item.id = f"n{n}"
    return kept


def collect(since: datetime, until: datetime) -> tuple[list[NewsItem], dict[str, int]]:
    """Return (headlines, per-source item counts) for the window."""
    days = max(1, math.ceil((until - since).total_seconds() / 86400))
    jobs = [(f.url, f.name, f.category) for f in config.FEEDS]
    for category, queries in config.GOOGLE_NEWS_QUERIES.items():
        jobs += [(google_news_url(q, days), f"Google News: {q}", category) for q in queries]

    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: _fetch(session, *job), jobs))

    health = {source: len(found) for (_, source, _), found in zip(jobs, results)}
    items = select([i for found in results for i in found], since, until)
    log.info("news: %d items in window from %d/%d live sources", len(items), sum(1 for c in health.values() if c), len(jobs))
    return items, health
