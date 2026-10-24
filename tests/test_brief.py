import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest

import anthropic
from brief import analyst, config, events, markets, news, notify, schedule, site, store, vapid
from brief.__main__ import main
from brief.models import Brief

FIXTURE = Path(__file__).parent / "fixtures" / "sample_record.json"
UTC = timezone.utc


@pytest.fixture
def sample():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


# --- scheduling -------------------------------------------------------------

@pytest.mark.parametrize("tz, utc_time, state, expected", [
    # New York summer (UTC-4): prepare from 07:00, send from 07:30 local
    ("America/New_York", "2026-07-15T10:55", {}, "skip"),
    ("America/New_York", "2026-07-15T11:00", {}, "generate"),
    ("America/New_York", "2026-07-15T11:15", {"last_brief_date": "2026-07-15"}, "skip"),
    ("America/New_York", "2026-07-15T11:30", {"last_brief_date": "2026-07-15"}, "notify"),
    ("America/New_York", "2026-07-15T11:45", {"last_brief_date": "2026-07-15", "last_notified_date": "2026-07-15"}, "skip"),
    # Same wall-clock time in winter (UTC-5) is an hour later in UTC
    ("America/New_York", "2026-12-15T12:30", {"last_brief_date": "2026-12-15"}, "notify"),
    # Travelling: Singapore (UTC+8) and India (UTC+5:30) - 07:30 there
    ("Asia/Singapore", "2026-07-14T23:00", {}, "generate"),
    ("Asia/Singapore", "2026-07-14T23:30", {"last_brief_date": "2026-07-15"}, "notify"),
    ("Asia/Kolkata", "2026-07-15T02:00", {"last_brief_date": "2026-07-15"}, "notify"),
    # A brief that finished late (after 07:30) still gets sent by the next check
    ("Europe/London", "2026-07-15T09:15", {"last_brief_date": "2026-07-15"}, "notify"),
    # Three failures today: stop retrying until tomorrow
    ("America/New_York", "2026-07-15T11:45", {"failures_date": "2026-07-15", "failures": 3}, "skip"),
    ("America/New_York", "2026-07-15T11:45", {"failures_date": "2026-07-14", "failures": 3}, "generate"),
])
def test_plan_sends_at_0730_in_any_time_zone(monkeypatch, tz, utc_time, state, expected):
    monkeypatch.setattr(config, "TIMEZONE", tz)
    now = datetime.fromisoformat(utc_time).replace(tzinfo=UTC)
    assert schedule.plan(schedule.State(**state), now)[0] == expected


def test_plan_force_and_bad_time_zone(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "Mars/Olympus_Mons")
    now = datetime(2026, 7, 15, 3, 0, tzinfo=UTC)
    assert schedule.local_now(now).tzinfo.key == schedule.FALLBACK_TIMEZONE
    assert schedule.plan(schedule.State(last_brief_date="2026-07-14"), now, force=True)[0] == "generate"


def test_notify_due(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "Asia/Tokyo")
    at_0715 = datetime(2026, 7, 14, 22, 15, tzinfo=UTC)
    at_0730 = datetime(2026, 7, 14, 22, 30, tzinfo=UTC)
    ready = schedule.State(last_brief_date="2026-07-15")
    assert not schedule.notify_due(ready, at_0715)
    assert schedule.notify_due(ready, at_0730)
    assert schedule.notify_due(ready, at_0715, force=True)
    sent = schedule.State(last_brief_date="2026-07-15", last_notified_date="2026-07-15")
    assert not schedule.notify_due(sent, at_0730)
    assert not schedule.notify_due(schedule.State(last_brief_date="2026-07-14"), at_0730, force=True)


def test_news_window_since_last_brief_with_cap():
    now = datetime(2026, 10, 24, 10, 40, tzinfo=UTC)
    since, until = schedule.news_window(schedule.State(), now)
    assert until - since == timedelta(hours=24)  # first run
    last = (now - timedelta(hours=25)).isoformat()
    since, _ = schedule.news_window(schedule.State(last_news_cutoff=last), now)
    assert now - since == timedelta(hours=25, minutes=15)  # since last cutoff, with overlap
    stale = (now - timedelta(days=10)).isoformat()
    since, _ = schedule.news_window(schedule.State(last_news_cutoff=stale), now)
    assert now - since == timedelta(hours=config.MAX_LOOKBACK_HOURS)


# --- news -------------------------------------------------------------------

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title><link>https://example.com</link>
<item><title>Fed holds rates steady</title><link>https://example.com/a</link>
  <description>&lt;p&gt;Officials signal patience.&lt;/p&gt;</description><pubDate>Fri, 24 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://example.com/old</link><pubDate>Mon, 20 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>No date story</title><link>https://example.com/nodate</link></item>
</channel></rss>"""

GOOGLE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title><link>https://news.google.com/</link>
<item><title>Fed holds rates steady - Reuters</title><link>https://news.google.com/x</link>
  <source url="https://reuters.com">Reuters</source><pubDate>Fri, 24 Oct 2026 09:05:00 GMT</pubDate>
  <description>&lt;a href="x"&gt;Fed holds rates steady&lt;/a&gt;</description></item>
<item><title>Brent slides on supply - Bloomberg</title><link>https://news.google.com/y</link>
  <source url="https://bloomberg.com">Bloomberg</source><pubDate>Fri, 24 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_parse_and_select_headlines():
    items = news.parse_feed(RSS, "Example", "rates") + news.parse_feed(GOOGLE_RSS, "Google News: fed", "oil")
    assert {i.title for i in items} == {"Fed holds rates steady", "Old story", "Brent slides on supply"}
    google = next(i for i in items if i.title == "Brent slides on supply")
    assert google.source == "Bloomberg" and google.summary == ""

    since = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
    until = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    picked = news.select(items, since, until)
    assert [i.title for i in picked] == ["Fed holds rates steady", "Brent slides on supply"]  # dupe + old dropped
    assert [i.id for i in picked] == ["n1", "n2"]
    assert picked[0].summary == "Officials signal patience."


def test_google_news_url_encodes_query():
    assert "q=brent+crude+when%3A2d" in news.google_news_url("brent crude", 2)


# --- markets ----------------------------------------------------------------

def _chart(closes, price, market_day):
    start = datetime(2026, 9, 1, 20, tzinfo=UTC)
    stamps = [int((start + timedelta(days=n)).timestamp()) for n in range(len(closes))]
    market_time = int(datetime.fromisoformat(market_day).replace(tzinfo=UTC).timestamp())
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": price, "regularMarketTime": market_time},
        "timestamp": stamps, "indicators": {"quote": [{"close": closes}]},
    }]}}


def test_quote_from_chart_price_and_yield():
    closes = [100.0 + n for n in range(30)]  # last bar (129) is today's partial bar
    payload = _chart(closes, 130.0, "2026-09-30T20:00")
    q = markets.quote_from_chart(payload, "SPY", "S&P", "equities", "price")
    assert q.last == 130.0
    assert q.change_1d == round((130 / 128 - 1) * 100, 2)  # vs yesterday's close, not today's bar
    assert q.change_1w == round((130 / 124 - 1) * 100, 2)

    ylds = [4.0] * 29 + [4.1]
    yq = markets.quote_from_chart(_chart(ylds, 4.1, "2026-09-30T20:00"), "^TNX", "10Y", "rates", "yield")
    assert yq.change_1d == 10.0  # basis points


def test_quote_from_chart_handles_empty():
    assert markets.quote_from_chart({"chart": {"result": None}}, "X", "X", "equities", "price") is None


def test_parse_treasury_csv():
    csv_text = ('Date,"1 Mo","3 Mo","6 Mo","1 Yr","2 Yr","5 Yr","10 Yr","30 Yr"\n'
                "10/23/2026,4.10,4.05,3.95,3.80,3.62,3.79,4.08,4.55\n"
                "10/22/2026,4.10,4.06,3.96,3.82,3.64,3.82,4.11,4.57\n")
    curve = markets.parse_treasury_csv(csv_text)
    assert curve["date"] == "10/23/2026"
    assert curve["spread_2s10s_bp"] == 46.0
    assert curve["change_bp"]["10 Yr"] == -3.0


# --- earnings & economic data -------------------------------------------------

NASDAQ_EARNINGS_PAST = {"data": {"rows": [
    {"symbol": "MSFT", "name": "Microsoft Corporation", "marketCap": "$3,100,000,000,000", "time": "time-after-hours",
     "fiscalQuarterEnding": "Sep/2026", "eps": "$3.30", "epsForecast": "$3.10", "surprise": "6.45"},
    {"symbol": "TINY", "name": "Tiny Corp", "marketCap": "$900,000,000", "time": "time-pre-market", "eps": "$0.10"},
]}}
NASDAQ_EARNINGS_TODAY = {"data": {"rows": [
    {"symbol": "XOM", "name": "Exxon Mobil Corporation", "marketCap": "$480,000,000,000", "time": "time-pre-market",
     "epsForecast": "$1.85", "lastYearEPS": "$1.92", "eps": ""},
    {"symbol": "NA", "name": "No cap", "marketCap": "N/A", "time": "time-not-supplied"},
]}}
NASDAQ_ECONOMIC = {"data": {"rows": [
    {"gmt": "12:30", "country": "United States", "eventName": "Initial Jobless Claims", "actual": "231K", "consensus": "220K", "previous": "218K"},
    {"gmt": "14:00", "country": "United States", "eventName": "Existing Home Sales", "actual": "&nbsp;", "consensus": "3.9M", "previous": "4.0M"},
    {"gmt": "09:00", "country": "Germany", "eventName": "Ifo", "actual": "88.1", "consensus": "88", "previous": "87.7"},
]}}
FOREXFACTORY = [
    {"title": "Core PCE Price Index m/m", "country": "USD", "date": "2026-10-23T08:30:00-04:00", "impact": "High", "forecast": "0.2%", "previous": "0.3%"},
    {"title": "Natural Gas Storage", "country": "USD", "date": "2026-10-23T10:30:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "ECB Press Conference", "country": "EUR", "date": "2026-10-23T08:45:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "German Ifo", "country": "EUR", "date": "2026-10-23T04:00:00-04:00", "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "Old news", "country": "USD", "date": "2026-10-19T08:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
]


def test_parse_earnings_keeps_large_caps():
    past = events.parse_earnings(NASDAQ_EARNINGS_PAST, "2026-10-22")
    assert [r["symbol"] for r in past] == ["MSFT"]
    assert past[0]["time"] == "after close" and past[0]["surprise_pct"] == "6.45"
    today = events.parse_earnings(NASDAQ_EARNINGS_TODAY, "2026-10-23")
    assert [r["symbol"] for r in today] == ["XOM"] and today[0]["eps"] == ""


def test_parse_economic_us_only_and_cleans_blanks():
    rows = events.parse_economic(NASDAQ_ECONOMIC, "2026-10-22")
    assert [r["event"] for r in rows] == ["Initial Jobless Claims", "Existing Home Sales"]
    assert rows[1]["actual"] == ""


def test_parse_forexfactory_filters_impact_and_window():
    start = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
    rows = events.parse_forexfactory(FOREXFACTORY, start, start + timedelta(days=7))
    assert [r["event"] for r in rows] == ["Core PCE Price Index m/m", "ECB Press Conference"]


def test_collect_sorts_reported_and_upcoming(monkeypatch):
    def fake_get(session, url):
        if "earnings?date=2026-10-22" in url:
            return NASDAQ_EARNINGS_PAST
        if "earnings?date=2026-10-23" in url:
            return NASDAQ_EARNINGS_TODAY
        if "economicevents?date=2026-10-22" in url:
            return NASDAQ_ECONOMIC
        if "ff_calendar_thisweek" in url:
            return FOREXFACTORY
        return None

    monkeypatch.setattr(events, "_get_json", fake_get)
    since = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)
    until = datetime(2026, 10, 23, 11, 0, tzinfo=UTC)
    got = events.collect(since, until)
    assert [r["symbol"] for r in got["earnings_reported"]] == ["MSFT"]
    assert [r["symbol"] for r in got["earnings_upcoming"]] == ["XOM"]
    assert [r["event"] for r in got["econ_released"]] == ["Initial Jobless Claims"]
    assert [r["event"] for r in got["econ_upcoming"]] == ["Core PCE Price Index m/m", "ECB Press Conference"]


CALENDAR = {
    "earnings_reported": events.parse_earnings(NASDAQ_EARNINGS_PAST, "2026-10-22"),
    "earnings_upcoming": events.parse_earnings(NASDAQ_EARNINGS_TODAY, "2026-10-23"),
    "econ_released": [r for r in events.parse_economic(NASDAQ_ECONOMIC, "2026-10-22") if r["actual"]],
    "econ_upcoming": events.parse_forexfactory(FOREXFACTORY, datetime(2026, 10, 23, 10, tzinfo=UTC),
                                               datetime(2026, 10, 30, tzinfo=UTC)),
}


# --- prompt & model call ------------------------------------------------------

def _items():
    return [news.NewsItem("Fed holds rates steady", "https://e.com/a", "Reuters", "rates",
                          datetime(2026, 10, 24, 9, tzinfo=UTC), "Officials signal patience.", "n1")]


def test_build_prompt_includes_all_inputs(sample, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    local = schedule.local_now(datetime(2026, 10, 23, 10, 40, tzinfo=UTC))
    prompt = analyst.build_prompt(local, local - timedelta(hours=24), _items(), sample["market"], sample,
                                  "Futures +0.2%", CALENDAR)
    assert "Friday 23 October 2026, 06:40" in prompt
    assert "pre-market (06:40 ET)" in prompt
    assert "[n1] 10-24 05:00 · rates · Reuters — Fed holds rates steady. Officials signal patience." in prompt
    assert "US 10Y yield [^TNX]: 4.080% | 1D -3.0bp" in prompt
    assert "2s10s +46bp" in prompt
    assert "1. BUY IEF" in prompt  # previous ideas carried in for review
    assert "Futures +0.2%" in prompt
    assert "- MSFT Microsoft Corporation ($3.10T) 2026-10-22 after close, quarter Sep/2026: EPS $3.30 vs est $3.10 (surprise 6.45%)" in prompt
    assert "- 2026-10-23 before open: XOM Exxon Mobil Corporation ($480B), est EPS $1.85, last year $1.92" in prompt
    assert "- 2026-10-22 12:30 Initial Jobless Claims: actual 231K, consensus 220K, previous 218K" in prompt
    assert "- Fri 23 Oct 08:30 USD Core PCE Price Index m/m [High]: forecast 0.2%, previous 0.3%" in prompt


def test_session_status_weekend():
    saturday = datetime(2026, 10, 24, 10, 40, tzinfo=UTC)
    assert analyst.us_session_status(saturday).startswith("closed for the weekend")


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def test_write_brief_request_and_parse(sample):
    """Runs the real SDK against a fake transport to check the request we send."""
    seen = {}
    text = json.dumps(sample["brief"])

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": config.MODEL,
                                                  "content": [], "stop_reason": None, "stop_sequence": None,
                                                  "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 500}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    client = anthropic.Anthropic(api_key="test", base_url="http://api.test",
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    brief = analyst.ApiEngine(client).write_brief("prompt")

    assert isinstance(brief, Brief) and len(brief.top_ideas) == 3
    body = seen["body"]
    assert body["model"] == config.MODEL
    assert body["fallbacks"] == "default"
    assert analyst.FALLBACK_BETA in seen["beta"]
    assert body["output_config"]["effort"] == config.EFFORT
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "temperature" not in body


def test_write_brief_raises_on_refusal(sample):
    def handler(request):
        events = [
            {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": config.MODEL,
                                                  "content": [], "stop_reason": None, "stop_sequence": None,
                                                  "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    client = anthropic.Anthropic(api_key="t", base_url="http://api.test",
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="declined"):
        analyst.ApiEngine(client).write_brief("prompt")


# --- delivery ---------------------------------------------------------------

def test_notification_text(sample):
    msg = notify.notification(sample)
    assert msg["title"] == sample["brief"]["notification_title"]
    lines = msg["body"].splitlines()
    assert len(lines) == 3 and lines[0].startswith("1. ▲ IEF: ")
    assert msg["url"] == "./?d=2026-10-23"
    assert len(json.dumps(msg).encode()) < 3000  # web push payloads must stay small


def test_web_push_is_encrypted_for_the_subscription(monkeypatch, capsys, sample):
    import http_ece
    import pywebpush

    main(["vapid"])
    keys = dict(line.split(" = ") for line in capsys.readouterr().out.split("\n") if " = " in line)
    private_key = next(v for k, v in keys.items() if "PRIVATE" in k).strip()

    subscription, browser, auth = _browser_subscription()

    sent = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        sent.update(url=url, data=data, headers=headers)
        return SimpleNamespace(status_code=201, text="", headers={})

    monkeypatch.setattr(pywebpush.requests, "post", fake_post)
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", json.dumps([subscription]))
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private_key)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    message = notify.notification(sample)
    assert notify.deliver(message) == 1
    assert sent["url"] == subscription["endpoint"]
    assert sent["headers"]["Authorization"].startswith("vapid t=")
    plain = http_ece.decrypt(sent["data"], private_key=browser, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == message


def test_deliver_without_channels_is_a_warning(monkeypatch):
    for var in ("WEB_PUSH_SUBSCRIPTIONS", "VAPID_PRIVATE_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    assert notify.deliver({"title": "t", "body": "b", "url": "./", "tag": "x"}) == 0


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _browser_subscription():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    browser = ec.generate_private_key(ec.SECP256R1())
    p256dh = browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": _b64(p256dh), "auth": _b64(auth)}}
    return sub, browser, auth


def test_expired_subscription_is_reported(monkeypatch, caplog):
    import pywebpush

    sub, _, _ = _browser_subscription()
    monkeypatch.setattr(pywebpush.requests, "post",
                        lambda url, **kw: SimpleNamespace(status_code=410, reason="Gone", text="gone", headers={}))
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", json.dumps(sub))
    monkeypatch.setenv("VAPID_PRIVATE_KEY", _b64(os.urandom(32)))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(RuntimeError, match="no notification"):
        notify.deliver({"title": "t", "body": "b", "url": "./", "tag": "x"})
    assert "subscription expired (HTTP 410)" in caplog.text


def test_ntfy_headers(monkeypatch):
    calls = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.update(url=url, data=data, headers=headers)
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(notify.requests, "post", fake_post)
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(config, "SITE_URL", "https://me.github.io/market-brief/")
    notify.send_ntfy({"title": "Yields ease — chips lead", "body": "1. ▲ IEF: x", "url": "./?d=2026-10-24", "tag": "t"})
    assert calls["url"] == "https://ntfy.sh/my-topic"
    assert calls["headers"]["Click"] == "https://me.github.io/market-brief/?d=2026-10-24"
    assert calls["headers"]["Title"].startswith("=?UTF-8?B?")  # non-ASCII title survives HTTP headers


# --- end to end ---------------------------------------------------------------

class FakeEngine:
    def __init__(self, brief=None, error=None):
        self.brief, self.error, self.prompts = brief, error, []

    def research(self, local, items):
        return ""

    def write_brief(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return Brief.model_validate(self.brief)


@pytest.fixture
def offline_sources(monkeypatch, sample):
    monkeypatch.setattr(config, "WEB_RESEARCH", False)
    monkeypatch.setattr(news, "collect", lambda since, until: (_items(), {"Reuters": 1, "Dead feed": 0}))
    monkeypatch.setattr(markets, "snapshot", lambda: sample["market"])
    monkeypatch.setattr(events, "collect", lambda since, until: CALENDAR)


def test_generate_end_to_end(monkeypatch, data_dir, sample, offline_sources):
    engine = FakeEngine(sample["brief"])
    monkeypatch.setattr(analyst, "make_engine", lambda: engine)
    assert main(["generate"]) == 0

    record = store.latest()
    assert record["sources"] == {"headlines_considered": 1, "live_sources": 1, "total_sources": 2,
                                 "earnings_events": 2, "economic_events": 3, "web_research": False}
    assert record["brief"]["top_ideas"][0]["reasoning"].startswith("Sample. I start from the data")
    assert [h["id"] for h in record["headlines"]] == ["n1"]  # only headlines the brief cites are stored
    state = schedule.load_state()
    assert state.last_brief_date == record["date"] and state.last_news_cutoff
    assert json.loads((data_dir / "index.json").read_text())[0]["date"] == record["date"]
    assert "(none — this is the first brief)" in engine.prompts[0]
    assert "Initial Jobless Claims: actual 231K" in engine.prompts[0]


def test_generate_failures_retry_then_notify_once(monkeypatch, data_dir, offline_sources):
    monkeypatch.setattr(analyst, "make_engine", lambda: FakeEngine(error=RuntimeError("boom")))
    notices = []
    monkeypatch.setattr(notify, "deliver", lambda message: notices.append(message["title"]))
    for attempt in range(1, config.MAX_ATTEMPTS_PER_DAY + 1):
        with pytest.raises(RuntimeError, match="boom"):
            main(["generate"])
        assert schedule.load_state().failures == attempt
    assert notices == ["Morning brief failed"]  # only after the last attempt
    assert schedule.plan(schedule.load_state())[0] == "skip"


def test_notify_waits_for_send_time_then_records(monkeypatch, data_dir, sample):
    monkeypatch.setattr(config, "TIMEZONE", "Asia/Singapore")
    store.save(sample)
    sent = []
    monkeypatch.setattr(notify, "deliver", lambda message: sent.append(message) or 1)
    monkeypatch.setattr(schedule, "notify_due", lambda state, force=False: False)
    assert main(["notify"]) == 0 and sent == []

    monkeypatch.setattr(schedule, "notify_due", lambda state, force=False: True)
    assert main(["notify"]) == 0
    assert sent[0]["title"] == sample["brief"]["notification_title"]
    assert schedule.load_state().last_notified_date == schedule.local_now().date().isoformat()


# --- free engine: Claude Code with a Claude subscription -------------------------

def _fake_cli(tmp_path, payload: dict, exit_code: int = 0) -> str:
    """A stand-in `claude` binary that records how it was called and prints `payload`."""
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    script = tmp_path / "claude"
    script.write_text(f"""#!{os.sys.executable}
import json, os, sys
call = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
        "has_api_key": "ANTHROPIC_API_KEY" in os.environ, "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}
json.dump(call, open({str(tmp_path / "call.json")!r}, "w"))
print(open({str(tmp_path / "payload.json")!r}).read())
sys.exit({exit_code})
""")
    script.chmod(0o755)
    return str(script)


def test_claude_code_engine_uses_subscription_not_api_key(tmp_path, monkeypatch, sample):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    out = {"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 2,
           "structured_output": sample["brief"]}
    engine = analyst.ClaudeCodeEngine(_fake_cli(tmp_path, out))
    brief = engine.write_brief("PROMPT TEXT")

    call = json.loads((tmp_path / "call.json").read_text())
    assert brief.top_ideas[0].instrument == "IEF"
    assert call["stdin"] == "PROMPT TEXT"
    assert call["oauth"] == "sk-ant-oat-test" and call["has_api_key"] is False
    argv = call["argv"]
    assert argv[:2] == ["-p", "--model"] and argv[2] == config.MODEL
    assert json.loads(argv[argv.index("--json-schema") + 1])["title"] == "Brief"
    assert argv[argv.index("--system-prompt") + 1] == analyst.SYSTEM
    assert "--bare" not in argv  # --bare ignores the subscription sign-in
    assert not call["cwd"].startswith(str(config.ROOT))


def test_claude_code_engine_falls_back_to_json_in_text(tmp_path, sample):
    out = {"type": "result", "subtype": "success", "is_error": False,
           "result": "Here it is:\n```json\n" + json.dumps(sample["brief"]) + "\n```"}
    brief = analyst.ClaudeCodeEngine(_fake_cli(tmp_path, out)).write_brief("p")
    assert len(brief.top_ideas) == 3


def test_claude_code_engine_reports_cli_errors(tmp_path):
    out = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Invalid API key"}
    with pytest.raises(RuntimeError, match="Invalid API key"):
        analyst.ClaudeCodeEngine(_fake_cli(tmp_path, out, exit_code=1)).write_brief("p")


def test_claude_code_research_allows_only_web_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    out = {"type": "result", "subtype": "success", "is_error": False, "result": "Futures +0.3%"}
    local = schedule.local_now(datetime(2026, 10, 23, 11, tzinfo=UTC))
    assert analyst.ClaudeCodeEngine(_fake_cli(tmp_path, out)).research(local, _items()) == "Futures +0.3%"
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"


@pytest.mark.parametrize("env, expected", [
    ({"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}, analyst.ClaudeCodeEngine),
    ({"ANTHROPIC_API_KEY": "k"}, analyst.ApiEngine),
])
def test_make_engine_prefers_the_free_subscription(monkeypatch, env, expected):
    monkeypatch.setattr(config, "ENGINE", "auto")
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert isinstance(analyst.make_engine(), expected)


def test_make_engine_without_credentials_explains(monkeypatch):
    monkeypatch.setattr(config, "ENGINE", "auto")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        analyst.make_engine()


def test_build_site_with_sample(tmp_path, monkeypatch):
    private, public = vapid.generate()
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)  # derived from the private key
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/market-brief")
    out = site.build(tmp_path / "_site", sample=True)
    cfg = json.loads((out / "config.json").read_text())
    assert cfg["repo"] == "me/market-brief" and cfg["sendAt"] == "07:30"
    assert (out / "shared.js").exists()
    assert (out / "index.html").exists() and (out / "data" / "latest.json").exists()
    assert cfg["vapidPublicKey"] == public
    assert "__BUILD__" not in (out / "sw.js").read_text()
    assert json.loads((out / "data" / "index.json").read_text())[0]["date"] == "2026-10-23"


def test_research_resumes_paused_turns(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        n = len(bodies)
        text, stop = ("Futures +0.2%. ", "pause_turn") if n == 1 else ("Brent $71.", "end_turn")
        events = [
            {"type": "message_start", "message": {"id": f"m{n}", "type": "message", "role": "assistant", "model": config.MODEL,
                                                  "content": [], "stop_reason": None, "stop_sequence": None,
                                                  "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    client = anthropic.Anthropic(api_key="t", base_url="http://api.test",
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    local = schedule.local_now(datetime(2026, 10, 23, 10, 40, tzinfo=UTC))
    notes = analyst.ApiEngine(client).research(local, _items())

    assert notes == "Futures +0.2%. Brent $71."
    assert bodies[0]["tools"][0]["type"] == "web_search_20260209"
    assert [m["role"] for m in bodies[1]["messages"]] == ["user", "assistant"]  # resumed, not restarted


def test_wrapped_token_and_keys_are_cleaned(tmp_path, monkeypatch, sample):
    # Copying from an 80-column terminal adds a line break inside the token.
    wrapped = "sk-ant-oat01-" + "a" * 67 + "\r\n" + "b" * 30 + "\n"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", wrapped)
    out = {"type": "result", "subtype": "success", "is_error": False, "structured_output": sample["brief"]}
    analyst.ClaudeCodeEngine(_fake_cli(tmp_path, out)).write_brief("p")
    assert json.loads((tmp_path / "call.json").read_text())["oauth"] == "sk-ant-oat01-" + "a" * 67 + "b" * 30

    private, public = vapid.generate()
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public[:80] + "\n" + public[80:] + " ")
    built = site.build(tmp_path / "_site", sample=True)
    assert json.loads((built / "config.json").read_text())["vapidPublicKey"] == public


@pytest.mark.parametrize("text, expected", [
    ("XLE", "XLE"),
    ("3-6 month T-bills (or SGOV); SHY as a slightly longer alternative", "3-6 month T-bills"),
    ("Alt L1s (SOL, ADA, AVAX, SUI, TON); BTC via IBIT or spot only on confirmation", "Alt L1s"),
    ("BZ=F / BNO", "BZ=F"),
    ("Investment-grade corporate bond ETFs", "Investment-grade…"),
])
def test_notification_shortens_long_instruments(text, expected):
    assert notify.short_instrument(text) == expected



# --- push keys: the public key is always derived from the private one -----------

def test_vapid_resolve_handles_common_mistakes():
    private, public = vapid.generate()
    other_private, other_public = vapid.generate()

    assert vapid.resolve({"VAPID_PRIVATE_KEY": private, "VAPID_PUBLIC_KEY": public}) == (private, public, [])
    # public key left out: derived
    assert vapid.resolve({"VAPID_PRIVATE_KEY": private})[:2] == (private, public)
    # wrong public key (e.g. from another pair, or the private key pasted twice): derived wins
    for wrong in (other_public, private, "Public Key: " + public):
        got_private, got_public, problems = vapid.resolve({"VAPID_PRIVATE_KEY": private, "VAPID_PUBLIC_KEY": wrong})
        assert (got_private, got_public) == (private, public) and problems
    # swapped
    got = vapid.resolve({"VAPID_PRIVATE_KEY": public, "VAPID_PUBLIC_KEY": private})
    assert got[:2] == (private, public) and "swapped" in got[2][0]
    # private key unusable: nothing to subscribe with, and a clear message
    got = vapid.resolve({"VAPID_PRIVATE_KEY": private[:30], "VAPID_PUBLIC_KEY": "BAD"})
    assert got[:2] == ("", "") and "43 characters" in got[2][0]


def test_vapid_keys_from_npx_web_push_are_accepted():
    # Format printed by `npx web-push generate-vapid-keys` (README step 3).
    private, public = "b4jik4US6BeiUtYhtD81OQ8TxPyfLhr0J28emLtnrVQ", \
        "BNNvip9kkiHTXMnr2LaA2hOMC0r3gbJPvBN4fYF6HW66XDGyoshHuXRCpUCykb_OONrGUgBn7_YqyOVaLg5YPok"
    assert vapid.resolve({"VAPID_PRIVATE_KEY": private, "VAPID_PUBLIC_KEY": public}) == (private, public, [])


@pytest.mark.parametrize("given, expected", [
    ("mailto:ann.lee@gmail.com", "mailto:ann.lee@gmail.com"),
    ("ann.lee@gmail.com", "mailto:ann.lee@gmail.com"),             # bare email
    ("mailto: ann.lee@gmail.com\n", "mailto:ann.lee@gmail.com"),   # space and pasted line break
    ("mailto:<ann.lee@gmail.com>", "mailto:ann.lee@gmail.com"),    # angle brackets
    ("https://ml7084.github.io/Market-Brief/", "https://ml7084.github.io/Market-Brief/"),
    ("", "https://ml7084.github.io"),                        # not set: the site's own address
    ("mailto:you@yourdomain.com", "https://ml7084.github.io"),  # README placeholder
    ("mailto:market-brief@example.com", "https://ml7084.github.io"),
    ("my email", "https://ml7084.github.io"),
    # The README's instruction text pasted as the value (seen in the wild)
    ("mailto: followed by an email you use, e.g. mailto:you@gmail.com", "https://ml7084.github.io"),
    ("mailto:you@gmail.com", "https://ml7084.github.io"),       # README example copied as-is
    ("mailto:Thomas.Lu+brief@company.co.uk", "mailto:Thomas.Lu+brief@company.co.uk"),
])
def test_push_subject_is_always_one_apple_accepts(given, expected):
    assert vapid.subject({"VAPID_SUBJECT": given, "GITHUB_REPOSITORY_OWNER": "ml7084"}) == expected


def test_signed_push_meets_apples_rules(monkeypatch, sample):
    """Decode the JWT actually sent and check each BadJwtToken cause Apple documents."""
    import time

    import pywebpush
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    private, public = vapid.generate()
    subscription, _, _ = _browser_subscription()
    sent = {}
    monkeypatch.setattr(pywebpush.requests, "post", lambda url, data=None, headers=None, timeout=None:
                        sent.update(headers=headers) or SimpleNamespace(status_code=201, text="", headers={}))
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", json.dumps(subscription))
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "wrong")
    monkeypatch.setenv("VAPID_SUBJECT", " mailto: brief.owner@gmail.com ")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notify.deliver(notify.notification(sample)) == 1

    auth = sent["headers"]["Authorization"]  # "vapid t=<jwt>, k=<public key>"
    token = auth.split("t=")[1].split(",")[0]
    key = auth.split("k=")[1]
    head, body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert key == public                                   # 1. signed with the key the phone subscribed with
    assert claims["sub"] == "mailto:brief.owner@gmail.com"  # 2. clean mailto:
    assert claims["aud"] == "https://web.push.apple.com"   # 3. the push server's origin
    assert claims["exp"] - time.time() <= 24 * 3600        # 4. at most a day ahead
    raw_sig = base64.urlsafe_b64decode(pad(sig))
    pub = _ec.EllipticCurvePublicKey.from_encoded_point(_ec.SECP256R1(), base64.urlsafe_b64decode(pad(public)))
    pub.verify(encode_dss_signature(int.from_bytes(raw_sig[:32], "big"), int.from_bytes(raw_sig[32:], "big")),
               f"{head}.{body}".encode(), _ec.ECDSA(hashes.SHA256()))


def test_bad_jwt_rejection_is_explained(monkeypatch, caplog):
    import pywebpush

    subscription, _, _ = _browser_subscription()
    monkeypatch.setattr(pywebpush.requests, "post", lambda url, **kw: SimpleNamespace(
        status_code=403, reason="Forbidden", text='{"reason":"BadJwtToken"}', headers={}))
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", json.dumps(subscription))
    monkeypatch.setenv("VAPID_PRIVATE_KEY", vapid.generate()[0])
    with pytest.raises(RuntimeError):
        notify.deliver({"title": "t", "body": "b", "url": "./", "tag": "x"})
    assert "Reconnect this iPhone" in caplog.text and "BadJwtToken" in caplog.text



def test_contact_email_is_masked_in_logs():
    assert vapid.redacted("mailto:jane.doe@uni.edu") == "mailto:j…@uni.edu"
    assert vapid.redacted("https://ml7084.github.io") == "https://ml7084.github.io"



def test_several_device_codes_can_be_pasted_one_per_line(monkeypatch):
    mac = {"endpoint": "https://web.push.apple.com/mac", "keys": {"p256dh": "a", "auth": "b"}}
    iphone = {"endpoint": "https://web.push.apple.com/iphone", "keys": {"p256dh": "c", "auth": "d"}}
    for raw in (json.dumps(iphone),
                json.dumps([mac, iphone]),
                json.dumps(mac) + "\n" + json.dumps(iphone) + "\n",
                json.dumps(mac) + "," + json.dumps(iphone) + "\n" + json.dumps(iphone)):  # duplicate ignored
        monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", raw)
        endpoints = [s["endpoint"] for s in notify._subscriptions()]
        assert endpoints[-1] == iphone["endpoint"] and len(endpoints) == len(set(endpoints))
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", json.dumps(mac) + "\n" + json.dumps(iphone))
    assert len(notify._subscriptions()) == 2



@pytest.mark.parametrize("raw, hint", [
    ("https://github.com/ml7084/Market-Brief/settings/secrets/actions", "web address"),
    ('{"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh"', "part of a code"),
    ("Copy code", "doesn't contain"),
])
def test_unusable_device_code_is_explained(monkeypatch, caplog, raw, hint):
    monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", raw)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", vapid.generate()[0])
    assert main(["notify", "--test"]) == 1          # fails the run, without a traceback
    assert hint in caplog.text and "Show connection code" in caplog.text


def test_codes_are_found_among_stray_text(monkeypatch):
    phone = {"endpoint": "https://web.push.apple.com/iphone", "keys": {"p256dh": "c", "auth": "d"}}
    smart = json.dumps(phone).replace('"', "“", 1)  # one quote smartened by Notes
    for raw in ("here is my code: " + json.dumps(phone) + " thanks",
                "https://github.com/ml7084/Market-Brief\n" + json.dumps(phone),
                smart):
        monkeypatch.setenv("WEB_PUSH_SUBSCRIPTIONS", raw)
        assert [s["endpoint"] for s in notify._subscriptions()] == [phone["endpoint"]]
