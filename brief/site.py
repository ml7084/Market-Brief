"""Assemble the static web app (web/ + brief data) for GitHub Pages."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, store

SAMPLE = config.ROOT / "tests" / "fixtures" / "sample_record.json"


def build(out: Path, sample: bool = False) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(config.WEB_DIR, out)
    data_out = out / "data"
    if sample:
        record = json.loads(SAMPLE.read_text())
        store.save(record, data_out)
    else:
        for name in ("briefs", "latest.json", "index.json"):
            src = config.DATA_DIR / name
            if src.is_dir():
                shutil.copytree(src, data_out / name)
            elif src.exists():
                data_out.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, data_out / name)
    build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    (out / "config.json").write_text(json.dumps({
        "vapidPublicKey": config.clean_secret(os.getenv("VAPID_PUBLIC_KEY", "")),
        "repo": os.getenv("GITHUB_REPOSITORY", ""),  # owner/name, for the app's time zone sync
        "sendAt": config.SEND_AT,
        "build": build_id,
    }))
    # Bump the service worker's cache name so installed apps pick up new app code.
    sw = out / "sw.js"
    sw.write_text(sw.read_text().replace("__BUILD__", build_id))
    (out / ".nojekyll").touch()
    return out
