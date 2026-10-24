"""Deliver the brief to the phone: Web Push to the installed web app, and/or ntfy."""

from __future__ import annotations

import base64
import json
import logging
import os
import re

import requests
from pywebpush import WebPushException, webpush

from . import config

log = logging.getLogger(__name__)

ARROWS = {"buy": "▲", "add": "▲", "trim": "▼", "sell": "▼", "avoid": "✕", "hedge": "◆"}


def short_instrument(text: str, limit: int = 18) -> str:
    """'3-6 month T-bills (or SGOV); SHY as ...' -> '3-6 month T-bills' for the lock screen."""
    head = re.split(r" \(|;|,| or | / ", text, maxsplit=1)[0].strip()
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def notification(record: dict) -> dict:
    """Title/body for the lock screen from a stored brief record."""
    brief = record["brief"]
    lines = [
        f"{i['rank']}. {ARROWS.get(i['direction'], '•')} {short_instrument(i['instrument'])}: {i['one_liner']}"
        for i in brief["top_ideas"]
    ]
    return {
        "title": brief["notification_title"],
        "body": "\n".join(lines),
        "url": f"./?d={record['date']}",
        "tag": f"brief-{record['date']}",
    }


def _subscriptions() -> list[dict]:
    raw = os.getenv("WEB_PUSH_SUBSCRIPTIONS", "").strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else [parsed]


def send_web_push(message: dict) -> int:
    subs = _subscriptions()
    key = config.clean_secret(os.getenv("VAPID_PRIVATE_KEY", ""))
    if not subs or not key:
        log.info("web push not configured (need WEB_PUSH_SUBSCRIPTIONS and VAPID_PRIVATE_KEY)")
        return 0
    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(message),
                vapid_private_key=key,
                vapid_claims={"sub": config.VAPID_SUBJECT},
                ttl=12 * 3600,
                headers={"Urgency": "high"},
                timeout=config.HTTP_TIMEOUT,
            )
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                log.error("push subscription expired (HTTP %s): open the app, tap 'Enable notifications' "
                          "again and update the WEB_PUSH_SUBSCRIPTIONS secret", status)
            else:
                log.error("web push failed (HTTP %s): %s", status, exc)
        except Exception as exc:  # network errors etc.; keep going for other devices
            log.error("web push failed: %s", exc)
    return sent


def send_ntfy(message: dict) -> int:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return 0
    headers = {"Title": message["title"], "Priority": "high", "Tags": "chart_with_upwards_trend"}
    if config.SITE_URL:
        headers["Click"] = config.SITE_URL.rstrip("/") + "/" + message["url"].removeprefix("./")
    if token := os.getenv("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    # HTTP headers must be latin-1; ntfy decodes RFC 2047 encoded words.
    headers = {k: v if v.isascii() else f"=?UTF-8?B?{_b64(v)}?=" for k, v in headers.items()}
    resp = requests.post(f"{config.NTFY_SERVER.rstrip('/')}/{topic}", data=message["body"].encode(),
                         headers=headers, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return 1


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def deliver(message: dict) -> int:
    web_push = bool(_subscriptions() and config.clean_secret(os.getenv("VAPID_PRIVATE_KEY", "")))
    if not web_push and not os.getenv("NTFY_TOPIC", "").strip():
        # Expected on the very first run, before a phone is connected (README step 6).
        log.warning("no notification channel configured yet; brief published without a push")
        return 0
    sent = send_web_push(message) + send_ntfy(message)
    if sent == 0:
        raise RuntimeError("no notification was delivered — check the push secrets (see README)")
    log.info("delivered %d notification(s)", sent)
    return sent
