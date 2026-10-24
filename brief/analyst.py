"""Turn headlines + prices into a structured brief with Claude."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from . import config
from .models import Brief
from .news import NewsItem

log = logging.getLogger(__name__)

# Server-side refusal fallback: a declined request is retried on Anthropic's
# recommended model for that refusal category instead of failing the brief.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You are the morning markets strategist for one private investor. Each morning you turn the news flow since the previous brief and a price snapshot into a short, decision-ready brief covering four areas: US equities, US Treasuries and credit ("bonds"), Brent crude, and the major layer-1 crypto networks (BTC, ETH, SOL, BNB, XRP, ADA, AVAX, SUI, TON).

How to work:
- Ground every claim in the supplied headlines, price snapshot or web research notes, and quote the actual numbers. If something isn't in the material, don't state it as fact. If data is missing or stale (weekend, holiday, failed source), say so.
- Separate signal from noise. Lead with what moves prices: the Fed and rate expectations, inflation and labour data, earnings from index heavyweights, OPEC+ and geopolitics, crypto ETF flows and regulation.
- The three ideas are the point of the brief. Make each specific and tradeable for the investor profile: a named instrument, a direction, an entry approach, the level or event that invalidates it, a horizon and a position size. Rank by reward relative to risk, not by excitement. Spread ideas across asset classes only when the evidence supports it; a defensive idea (trim, hedge, avoid, park in short Treasuries) is a valid pick on a risky day.
- Prefer liquid vehicles the investor can actually trade: index and sector ETFs, large-cap stocks, Treasury ETFs or bills, BNO for Brent, spot crypto or spot BTC/ETH ETFs.
- Review the previous brief's ideas honestly against today's prices: what worked, what didn't, and whether to keep, adjust or drop each. Carry an idea forward only if the case still holds.
- Write plainly and tersely for an experienced reader. No hype and no generic disclaimers; the app shows one.
- Refer to headlines only by the ids given (like "n12")."""

RESEARCH_PROMPT = """It is {now}. I'm preparing a morning markets brief. The headlines I already have are listed below.

Use web search to gather what a strategist needs this morning that those headlines may not cover:
1. US stock index futures and notable pre-market movers right now (on a weekend: how the week closed).
2. Today's and this week's US economic calendar, Fed speakers, Treasury auctions and major earnings.
3. Brent crude: price and drivers in the last 12-24 hours (OPEC+, inventories, geopolitics).
4. Major layer-1 crypto developments in the last 12-24 hours (ETF flows, regulation, network events).

Reply with compact factual notes under those four headings. Give each note its source and publication time. No recommendations.

<headlines>
{titles}
</headlines>"""


def _fmt_change(kind: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}bp" if kind == "yield" else f"{value:+.2f}%"


def format_snapshot(market: dict) -> str:
    groups = {"equities": "US equities", "rates": "Rates & credit", "oil": "Oil, gold & dollar", "crypto": "Crypto (L1)"}
    lines = []
    for group, title in groups.items():
        rows = [q for q in market.get("quotes", []) if q["group"] == group]
        if not rows:
            continue
        lines.append(f"## {title}")
        for q in rows:
            last = f"{q['last']:.3f}%" if q["kind"] == "yield" else f"{q['last']:,.4g}" if q["last"] < 1 else f"{q['last']:,.2f}"
            changes = " | ".join(f"{k} {_fmt_change(q['kind'], q[f'change_{k.lower()}'])}" for k in ("1D", "1W", "1M"))
            lines.append(f"- {q['label']} [{q['symbol']}]: {last} | {changes} | as of {q['as_of']}")
    if market.get("missing"):
        lines.append(f"Unavailable this morning: {', '.join(market['missing'])}")
    curve = market.get("treasury_curve")
    if curve:
        points = ", ".join(f"{t} {v:.2f}%" for t, v in curve["curve"].items() if v is not None)
        moves = ", ".join(f"{t} {v:+.1f}bp" for t, v in curve.get("change_bp", {}).items())
        lines.append(f"## Official US Treasury par curve ({curve['date']})\n{points}\nDay change: {moves or 'n/a'}")
        if "spread_2s10s_bp" in curve:
            lines.append(f"2s10s {curve['spread_2s10s_bp']:+.0f}bp, 3m10y {curve.get('spread_3m10y_bp', 0):+.0f}bp")
    return "\n".join(lines)


def format_headlines(items: list[NewsItem], tz: ZoneInfo) -> str:
    out = []
    for i in items:
        stamp = i.published.astimezone(tz).strftime("%m-%d %H:%M")
        line = f"[{i.id}] {stamp} · {i.category} · {i.source} — {i.title}"
        if i.summary and i.summary.lower() not in i.title.lower():
            line += f". {i.summary}"
        out.append(line)
    return "\n".join(out) or "(no headlines collected)"


def format_previous(previous: dict | None) -> str:
    if not previous:
        return "(none — this is the first brief)"
    b = previous["brief"]
    ideas = "\n".join(
        f"{x['rank']}. {x['direction'].upper()} {x['instrument']} — {x['title']}. Entry: {x['entry']}. "
        f"Invalidation: {x['invalidation']}. Horizon: {x['horizon']}."
        for x in b["top_ideas"]
    )
    return f"Date: {previous['date']}\nHeadline: {b['notification_title']}\nIdeas:\n{ideas}"


def us_session_status(local: datetime) -> str:
    ny = local.astimezone(ZoneInfo("America/New_York"))
    if ny.weekday() >= 5:
        return "closed for the weekend; next regular session Monday 09:30 ET"
    minutes = ny.hour * 60 + ny.minute
    if minutes < 9 * 60 + 30:
        return f"pre-market ({ny:%H:%M} ET); regular session opens 09:30 ET"
    if minutes < 16 * 60:
        return f"regular session open ({ny:%H:%M} ET)"
    return f"after the close ({ny:%H:%M} ET)"


def build_prompt(local: datetime, since: datetime, items: list[NewsItem], market: dict,
                 previous: dict | None, research_notes: str) -> str:
    tz = local.tzinfo
    hours = (local - since).total_seconds() / 3600
    return f"""<context>
Brief for: {local:%A %d %B %Y, %H:%M} ({config.TIMEZONE})
News window: since {since.astimezone(tz):%a %d %b %H:%M} ({hours:.0f} hours)
US cash equity market: {us_session_status(local)} (exchange holidays not checked)
Investor profile: {config.INVESTOR_PROFILE}
</context>

<price_snapshot fetched_at="{market.get('fetched_at', '')}">
{format_snapshot(market)}
</price_snapshot>

<previous_brief>
{format_previous(previous)}
</previous_brief>

<web_research>
{research_notes or '(not available)'}
</web_research>

<headlines count="{len(items)}" times="{config.TIMEZONE}">
{format_headlines(items, tz)}
</headlines>

Write today's brief."""


class Analyst:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(max_retries=3)

    def research(self, local: datetime, items: list[NewsItem]) -> str:
        """Optional web-search pass for things RSS misses (futures, calendar)."""
        prompt = RESEARCH_PROMPT.format(
            now=f"{local:%A %d %B %Y %H:%M} {config.TIMEZONE}",
            titles="\n".join(f"- {i.title}" for i in items[:80]) or "(none)",
        )
        messages: list[dict] = [{"role": "user", "content": prompt}]
        content: list = []
        for _ in range(4):  # resume server-side pause_turn a few times at most
            with self.client.beta.messages.stream(
                model=config.MODEL,
                max_tokens=16000,
                output_config={"effort": "medium"},
                tools=[{"type": "web_search_20260209", "name": "web_search",
                        "max_uses": config.WEB_RESEARCH_MAX_SEARCHES}],
                messages=messages,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                msg = stream.get_final_message()
            content += msg.content
            if msg.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": content}]
        if msg.stop_reason == "refusal":
            log.warning("research pass declined; continuing without it")
            return ""
        notes = "".join(b.text for b in content if b.type == "text").strip()
        log.info("research: %d chars of notes", len(notes))
        return notes

    def write_brief(self, prompt: str) -> Brief:
        with self.client.beta.messages.stream(
            model=config.MODEL,
            max_tokens=32000,
            system=SYSTEM,
            output_config={"effort": config.EFFORT},
            output_format=Brief,
            messages=[{"role": "user", "content": prompt}],
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        log.info("brief: model=%s stop=%s usage=%s", msg.model, msg.stop_reason, msg.usage.model_dump(exclude_none=True))
        if msg.stop_reason == "refusal":
            raise RuntimeError(f"model declined to write the brief: {msg.stop_details}")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("brief was cut off at max_tokens")
        brief = msg.parsed_output
        if brief is None:
            text = "".join(b.text for b in msg.content if b.type == "text")
            brief = Brief.model_validate_json(text)
        brief.top_ideas = sorted(brief.top_ideas, key=lambda i: i.rank)[:3]
        return brief
