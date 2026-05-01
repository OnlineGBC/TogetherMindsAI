const CACHE = "tmai-v3";
const PRECACHE = [
  "/",
  "/static/css/style.css",
  "/static/js/therapy.js",
  "/static/js/voice.js",
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

// Network-first for all requests (therapy sessions must be live)
// Falls back to cache only for static assets
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/")) {
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

  // Everything else (pages, API): network-first
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
