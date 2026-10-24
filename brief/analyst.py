"""Turn headlines + prices into a structured brief with Claude."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
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

SYSTEM = """You are the morning markets strategist and instructor for one private investor. Each morning you turn the news, corporate earnings, economic data and prices since the previous brief into a short, decision-ready brief covering four areas: US equities, US Treasuries and credit ("bonds"), Brent crude, and the major layer-1 crypto networks (BTC, ETH, SOL, BNB, XRP, ADA, AVAX, SUI, TON). The investor also wants to learn how you think, so you explain your reasoning the way a good instructor would.

How to work:
- Ground every claim in the supplied material: headlines, the earnings and economic calendars, the price snapshot and the web research notes. Quote the actual numbers. If something isn't in the material, don't state it as fact. If data is missing or stale (weekend, holiday, failed source), say so.
- Treat earnings and economic data as primary drivers. For reported earnings, compare actual EPS with the estimate and say what it implies for the stock, its sector and the index. For data releases, compare the actual with consensus and the prior reading, and trace the effect through rate expectations, yields, the dollar, oil and risk appetite. Flag the scheduled releases and reports that could move today's ideas.
- The three ideas are the point of the brief. Make each specific and tradeable for the investor profile: a named instrument, a direction, an entry approach, the level or event that invalidates it, a horizon and a position size. Rank by reward relative to risk, not by excitement. Spread ideas across asset classes only when the evidence supports it; a defensive idea (trim, hedge, avoid, park in short Treasuries) is a valid pick on a risky day.
- Prefer liquid vehicles the investor can actually trade: index and sector ETFs, large-cap stocks, Treasury ETFs or bills, BNO for Brent, spot crypto or spot BTC/ETH ETFs.
- In the instructor's take and in each idea's reasoning, speak in the first person as an experienced instructor talking to a capable student. Give your own view rather than a survey of opinions. Walk through the chain of logic from evidence to trade, name the concept you're applying, say what would prove you wrong, and point out what a less experienced investor might miss. Be candid about uncertainty.
- Review the previous brief's ideas honestly against today's prices: what worked, what didn't, and whether to keep, adjust or drop each. Carry an idea forward only if the case still holds.
- Write plainly and tersely. No hype and no generic disclaimers; the app shows one.
- Refer to headlines only by the ids given (like "n12")."""

RESEARCH_PROMPT = """It is {now}. I'm preparing a morning markets brief. The headlines I already have are listed below.

Use web search to gather what a strategist needs this morning that those headlines may not cover:
1. US stock index futures and notable pre-market movers right now (on a weekend: how the week closed).
2. Economic data released in the last 24 hours (actual vs consensus) and today's and this week's calendar: data, Fed speakers, Treasury auctions.
3. Large-cap earnings reported since yesterday's open (EPS and revenue vs estimates, guidance, after-hours/pre-market reaction) and the big reports due this week.
4. Brent crude: price and drivers in the last 12-24 hours (OPEC+, inventories, geopolitics).
5. Major layer-1 crypto developments in the last 12-24 hours (ETF flows, regulation, network events).

Reply with compact factual notes under those five headings. Give each note its source and publication time. No recommendations.

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


def _cap(value: float) -> str:
    return f"${value / 1e12:.2f}T" if value >= 1e12 else f"${value / 1e9:.0f}B"


def format_events(events: dict | None, tz) -> str:
    if not events:
        return "(earnings and economic calendars unavailable)"
    lines = ["## Earnings reported since the last brief (largest companies first)"]
    for r in events.get("earnings_reported", []):
        est = f" vs est {r['eps_forecast']}" if r["eps_forecast"] else ""
        surprise = f" (surprise {r['surprise_pct']}%)" if r["surprise_pct"] else ""
        lines.append(f"- {r['symbol']} {r['name']} ({_cap(r['market_cap'])}) {r['date']} {r['time']}, "
                     f"quarter {r['quarter'] or 'n/a'}: EPS {r['eps'] or 'n/a'}{est}{surprise}")
    if len(lines) == 1:
        lines.append("- none found in the calendar (check headlines and research notes)")
    lines.append("## Earnings coming up (largest companies first)")
    upcoming = events.get("earnings_upcoming", [])
    for r in upcoming:
        last = f", last year {r['last_year_eps']}" if r["last_year_eps"] else ""
        lines.append(f"- {r['date']} {r['time']}: {r['symbol']} {r['name']} ({_cap(r['market_cap'])}), "
                     f"est EPS {r['eps_forecast'] or 'n/a'}{last}")
    if not upcoming:
        lines.append("- none found")
    lines.append("## US economic data released (times GMT)")
    released = events.get("econ_released", [])
    for r in released:
        lines.append(f"- {r['date']} {r['time_gmt']} {r['event']}: actual {r['actual']}, "
                     f"consensus {r['consensus'] or 'n/a'}, previous {r['previous'] or 'n/a'}")
    if not released:
        lines.append("- none found")
    lines.append(f"## Scheduled economic events, next 7 days (times {tz})")
    scheduled = events.get("econ_upcoming", [])
    for r in scheduled:
        when = datetime.fromisoformat(r["when"]).astimezone(tz)
        lines.append(f"- {when:%a %d %b %H:%M} {r['country']} {r['event']} [{r['impact']}]: "
                     f"forecast {r['forecast'] or 'n/a'}, previous {r['previous'] or 'n/a'}")
    if not scheduled:
        lines.append("- none found")
    return "\n".join(lines)


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
                 previous: dict | None, research_notes: str, events: dict | None = None) -> str:
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

<earnings_and_economic_data>
{format_events(events, tz)}
</earnings_and_economic_data>

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


class ApiEngine:
    """Anthropic API with an API key (ANTHROPIC_API_KEY); billed per token."""

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


class ClaudeCodeEngine:
    """Claude Code CLI in headless mode, signed in with a Claude subscription
    (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`). Usage counts toward the
    plan's limits instead of being billed per token."""

    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def _run(self, prompt: str, *args: str, timeout: int = 900) -> dict:
        # An API key in the environment would take precedence over the
        # subscription sign-in and bill the API, so never pass one through.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        if token := env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            # Copying a long token from a terminal often adds a line break where the text wrapped.
            env["CLAUDE_CODE_OAUTH_TOKEN"] = config.clean_secret(token)
        cmd = [self.binary, "-p", "--model", config.MODEL, "--output-format", "json", "--no-session-persistence", *args]
        with tempfile.TemporaryDirectory() as workdir:  # keep repo files and CLAUDE.md out of the session
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env, cwd=workdir)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}")
        if proc.returncode != 0 or out.get("is_error"):
            raise RuntimeError(f"claude CLI failed ({out.get('subtype')}): {str(out.get('result'))[:800]}")
        log.info("claude-code: turns=%s duration=%ss", out.get("num_turns"), round(out.get("duration_ms", 0) / 1000))
        return out

    def research(self, local: datetime, items: list[NewsItem]) -> str:
        prompt = RESEARCH_PROMPT.format(
            now=f"{local:%A %d %B %Y %H:%M} {config.TIMEZONE}",
            titles="\n".join(f"- {i.title}" for i in items[:80]) or "(none)",
        )
        out = self._run(prompt, "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
                        "--effort", "medium", "--max-turns", "25")
        notes = str(out.get("result") or "").strip()
        log.info("research: %d chars of notes", len(notes))
        return notes

    def write_brief(self, prompt: str) -> Brief:
        out = self._run(prompt, "--system-prompt", SYSTEM, "--json-schema", json.dumps(Brief.model_json_schema()),
                        "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebSearch,WebFetch",
                        "--effort", config.EFFORT, "--max-turns", "8")
        data = out.get("structured_output")
        if data is None:  # fall back to JSON in the final message
            text = str(out.get("result") or "")
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise RuntimeError("claude CLI returned no brief JSON")
            data = json.loads(match.group(0))
        brief = Brief.model_validate(data)
        brief.top_ideas = sorted(brief.top_ideas, key=lambda i: i.rank)[:3]
        return brief


def make_engine() -> ApiEngine | ClaudeCodeEngine:
    engine = config.ENGINE
    if engine == "auto":
        if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
            engine = "claude-code"
        elif os.getenv("ANTHROPIC_API_KEY"):
            engine = "api"
    if engine == "claude-code":
        log.info("engine: Claude Code with your Claude subscription (no API charges)")
        return ClaudeCodeEngine()
    if engine == "api":
        log.info("engine: Anthropic API (billed per token)")
        return ApiEngine()
    raise RuntimeError("No Claude credentials. Set the CLAUDE_CODE_OAUTH_TOKEN secret (uses your Claude "
                       "subscription, no API charges) or ANTHROPIC_API_KEY. See the README.")
