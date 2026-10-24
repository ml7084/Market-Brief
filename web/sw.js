importScripts("shared.js");

// Replaced with a build id by `python -m brief build-site`, so each deploy refreshes the app shell.
const CACHE = "brief-__BUILD__";
const SHELL = ["./", "index.html", "styles.css", "shared.js", "app.js", "manifest.webmanifest",
  "icons/icon-192.png", "icons/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Briefs and config: network first so the morning's brief is never stale; cached copy when offline.
// App shell: cache first.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  const fresh = url.pathname.includes("/data/") || url.pathname.endsWith("/config.json");
  event.respondWith(fresh ? networkFirst(event.request) : cacheFirst(event.request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const cached = await cache.match(request, { ignoreSearch: true });
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  return cached || fetch(request);
}

// iOS revokes push permission from apps that receive a push without showing a notification,
// so always show one, even if the payload is unreadable.
self.addEventListener("push", (event) => {
  let msg = {};
  try {
    msg = event.data ? event.data.json() : {};
  } catch {
    msg = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    Promise.all([
      self.registration.showNotification(msg.title || "Market Brief", {
        body: msg.body || "Your morning brief is ready.",
        tag: msg.tag || "brief",
        icon: "icons/icon-192.png",
        data: { url: new URL(msg.url || "./", self.registration.scope).href },
      }),
      // Each morning's push is a chance to tell the schedule where the phone is now.
      self.BriefShared.syncTimeZone().catch(() => null),
    ]),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data?.url || self.registration.scope;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => w.url.startsWith(self.registration.scope));
      if (open) {
        open.postMessage({ type: "open", url: target });
        return open.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
