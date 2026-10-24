import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest

import anthropic
from brief import analyst, config, markets, news, notify, schedule, site, store
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

@pytest.mark.parametrize("utc_time, expected", [
    ("2026-07-15T10:40", True),   # EDT: 06:40 New York -> send
    ("2026-07-15T09:40", False),  # EDT: 05:40 -> too early
    ("2026-12-15T10:40", False),  # EST: 05:40 -> too early (the 11:40 run sends)
    ("2026-12-15T11:40", True),   # EST: 06:40 -> send
])
def test_gate_handles_daylight_saving(monkeypatch, utc_time, expected):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    monkeypatch.setattr(config, "SEND_AFTER", "06:30")
    now = datetime.fromisoformat(utc_time).replace(tzinfo=UTC)
    assert schedule.should_run(schedule.State(), now)[0] is expected


def test_gate_skips_second_run_same_day(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    now = datetime(2026, 7, 15, 11, 40, tzinfo=UTC)
    state = schedule.State(last_brief_date="2026-07-15")
    assert schedule.should_run(state, now)[0] is False
    assert schedule.should_run(state, now, force=True)[0] is True


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


# --- prompt & model call ------------------------------------------------------

def _items():
    return [news.NewsItem("Fed holds rates steady", "https://e.com/a", "Reuters", "rates",
                          datetime(2026, 10, 24, 9, tzinfo=UTC), "Officials signal patience.", "n1")]


def test_build_prompt_includes_all_inputs(sample, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    local = schedule.local_now(datetime(2026, 10, 23, 10, 40, tzinfo=UTC))
    prompt = analyst.build_prompt(local, local - timedelta(hours=24), _items(), sample["market"], sample, "Futures +0.2%")
    assert "Friday 23 October 2026, 06:40" in prompt
    assert "pre-market (06:40 ET)" in prompt
    assert "[n1] 10-24 05:00 · rates · Reuters — Fed holds rates steady. Officials signal patience." in prompt
    assert "US 10Y yield [^TNX]: 4.080% | 1D -3.0bp" in prompt
    assert "2s10s +46bp" in prompt
    assert "1. BUY IEF" in prompt  # previous ideas carried in for review
    assert "Futures +0.2%" in prompt


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
    brief = analyst.Analyst(client).write_brief("prompt")

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
        analyst.Analyst(client).write_brief("prompt")


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

def test_generate_end_to_end(monkeypatch, data_dir, sample):
    monkeypatch.setattr(config, "WEB_RESEARCH", False)
    monkeypatch.setattr(news, "collect", lambda since, until: (_items(), {"Reuters": 1, "Dead feed": 0}))
    monkeypatch.setattr(markets, "snapshot", lambda: sample["market"])
    prompts = []

    class FakeAnalyst:
        def write_brief(self, prompt):
            prompts.append(prompt)
            return Brief.model_validate(sample["brief"])

    monkeypatch.setattr(analyst, "Analyst", FakeAnalyst)
    assert main(["generate"]) == 0

    record = store.latest()
    assert record["sources"] == {"headlines_considered": 1, "live_sources": 1, "total_sources": 2, "web_research": False}
    assert [h["id"] for h in record["headlines"]] == ["n1"]  # only headlines the brief cites are stored
    state = schedule.load_state()
    assert state.last_brief_date == record["date"] and state.last_news_cutoff
    assert json.loads((data_dir / "index.json").read_text())[0]["date"] == record["date"]
    assert "(none — this is the first brief)" in prompts[0]


def test_build_site_with_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLIC")
    out = site.build(tmp_path / "_site", sample=True)
    assert (out / "index.html").exists() and (out / "data" / "latest.json").exists()
    assert json.loads((out / "config.json").read_text())["vapidPublicKey"] == "BPUBLIC"
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
    notes = analyst.Analyst(client).research(local, _items())

    assert notes == "Futures +0.2%. Brent $71."
    assert bodies[0]["tools"][0]["type"] == "web_search_20260209"
    assert [m["role"] for m in bodies[1]["messages"]] == ["user", "assistant"]  # resumed, not restarted
