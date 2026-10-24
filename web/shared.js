/* Shared by the page (app.js) and the service worker (sw.js, via importScripts):
   a tiny key-value store in IndexedDB, and the time zone sync that keeps the
   morning brief at 7:30 wherever the phone is.

   The sync writes the phone's IANA time zone to the repository variable
   BRIEF_TIMEZONE, which the scheduled workflow reads on every check. It runs
   when the app opens and whenever a push arrives, using a fine-grained GitHub
   token (this repository only, "Variables: read and write") saved in the app. */
(function (scope) {
  const DB = "brief";
  const STORE = "kv";

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function withStore(mode, action) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = action(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
    });
  }

  const get = (key) => withStore("readonly", (s) => s.get(key));
  const set = (key, value) => withStore("readwrite", (s) => s.put(value, key));
  const remove = (key) => withStore("readwrite", (s) => s.delete(key));
  const deviceTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

  async function syncTimeZone({ force = false } = {}) {
    const [token, repo, synced] = await Promise.all([get("githubToken"), get("repo"), get("syncedTimeZone")]);
    const tz = deviceTimeZone();
    if (!token || !repo) return { status: "off", tz };
    if (!force && synced === tz) return { status: "unchanged", tz };

    const base = `https://api.github.com/repos/${repo}/actions/variables`;
    const headers = {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
    const body = JSON.stringify({ name: "BRIEF_TIMEZONE", value: tz });
    let res = await fetch(`${base}/BRIEF_TIMEZONE`, { method: "PATCH", headers, body });
    if (res.status === 404) res = await fetch(base, { method: "POST", headers, body }); // first sync creates it
    if (!res.ok) return { status: "error", tz, code: res.status };
    await Promise.all([set("syncedTimeZone", tz), set("syncedAt", new Date().toISOString())]);
    return { status: "synced", tz };
  }

  scope.BriefShared = { get, set, remove, deviceTimeZone, syncTimeZone };
})(self);
