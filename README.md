# Market Brief

A morning markets brief on your iPhone lock screen. Every morning it:

1. collects all market news published since the previous brief (about 25 RSS and Google News feeds),
2. pulls prices for US equities and sectors, Treasuries and credit, Brent and WTI, gold and the dollar, and the major layer-1 crypto networks (BTC, ETH, SOL, BNB, XRP, ADA, AVAX, SUI, TON),
3. optionally runs a short web-search pass for pre-market futures and today's calendar,
4. has Claude write the brief, including **three ranked investment ideas** with entry, invalidation level, horizon and size,
5. publishes it to a small web app on GitHub Pages and sends a push notification to your iPhone.

```
GitHub Actions (daily cron)
  ├─ news.py      RSS + Google News since the last brief
  ├─ markets.py   Yahoo Finance · CoinGecko · US Treasury yield curve
  ├─ analyst.py   Claude: web research pass → structured brief (JSON schema)
  ├─ data/        brief committed to the repo (history + "since last" state)
  ├─ GitHub Pages web app (installable on iPhone, works offline)
  └─ notify.py    Web Push to the installed app (and/or ntfy)
```

![The app on iPhone, light and dark mode (sample data)](docs/preview.png)

There's no App Store, Xcode or Apple developer account involved. On iPhone it installs from Safari as a Home Screen web app, which iOS 16.4 and later allows to receive notifications.

> Everything is generated automatically from public data and can be wrong. It is not personalised investment advice.

## Set up (about 15 minutes)

### 1. Put the code on GitHub

Create a new repository and push this folder to it. Free GitHub Pages needs a **public** repository (private works on paid plans). Your API keys and push subscription stay in encrypted secrets either way, but the briefs themselves will be publicly readable.

### 2. Add your Anthropic API key

Create a key at [console.anthropic.com](https://console.anthropic.com), then in the repo go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key |

### 3. Create the push keys

```bash
pip install -r requirements.txt
python -m brief vapid
```

Add the output to the repo:

| Type | Name | Value |
|---|---|---|
| Secret | `VAPID_PRIVATE_KEY` | from the command |
| Variable | `VAPID_PUBLIC_KEY` | from the command |
| Variable | `VAPID_SUBJECT` | `mailto:you@yourdomain.com` (Apple wants a real contact) |

### 4. Turn on GitHub Pages

**Settings → Pages → Build and deployment → Source: GitHub Actions.**

### 5. Run it once

**Actions → Morning brief → Run workflow.** When it finishes, the app is live at `https://<your-user>.github.io/<repo>/` (the URL is shown on the deploy job). No notification goes out yet because no phone is connected; the log says so.

### 6. Install on your iPhone and connect notifications

1. Open the URL in **Safari** → tap **Share** → **Add to Home Screen**.
2. Open **Brief** from the Home Screen (not from Safari) → **Enable notifications** → **Allow**.
3. Tap **Copy code** and save it as the repo secret `WEB_PUSH_SUBSCRIPTIONS`.
   For several devices, paste a JSON array: `[{…iphone…}, {…ipad…}]`.
4. Run the workflow again with **Only send a test notification** ticked. A test notification should arrive within seconds.

That's it. From now on the brief arrives every morning. Tap the notification to open the full brief. **Past briefs** keeps the history.

If you delete and re-add the Home Screen app, its subscription changes: repeat step 6 (the workflow log says *"push subscription expired"* when this is needed).

### Option: ntfy instead of (or alongside) Web Push

Install the free [ntfy](https://ntfy.sh) iOS app, subscribe to a long random topic name (topic names on ntfy.sh are public, so make it unguessable), and add it as the secret `NTFY_TOPIC`. Tapping the notification opens the brief in the web app. Self-hosted ntfy: set the variable `NTFY_SERVER` and, if needed, the secret `NTFY_TOKEN`.

## Change the delivery time

The default is about **06:40 New York time** (pre-market), all year round. GitHub cron runs in UTC, so the workflow fires at two UTC times: the summer and winter offsets of your send time. The `gate` step then skips whichever run is too early or comes after today's brief has already gone out.

To change it, set the repo variables and edit the two `cron` lines in `.github/workflows/morning-brief.yml`:

| You want | `BRIEF_TIMEZONE` | `BRIEF_SEND_AFTER` | cron (UTC) |
|---|---|---|---|
| 06:40 New York (default) | `America/New_York` | `06:30` | `40 10 * * *` and `40 11 * * *` |
| 07:10 London | `Europe/London` | `07:00` | `10 6 * * *` and `10 7 * * *` |
| 07:10 Singapore / Hong Kong | `Asia/Singapore` | `07:00` | `10 23 * * *` (no DST, one line is enough) |
| 07:10 Tokyo | `Asia/Tokyo` | `07:00` | `10 22 * * *` |

GitHub can start scheduled runs 5–30 minutes late at busy times, so pick a minute that isn't `:00`. The brief runs every day, weekends included. Crypto trades 24/7, and Monday's brief covers everything since Friday's. For weekdays only, change the trailing `* * *` to `* * 1-5` (in UTC, so mind the date line for Asia: use `* * 0-4` for a 23:10 UTC run).

## Tune it

All optional, set as repository **Variables**:

| Variable | Default | What it does |
|---|---|---|
| `INVESTOR_PROFILE` | moderate risk, long-only USD cash account, ETFs/stocks/spot crypto, days–months | Tailors the three ideas to you. Describe your risk tolerance, account type, holding period and what you can trade. |
| `BRIEF_MODEL` | `claude-opus-5-5` | Claude model. `claude-sonnet-5-5` costs about half as much. |
| `BRIEF_EFFORT` | `high` | `medium` is cheaper and faster; `xhigh`/`max` think harder. |
| `BRIEF_WEB_RESEARCH` | `true` | `false` skips the web search pass (cheaper; RSS + prices only). |
| `SITE_URL` | Pages URL | Only needed for ntfy click-through on a custom domain. |

Feeds and instruments live in `brief/config.py`. Add a feed or ticker there and it flows through to the prompt and the app. A free CoinGecko demo key (secret `COINGECKO_API_KEY`) avoids rate limits.

## Cost

- **Claude API:** typically around $0.40–0.80 per brief with Opus 5.5 and web research on (roughly 40–90k input tokens, 5–15k output tokens, up to 6 searches). That's about $12–25 a month. Each run logs its exact token usage.
- **GitHub Actions and Pages:** free for public repositories. Skipped runs exit in about 10 seconds.
- **Market data:** free sources (Yahoo Finance chart endpoint, CoinGecko, US Treasury). They're unofficial and can change. A failed source is logged and skipped, and the brief notes the gap instead of failing.

## If something goes wrong

- **No notification, but the run succeeded:** check that the app was opened from the Home Screen, and that notifications for *Brief* are on in iOS Settings. Then redo step 6.
- **Run failed:** GitHub emails you, and the app sends a "Morning brief failed" notification. Open the run log in the Actions tab.
- **Scheduled runs stopped:** GitHub pauses schedules in repos with 60 days of no activity. The daily commit normally prevents this; re-enable it from the Actions tab if needed.

## Develop locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                                   # unit + integration tests, no network needed

python -m brief build-site --sample --out _site          # preview the app with a sample brief
python -m http.server -d _site 8000                      # → http://localhost:8000

export ANTHROPIC_API_KEY=...                             # a real run (writes to data/)
python -m brief generate --print-prompt
```

| Path | |
|---|---|
| `brief/config.py` | settings, feeds, instruments |
| `brief/schedule.py` | daily gate and "since last brief" state (stdlib only) |
| `brief/news.py`, `brief/markets.py` | data collection |
| `brief/models.py` | the brief's JSON schema |
| `brief/analyst.py` | prompts and Claude calls |
| `brief/notify.py` | Web Push and ntfy |
| `web/` | the iPhone web app (HTML/CSS/JS, service worker, manifest, icons) |
| `data/` | generated briefs and state (committed by the workflow) |
