"""Command line: python -m brief <gate|generate|build-site|notify|vapid>.

Third-party imports stay inside the commands so `gate` runs on a bare Python."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, schedule

log = logging.getLogger("brief")


def cmd_gate(args) -> int:
    action, reason = schedule.plan(schedule.load_state(), force=args.force)
    print(f"action={action} ({reason})")
    if out := os.getenv("GITHUB_OUTPUT"):
        with open(out, "a") as fh:
            fh.write(f"action={action}\n")
    return 0


def _record_headlines(brief, items) -> list[dict]:
    wanted = set(brief.top_headline_ids)
    for section in brief.sections:
        wanted.update(section.headline_ids)
    return [
        {"id": i.id, "title": i.title, "url": i.url, "source": i.source,
         "category": i.category, "published": i.published.isoformat(timespec="minutes")}
        for i in items if i.id in wanted
    ]


def cmd_generate(args) -> int:
    state = schedule.load_state()
    try:
        return _generate(args, state)
    except Exception:
        local = schedule.local_now()
        state.failures = schedule.failures_today(state, local) + 1
        state.failures_date = local.date().isoformat()
        schedule.save_state(state)
        if state.failures >= config.MAX_ATTEMPTS_PER_DAY:
            _send_failure_notice()
        else:
            log.error("attempt %d of %d failed; the next scheduled check retries",
                      state.failures, config.MAX_ATTEMPTS_PER_DAY)
        raise


def _send_failure_notice() -> None:
    from . import notify

    try:
        notify.deliver({"title": "Morning brief failed",
                        "body": f"{config.MAX_ATTEMPTS_PER_DAY} attempts failed today. Check the GitHub Actions log.",
                        "url": "./", "tag": "brief-failure"})
    except Exception as exc:
        log.error("could not send the failure notice: %s", exc)


def _generate(args, state: schedule.State) -> int:
    from . import events, markets, news, store
    from .analyst import build_prompt, make_engine

    started = datetime.now(timezone.utc)
    since, until = schedule.news_window(state, started)
    local = schedule.local_now(started)
    log.info("window %s -> %s", since.isoformat(timespec="minutes"), until.isoformat(timespec="minutes"))

    engine = make_engine()
    items, health = news.collect(since, until)
    market = markets.snapshot()
    calendar = events.collect(since, until)
    # On a forced re-run, compare with the previous day's brief rather than this morning's.
    previous = store.latest_before(local.date().isoformat())

    research = ""
    if config.WEB_RESEARCH:
        try:
            research = engine.research(local, items)
        except Exception as exc:  # research is a bonus; the feeds, calendars and prices still make a brief
            log.warning("web research failed: %s", exc)

    prompt = build_prompt(local, since, items, market, previous, research, calendar)
    if args.print_prompt:
        print(prompt)
    brief = engine.write_brief(prompt)

    record = {
        "date": local.date().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": str(local.tzinfo),
        "window": {"since": since.isoformat(timespec="minutes"), "until": until.isoformat(timespec="minutes")},
        "brief": brief.model_dump(),
        "headlines": _record_headlines(brief, items),
        "market": market,
        "sources": {
            "headlines_considered": len(items),
            "live_sources": sum(1 for n in health.values() if n),
            "total_sources": len(health),
            "earnings_events": len(calendar["earnings_reported"]) + len(calendar["earnings_upcoming"]),
            "economic_events": len(calendar["econ_released"]) + len(calendar["econ_upcoming"]),
            "web_research": bool(research),
        },
        "model": config.MODEL,
    }
    store.save(record)
    state.last_news_cutoff = started.isoformat(timespec="seconds")
    state.last_brief_date = record["date"]
    state.failures, state.failures_date = 0, None
    schedule.save_state(state)
    log.info("saved brief for %s: %s", record["date"], brief.notification_title)
    return 0


def cmd_build_site(args) -> int:
    from . import site

    out = site.build(Path(args.out), sample=args.sample)
    log.info("site written to %s", out)
    return 0


def cmd_notify(args) -> int:
    from . import notify, store

    if args.test:
        notify.deliver({"title": "Market Brief is connected", "body": "You'll get the morning brief here at 7:30.",
                        "url": "./", "tag": "brief-test"})
        return 0
    state = schedule.load_state()
    if not schedule.notify_due(state, force=args.force):
        log.info("not time to send yet (or already sent today)")
        return 0
    record = store.latest()
    if not record:
        log.error("no brief to send; run `python -m brief generate` first")
        return 1
    notify.deliver(notify.notification(record))
    state.last_notified_date = schedule.local_now().date().isoformat()
    schedule.save_state(state)
    return 0


def cmd_vapid(args) -> int:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print("Add these to your GitHub repository (Settings → Secrets and variables → Actions):\n")
    print(f"  Secret   VAPID_PRIVATE_KEY = {b64(private)}")
    print(f"  Variable VAPID_PUBLIC_KEY  = {b64(public)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m brief")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gate", help="decide what this scheduled check should do: generate, notify or skip")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("generate", help="collect news and prices, write the brief")
    p.add_argument("--print-prompt", action="store_true", help="print the prompt sent to Claude")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("build-site", help="assemble the web app for GitHub Pages")
    p.add_argument("--out", default="_site")
    p.add_argument("--sample", action="store_true", help="use the bundled sample brief (for previews)")
    p.set_defaults(func=cmd_build_site)

    p = sub.add_parser("notify", help="push today's brief to your phone once it's 7:30 local time")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--force", action="store_true", help="send now, even before send time or if already sent")
    group.add_argument("--test", action="store_true", help="send a test notification")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("vapid", help="generate the Web Push key pair")
    p.set_defaults(func=cmd_vapid)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
