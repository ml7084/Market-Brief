"""Settings, news sources and tracked instruments.

Everything that changes behaviour without code edits comes from environment
variables (GitHub repo Variables / Secrets in production, a shell locally).
Edit the lists below to add or remove feeds and instruments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BRIEF_DATA_DIR", ROOT / "data"))
WEB_DIR = ROOT / "web"


def _env(name: str, default: str) -> str:
    # GitHub passes unset repo variables through as empty strings.
    return os.getenv(name) or default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Scheduling --------------------------------------------------------------
# The phone keeps BRIEF_TIMEZONE in sync with wherever it is (see web/shared.js),
# so the notification lands at SEND_AT local time in whatever time zone you're in.
TIMEZONE = _env("BRIEF_TIMEZONE", "America/New_York")
SEND_AT = _env("BRIEF_SEND_AT", "07:30")
# Start preparing this many minutes earlier so the brief is ready at SEND_AT.
PREPARE_MINUTES = int(_env("BRIEF_PREPARE_MINUTES", "30"))
# A failing run is retried by the next scheduled check, up to this many times a day.
MAX_ATTEMPTS_PER_DAY = 3
FIRST_RUN_LOOKBACK_HOURS = 24
# If runs were missed (outage, paused workflow) don't dump a week of news.
MAX_LOOKBACK_HOURS = 96
# Small overlap so stories published while the previous run was working aren't lost.
OVERLAP_MINUTES = 15

# --- Model -------------------------------------------------------------------
# "claude-code": Claude Code CLI signed in with your Claude subscription
#   (CLAUDE_CODE_OAUTH_TOKEN) - no per-use API charges.
# "api": Anthropic API key (ANTHROPIC_API_KEY) - billed per token.
# "auto": claude-code when the subscription token is set, otherwise api.
ENGINE = _env("BRIEF_ENGINE", "auto")
MODEL = _env("BRIEF_MODEL", "claude-opus-5-5")
EFFORT = _env("BRIEF_EFFORT", "high")
WEB_RESEARCH = _env_bool("BRIEF_WEB_RESEARCH", True)
WEB_RESEARCH_MAX_SEARCHES = int(_env("BRIEF_WEB_RESEARCH_MAX_SEARCHES", "6"))

INVESTOR_PROFILE = _env(
    "INVESTOR_PROFILE",
    "Self-directed investor with moderate risk tolerance. Long-only cash account in USD "
    "that can trade US-listed stocks and ETFs (including spot bitcoin/ether ETFs) and spot "
    "crypto. Holding periods from a few days to a few months. No options, no leverage.",
)

# --- Delivery ----------------------------------------------------------------
SITE_URL = _env("SITE_URL", "")  # e.g. https://<user>.github.io/market-brief/
VAPID_SUBJECT = _env("VAPID_SUBJECT", "mailto:market-brief@example.com")
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh")

# --- Sources -----------------------------------------------------------------
CATEGORIES = ("equities", "rates", "oil", "crypto", "macro")


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    category: str


FEEDS: list[Feed] = [
    Feed("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "equities"),
    Feed("CNBC Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html", "equities"),
    Feed("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "macro"),
    Feed("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "equities"),
    Feed("MarketWatch Market Pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "equities"),
    Feed("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "equities"),
    Feed("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "rates"),
    Feed("OilPrice.com", "https://oilprice.com/rss/main", "oil"),
    Feed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
    Feed("Cointelegraph", "https://cointelegraph.com/rss", "crypto"),
    Feed("Decrypt", "https://decrypt.co/feed", "crypto"),
]

# Google News search feeds fill the gaps (wire stories from Reuters, Bloomberg, AP...).
GOOGLE_NEWS_QUERIES: dict[str, list[str]] = {
    "equities": ["stock market today Wall Street", "S&P 500 Nasdaq stocks", "premarket stocks movers earnings"],
    "rates": ["Treasury yields bond market", "Federal Reserve interest rates"],
    "oil": ["Brent crude oil prices", "OPEC oil supply"],
    "crypto": ["bitcoin price", "ethereum solana crypto market"],
    "macro": ["US economy inflation jobs report"],
}

# Earnings: only companies at least this large (USD market cap) make the brief.
EARNINGS_MIN_MARKET_CAP = 20e9
EARNINGS_PER_DAY = 15
EARNINGS_DAYS_AHEAD = 5
# Upcoming economic events: all US events of these impacts, plus "High" impact
# events from these other economies (they move oil, yields and the dollar).
ECON_US_IMPACTS = ("High", "Medium")
ECON_OTHER_COUNTRIES = ("EUR", "CNY", "JPY", "GBP")

MAX_ITEMS_PER_CATEGORY = 45
MAX_ITEMS_TOTAL = 180


@dataclass(frozen=True)
class Instrument:
    symbol: str  # Yahoo Finance symbol
    label: str
    group: str  # equities | rates | oil | crypto
    kind: str = "price"  # "price" -> % change, "yield" -> basis-point change


INSTRUMENTS: list[Instrument] = [
    # US equities
    Instrument("^GSPC", "S&P 500", "equities"),
    Instrument("^IXIC", "Nasdaq Composite", "equities"),
    Instrument("^DJI", "Dow Jones", "equities"),
    Instrument("^RUT", "Russell 2000", "equities"),
    Instrument("ES=F", "S&P 500 futures", "equities"),
    Instrument("NQ=F", "Nasdaq 100 futures", "equities"),
    Instrument("^VIX", "VIX", "equities"),
    Instrument("XLK", "Tech (XLK)", "equities"),
    Instrument("SMH", "Semis (SMH)", "equities"),
    Instrument("XLF", "Financials (XLF)", "equities"),
    Instrument("XLE", "Energy (XLE)", "equities"),
    Instrument("XLV", "Health care (XLV)", "equities"),
    Instrument("XLI", "Industrials (XLI)", "equities"),
    Instrument("XLY", "Cons. discretionary (XLY)", "equities"),
    Instrument("XLP", "Cons. staples (XLP)", "equities"),
    Instrument("XLU", "Utilities (XLU)", "equities"),
    # Rates & credit
    Instrument("^IRX", "US 13-week bill", "rates", "yield"),
    Instrument("^FVX", "US 5Y yield", "rates", "yield"),
    Instrument("^TNX", "US 10Y yield", "rates", "yield"),
    Instrument("^TYX", "US 30Y yield", "rates", "yield"),
    Instrument("SHY", "1-3Y Treasuries (SHY)", "rates"),
    Instrument("IEF", "7-10Y Treasuries (IEF)", "rates"),
    Instrument("TLT", "20Y+ Treasuries (TLT)", "rates"),
    Instrument("LQD", "IG credit (LQD)", "rates"),
    Instrument("HYG", "High yield (HYG)", "rates"),
    # Oil & cross-asset
    Instrument("BZ=F", "Brent crude", "oil"),
    Instrument("CL=F", "WTI crude", "oil"),
    Instrument("BNO", "Brent oil fund (BNO)", "oil"),
    Instrument("GC=F", "Gold", "oil"),
    Instrument("DX-Y.NYB", "US dollar index", "oil"),
]

# Major layer-1 networks, as CoinGecko ids. The Yahoo symbol is the fallback
# when CoinGecko rate-limits (None = no Yahoo listing worth relying on).
CRYPTO: list[tuple[str, str, str | None]] = [
    ("bitcoin", "Bitcoin (BTC)", "BTC-USD"),
    ("ethereum", "Ether (ETH)", "ETH-USD"),
    ("solana", "Solana (SOL)", "SOL-USD"),
    ("binancecoin", "BNB", "BNB-USD"),
    ("ripple", "XRP", "XRP-USD"),
    ("cardano", "Cardano (ADA)", "ADA-USD"),
    ("avalanche-2", "Avalanche (AVAX)", "AVAX-USD"),
    ("sui", "Sui (SUI)", None),
    ("the-open-network", "Toncoin (TON)", None),
]

USER_AGENT = "Mozilla/5.0 (compatible; MarketBrief/1.0; +https://github.com/)"
HTTP_TIMEOUT = 20
