const $ = (sel) => document.querySelector(sel);

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const safeUrl = (url) => {
  try {
    const parsed = new URL(url);
    return /^https?:$/.test(parsed.protocol) ? parsed.href : "#";
  } catch {
    return "#";
  }
};

const ASSET = { us_equities: "US equities", bonds: "Bonds", brent_oil: "Brent oil", crypto_l1: "Crypto L1" };
const DIRECTION_TONE = { buy: "up", add: "up", trim: "down", sell: "down", avoid: "", hedge: "warn" };
const SENTIMENT_TONE = { bullish: "up", bearish: "down", mixed: "warn", neutral: "" };
const GROUPS = [["equities", "Equities"], ["rates", "Rates"], ["oil", "Oil & macro"], ["crypto", "Crypto"]];

let config = {};
let index = [];
let shownDate = null;

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

/* ---------- formatting ---------- */

const fmtDay = (isoDate) =>
  new Date(`${isoDate}T12:00:00Z`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });

const fmtDateTime = (iso) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function fmtLast(q) {
  if (q.kind === "yield") return `${q.last.toFixed(2)}%`;
  const digits = q.last >= 1 ? 2 : 4;
  return q.last.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtChange(kind, value) {
  if (value === null || value === undefined) return '<span class="na">—</span>';
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "na";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const abs = Math.abs(value);
  const text = kind === "yield" ? `${sign}${abs.toFixed(abs < 10 ? 1 : 0)}bp` : `${sign}${abs.toFixed(2)}%`;
  return `<span class="${cls}">${text}</span>`;
}

const pill = (text, tone) => `<span class="pill ${tone || ""}">${esc(text)}</span>`;
const bullets = (items) => (items?.length ? `<ul class="bullets">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "");

/* ---------- rendering ---------- */

function renderIdea(idea) {
  return `
    <article class="card idea">
      <div class="idea-head">
        <span class="rank">${esc(idea.rank)}</span>
        <span class="instrument">${esc(idea.instrument)}</span>
        ${pill(idea.direction, DIRECTION_TONE[idea.direction])}
      </div>
      <h3>${esc(idea.title)}</h3>
      <p>${esc(idea.thesis)}</p>
      <div class="facts">
        <span>${esc(ASSET[idea.asset_class] || idea.asset_class)}</span>
        <span><b>Horizon</b> ${esc(idea.horizon)}</span>
        <span><b>Conviction</b> ${esc(idea.conviction)}</span>
        <span><b>Size</b> ${esc(idea.sizing)}</span>
      </div>
      <div class="levels">
        <div class="level"><span>Entry</span>${esc(idea.entry)}</div>
        <div class="level"><span>Wrong if</span>${esc(idea.invalidation)}</div>
      </div>
      <details>
        <summary>Catalysts &amp; risks</summary>
        <div class="cols">
          <div><h4>Catalysts</h4>${bullets(idea.catalysts)}</div>
          <div><h4>Risks</h4>${bullets(idea.risks)}</div>
        </div>
      </details>
    </article>`;
}

function renderHeadline(h) {
  return `<li><a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener">${esc(h.title)}</a>
    <span class="src">${esc(h.source)} · ${esc(fmtDateTime(h.published))}</span></li>`;
}

function renderSection(section, headlines) {
  const related = section.headline_ids.map((id) => headlines.get(id)).filter(Boolean).slice(0, 3);
  return `
    <article class="card asset">
      <div class="asset-head">
        <h3>${esc(ASSET[section.asset_class] || section.asset_class)}</h3>
        ${pill(section.sentiment, SENTIMENT_TONE[section.sentiment])}
      </div>
      <p>${esc(section.summary)}</p>
      ${bullets(section.key_points)}
      ${related.length ? `<ul class="related headlines">${related.map(renderHeadline).join("")}</ul>` : ""}
    </article>`;
}

function renderMarkets(market) {
  const quotes = market?.quotes || [];
  const groups = GROUPS.filter(([key]) => quotes.some((q) => q.group === key));
  if (!groups.length) return '<p class="na">Market data was unavailable for this brief.</p>';
  const tabs = groups
    .map(([key, label], n) => `<button class="tab" role="tab" data-tab="${key}" aria-selected="${n === 0}">${label}</button>`)
    .join("");
  const panels = groups
    .map(([key], n) => {
      const rows = quotes
        .filter((q) => q.group === key)
        .map((q) => `<tr><td>${esc(q.label)}<small>${esc(q.symbol)}</small></td><td>${esc(fmtLast(q))}</td>
          <td>${fmtChange(q.kind, q.change_1d)}</td><td>${fmtChange(q.kind, q.change_1w)}</td>
          <td class="col-1m">${fmtChange(q.kind, q.change_1m)}</td></tr>`)
        .join("");
      const curve = key === "rates" ? renderCurve(market.treasury_curve) : "";
      return `<div role="tabpanel" data-panel="${key}" ${n === 0 ? "" : "hidden"}>
        <div class="table-wrap"><table class="quotes"><thead><tr><th>Instrument</th><th>Last</th><th>1D</th><th>1W</th><th class="col-1m">1M</th></tr></thead>
        <tbody>${rows}</tbody></table></div>${curve}</div>`;
    })
    .join("");
  return `<div class="tabs" role="tablist">${tabs}</div>${panels}`;
}

function renderCurve(curve) {
  if (!curve) return "";
  const points = Object.entries(curve.curve)
    .filter(([, v]) => v !== null)
    .map(([tenor, v]) => `${tenor.replace(" Yr", "Y").replace(" Mo", "M")} ${v.toFixed(2)}`)
    .join(" · ");
  const spread = curve.spread_2s10s_bp !== undefined ? ` · 2s10s ${curve.spread_2s10s_bp > 0 ? "+" : ""}${curve.spread_2s10s_bp}bp` : "";
  return `<p class="curve">Treasury curve (${esc(curve.date)}): ${esc(points)}${esc(spread)}</p>`;
}

function renderBrief(rec) {
  const b = rec.brief;
  const headlines = new Map((rec.headlines || []).map((h) => [h.id, h]));
  const reading = b.top_headline_ids.map((id) => headlines.get(id)).filter(Boolean);
  return `
    <section class="hero">
      <div class="eyebrow">${esc(fmtDay(rec.date))}</div>
      <h1>${esc(b.notification_title)}</h1>
      <p class="summary">${esc(b.market_summary)}</p>
      <div class="window">Covers news since ${esc(fmtDateTime(rec.window.since))}</div>
    </section>

    <section>
      <h2 class="section-title">Top 3 ideas today</h2>
      <div class="stack">${b.top_ideas.map(renderIdea).join("")}</div>
    </section>

    <section>
      <h2 class="section-title">Since the last brief</h2>
      <div class="card prose">
        <p>${esc(b.since_last)}</p>
        ${b.prior_ideas_review ? `<p><b>Previous ideas.</b> ${esc(b.prior_ideas_review)}</p>` : ""}
      </div>
    </section>

    <section>
      <h2 class="section-title">Markets</h2>
      <div class="card">${renderMarkets(rec.market)}</div>
    </section>

    <section>
      <h2 class="section-title">By asset class</h2>
      <div class="stack">${b.sections.map((s) => renderSection(s, headlines)).join("")}</div>
    </section>

    ${b.watchlist.length ? `
    <section>
      <h2 class="section-title">On the radar</h2>
      <div class="card"><ul class="events">${b.watchlist.map((e) => `<li><time>${esc(e.time)}</time><span>${esc(e.event)}</span></li>`).join("")}</ul></div>
    </section>` : ""}

    ${reading.length ? `
    <section>
      <h2 class="section-title">Worth reading</h2>
      <div class="card"><ul class="headlines">${reading.map(renderHeadline).join("")}</ul></div>
    </section>` : ""}`;
}

function wireTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== tab.dataset.tab));
    });
  });
}

async function load(date) {
  const app = $("#app");
  const valid = date && /^\d{4}-\d{2}-\d{2}$/.test(date);
  try {
    const rec = await getJSON(valid ? `data/briefs/${date}.json` : "data/latest.json");
    app.innerHTML = renderBrief(rec);
    shownDate = rec.date;
    wireTabs();
    const s = rec.sources || {};
    $("#meta").textContent =
      `Generated ${fmtDateTime(rec.generated_at)} · ${s.headlines_considered ?? "?"} headlines from ` +
      `${s.live_sources ?? "?"} of ${s.total_sources ?? "?"} sources${s.web_research ? " + web research" : ""}`;
    document.title = `Market Brief · ${fmtDay(rec.date)}`;
    window.scrollTo(0, 0);
  } catch (err) {
    app.innerHTML = `<div class="empty">No brief here yet. The first one appears after the workflow runs.<br><small>${esc(err.message)}</small></div>`;
  }
}

function navigate(date) {
  const url = date ? `?d=${date}` : location.pathname;
  history.pushState({ date }, "", url);
  load(date);
}

/* ---------- history ---------- */

function openHistory() {
  const current = new URLSearchParams(location.search).get("d") || index[0]?.date;
  $("#history-list").innerHTML = index.length
    ? index.map((e) => `<li><a href="?d=${esc(e.date)}" data-date="${esc(e.date)}" aria-current="${e.date === current}">
        ${esc(fmtDay(e.date))}<small>${esc(e.title)}</small></a></li>`).join("")
    : '<li class="na">No briefs yet.</li>';
  $("#history").showModal();
}

/* ---------- notifications ---------- */

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const pushCapable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

const SHARE_ICON = `<svg class="share-glyph" width="16" height="18" viewBox="0 0 16 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-label="Share"><path d="M8 13V1M4 5l4-4 4 4M3 8H1.5v10.5h13V8H13"/></svg>`;

function b64ToBytes(b64) {
  const padded = (b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function currentSubscription() {
  if (!pushCapable) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function renderSetup() {
  const box = $("#setup");
  const sub = await currentSubscription().catch(() => null);
  if (sub && Notification.permission === "granted") {
    box.hidden = true;
    return;
  }
  let html;
  if (isIOS && !standalone) {
    html = `<h3>Install on your iPhone</h3>
      <p>In Safari, tap Share ${SHARE_ICON} then <b>Add to Home Screen</b>. Open <b>Brief</b> from your Home Screen to turn on the morning notification.</p>`;
  } else if (!pushCapable) {
    html = `<h3>Notifications unavailable here</h3>
      <p>This browser doesn’t support web push. On iPhone, use Safari on iOS 16.4 or later and add the app to your Home Screen.</p>`;
  } else if (!config.vapidPublicKey) {
    html = `<h3>Push keys missing</h3>
      <p>Add the <code>VAPID_PUBLIC_KEY</code> repository variable on GitHub and re-run the workflow (see the README).</p>`;
  } else {
    html = `<h3>Turn on the morning notification</h3>
      <p>Get the market mood and today’s top three ideas on your lock screen every morning.</p>
      <button class="primary" id="enable-push" type="button">Enable notifications</button>`;
  }
  box.innerHTML = `<div class="card">${html}</div>`;
  box.hidden = false;
  $("#enable-push")?.addEventListener("click", enablePush);
}

async function enablePush() {
  // iOS only shows the permission prompt when it's requested directly from a tap.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    alert("Notifications are off for Brief. Turn them on in Settings → Notifications → Brief, then try again.");
    return;
  }
  const reg = await navigator.serviceWorker.ready;
  const key = b64ToBytes(config.vapidPublicKey);
  let sub = await reg.pushManager.getSubscription();
  const sameKey = sub?.options?.applicationServerKey &&
    new Uint8Array(sub.options.applicationServerKey).every((byte, i) => byte === key[i]);
  if (sub && !sameKey) {
    await sub.unsubscribe();
    sub = null;
  }
  try {
    sub ||= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  } catch (err) {
    alert(`Couldn't subscribe: ${err.message}`);
    return;
  }
  $("#setup").hidden = true;
  showSubscription(sub);
}

function showSubscription(sub) {
  $("#subscription-code").value = JSON.stringify(sub.toJSON());
  $("#copy-code").textContent = "Copy code";
  $("#subscription").showModal();
}

async function copyCode() {
  const area = $("#subscription-code");
  try {
    await navigator.clipboard.writeText(area.value);
  } catch {
    area.select();
    document.execCommand("copy");
  }
  $("#copy-code").textContent = "Copied ✓";
}

async function openSettings() {
  const sub = await currentSubscription().catch(() => null);
  if (sub) {
    showSubscription(sub);
  } else {
    await renderSetup();
    $("#setup").scrollIntoView({ behavior: "smooth" });
  }
}

/* ---------- boot ---------- */

async function init() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch((err) => console.warn("service worker:", err));
    // The service worker asks an open window to show the brief a notification points to.
    navigator.serviceWorker.addEventListener("message", (event) => {
      if (event.data?.type === "open") navigate(new URL(event.data.url).searchParams.get("d"));
    });
  }
  [config, index] = await Promise.all([getJSON("config.json").catch(() => ({})), getJSON("data/index.json").catch(() => [])]);

  await load(new URLSearchParams(location.search).get("d"));
  renderSetup();

  $("#history-open").addEventListener("click", openHistory);
  $("#history-list").addEventListener("click", (event) => {
    const link = event.target.closest("a[data-date]");
    if (!link) return;
    event.preventDefault();
    $("#history").close();
    navigate(link.dataset.date === index[0]?.date ? null : link.dataset.date);
  });
  $("#copy-code").addEventListener("click", copyCode);
  $("#settings-open").addEventListener("click", openSettings);
  addEventListener("popstate", () => load(new URLSearchParams(location.search).get("d")));
  // Coming back to the app in the morning should show the newest brief.
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible" || new URLSearchParams(location.search).get("d")) return;
    index = await getJSON("data/index.json").catch(() => index);
    if (index[0] && index[0].date !== shownDate) load(null);
  });
}

init();
