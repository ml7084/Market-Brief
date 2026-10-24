"""Schema Claude fills in for each brief (sent as a structured-output format)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AssetClass = Literal["us_equities", "bonds", "brent_oil", "crypto_l1"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataNote(_Strict):
    release: str = Field(description="e.g. 'Core PCE (Sep, m/m)'.")
    status: Literal["released", "upcoming"]
    when: str = Field(description="Release time, e.g. 'Thu 08:30 ET'.")
    actual: str = Field(description="Empty string if not released yet.")
    expected: str
    previous: str
    takeaway: str = Field(description="What it means for Fed expectations, yields, the dollar, oil or risk appetite.")


class EarningsNote(_Strict):
    ticker: str
    company: str
    status: Literal["reported", "upcoming"]
    timing: str = Field(description="e.g. 'Tue after close', 'today before open'.")
    result: str = Field(description="Actual vs expected EPS (and revenue/guidance if known); the bar to beat if upcoming.")
    takeaway: str = Field(description="What it means for the stock, its sector and the indices.")


class AssetSection(_Strict):
    asset_class: AssetClass
    sentiment: Literal["bullish", "bearish", "neutral", "mixed"]
    summary: str = Field(description="2-3 sentences: what happened since the last brief and why it matters.")
    key_points: list[str] = Field(description="2-4 short bullets with the specific numbers and drivers.")
    headline_ids: list[str] = Field(description="ids (like 'n12') of the most relevant headlines for this asset class.")


class Idea(_Strict):
    rank: int
    asset_class: AssetClass
    instrument: str = Field(description="Ticker or asset, e.g. 'XLE', 'IEF', 'BZ=F / BNO', 'SOL'.")
    direction: Literal["buy", "add", "trim", "sell", "avoid", "hedge"]
    title: str = Field(description="Short name for the idea, under 60 characters.")
    one_liner: str = Field(description="Under 70 characters; shown on the lock-screen notification.")
    thesis: str = Field(description="2-3 sentence summary of the case.")
    reasoning: str = Field(description=(
        "First person, as the instructor teaching a student: walk through the logic step by step - the evidence "
        "(earnings, economic data, news, price action), how it feeds through to this instrument's price, why this "
        "instrument rather than the alternatives, and what would change my mind. 5-8 sentences."))
    catalysts: list[str] = Field(description="Dated events ahead, especially earnings and data releases.")
    risks: list[str]
    entry: str = Field(description="How to enter: level, range, or approach (e.g. scale in over 3 days).")
    invalidation: str = Field(description="The price level or event that proves the idea wrong.")
    horizon: Literal["days", "weeks", "months"]
    conviction: Literal["low", "medium", "high"]
    sizing: str = Field(description="Position size guidance relative to a diversified portfolio, e.g. '2-3% starter'.")


class Lesson(_Strict):
    concept: str = Field(description="One market concept at work today, e.g. 'bull steepener', 'earnings revisions'.")
    explanation: str = Field(description="Plain-language explanation, 3-4 sentences.")
    todays_example: str = Field(description="Where the concept shows up in today's market.")


class CalendarEvent(_Strict):
    time: str = Field(description="Local time (or 'pre-market', 'after close') for today/this week.")
    event: str


class Brief(_Strict):
    notification_title: str = Field(description="Lock-screen title under 55 characters capturing the market mood.")
    market_summary: str = Field(description="3-4 sentence overview across all four asset classes.")
    since_last: str = Field(description="What materially changed since the previous brief.")
    economic_data: list[DataNote] = Field(description="Releases since the last brief, then the most important ones coming up.")
    earnings: list[EarningsNote] = Field(description="The most market-relevant earnings reported since the last brief or due in the next few days.")
    sections: list[AssetSection] = Field(description="Exactly one per asset class, in order: us_equities, bonds, brent_oil, crypto_l1.")
    instructor_take: str = Field(description=(
        "First person: my own read of the market this morning, as an instructor talking to a student - what really "
        "matters today, what the crowd may be missing, and how I'd think about positioning. 5-8 sentences."))
    top_ideas: list[Idea] = Field(description="Exactly three ideas, ranked 1-3 by conviction-weighted reward/risk.")
    prior_ideas_review: str = Field(description="How the previous brief's ideas have played out; empty string if none.")
    lesson: Lesson
    watchlist: list[CalendarEvent] = Field(description="Scheduled data, Fed speakers, auctions, earnings, OPEC or crypto events to watch.")
    top_headline_ids: list[str] = Field(description="ids of the 6-10 headlines most worth reading in full.")
