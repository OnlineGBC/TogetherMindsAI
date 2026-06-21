const CACHE = "tmai-__BUILD_VERSION__";
const PRECACHE = [
  "/",
  "/static/css/style.css",
  "/static/js/therapy.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);

  // Images / icons rarely change → cache-first (fast, offline-friendly).
  if (url.pathname.startsWith("/static/icons/") ||
      /\.(png|jpe?g|svg|ico|webp|gif)$/.test(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then((cached) =>
        cached || fetch(e.request).then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
          return res;
        })
      )
    );
    return;
  }

  // Everything else — code (JS/CSS), pages, API → NETWORK-FIRST. A new deploy is
  // never blocked by a stale cached copy; the cache is used only when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (url.pathname.startsWith("/static/")) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
