const CACHE = "georisk-shell-v3";
const SHELL = ["/", "/pricing", "/enterprise", "/risk-assessments", "/grok-ops", "/briefing", "/reports", "/static/platform.css", "/static/reports.js", "/static/ra-diff-audit.js", "/static/logo.png", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(event.request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
      return response;
    }).catch(() => caches.match(event.request))
  );
});
