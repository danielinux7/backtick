// Backtick service worker.
// Cache-first for the static shell (HTML + JS + CSS + icons + lightweight-charts CDN).
// Network-only for /api/* — never cache mutable session/trade state.
// Bump CACHE_VERSION on every deploy so old shells are evicted.

// Bump on every meaningful frontend release — the install handler precaches
// under this key and activate evicts older keys, so visitors get the new
// assets on the next load.
const CACHE_VERSION = "5f922c4b84";
const CACHE = `backtick-shell-${CACHE_VERSION}`;
const SHELL = [
  "/",
  "/login",
  "/static/style.css",
  "/static/app.js",
  "/static/symbol-picker.js",
  "/static/pwa.js",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/favicon-32.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(SHELL).catch(() => {
        // Some shell URLs may 401 (redirect to /login) for logged-out users;
        // missing entries are OK — fetch will hit the network at runtime.
      }),
    ),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k.startsWith("backtick-shell-") && k !== CACHE)
          .map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // API calls: always network. We do NOT want to serve cached trade state.
  if (url.pathname.startsWith("/api/")) return;

  // External CDN assets (lightweight-charts, flatpickr): cache-first.
  // Same-origin static assets: cache-first.
  // HTML pages: network-first so the shell stays fresh, with a cache fallback
  // when offline.
  const isHTML = req.headers.get("accept")?.includes("text/html");

  if (isHTML) {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(req);
          const cache = await caches.open(CACHE);
          cache.put(req, fresh.clone());
          return fresh;
        } catch (_) {
          const hit = await caches.match(req);
          if (hit) return hit;
          return new Response("Offline", { status: 503 });
        }
      })(),
    );
    return;
  }

  event.respondWith(
    (async () => {
      const hit = await caches.match(req);
      if (hit) return hit;
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.status === 200 && fresh.type === "basic") {
          const cache = await caches.open(CACHE);
          cache.put(req, fresh.clone());
        }
        return fresh;
      } catch (_) {
        return new Response("Offline", { status: 503 });
      }
    })(),
  );
});
