/* GeoRiskIntel offline shell.
   GET /api/* is network-first, then the last cached snapshot.
   Static CSS, JS, and icons are cache-first.
   POST requests and auth responses are never stored.
   The origin marks API responses no-store. That only bypasses the HTTP cache.
   The Cache API below is the explicit field snapshot. */
const SHELL_CACHE = "georiskintel-shell-v1";
const API_CACHE = "georiskintel-api-v1";
const SHELL = [
  "/",
  "/risk-assessments",
  "/sentinel-zones",
  "/reports",
  "/grok-ops",
  "/static/platform.css",
  "/static/pwa-register.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/logo.png",
  "/static/favicon.png",
  "/static/phase1-map.js",
  "/static/grok-stream.js",
  "/static/news-stream.js",
  "/static/ra-sectors.js",
  "/static/reports.js"
];

function isStaticAsset(url) {
  return url.origin === self.location.origin
    && url.pathname.startsWith("/static/")
    && /\.(?:css|js|png|webmanifest|svg|ico|woff2?)$/i.test(url.pathname);
}

function isSensitiveRequest(request, url) {
  if (request.method !== "GET") return true;
  if (request.headers.has("authorization")) return true;
  if (url.pathname === "/login" || url.pathname.startsWith("/login/")) return true;
  if (url.pathname === "/api/admin" || url.pathname.startsWith("/api/admin/")) return true;
  return false;
}

function isSensitiveResponse(response) {
  if (!response || !response.ok) return true;
  try {
    if (new URL(response.url).pathname === "/login") return true;
  } catch (error) {
    return true;
  }
  if (response.headers.has("www-authenticate")) return true;
  if (response.headers.has("set-cookie")) return true;
  const cacheControl = (response.headers.get("cache-control") || "").toLowerCase();
  return cacheControl.indexOf("private") !== -1;
}

async function remember(cacheName, request, response) {
  if (!isSensitiveResponse(response)) {
    try {
      const cache = await caches.open(cacheName);
      await cache.put(request, response.clone());
    } catch (error) {
      /* A cache write must not hide a successful network response. */
    }
  }
  return response;
}

async function cacheFirst(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    return remember(SHELL_CACHE, request, response);
  } catch (error) {
    return new Response("", { status: 504, statusText: "Offline" });
  }
}

async function networkFirst(request, cacheName, fallbackUrl) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    await remember(cacheName, request, response);
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    if (fallbackUrl) {
      const shell = await cache.match(fallbackUrl);
      if (shell) return shell;
    }
    const body = cacheName === API_CACHE
      ? JSON.stringify({ ok: false, offline: true, message: "Offline. No cached snapshot for this request." })
      : "";
    return new Response(body, {
      status: 503,
      headers: { "Content-Type": cacheName === API_CACHE ? "application/json" : "text/plain" }
    });
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await Promise.all(SHELL.map(async (url) => {
      try {
        const response = await fetch(url, { cache: "reload" });
        if (response && response.ok) await cache.put(url, response);
      } catch (error) {
        /* A missing shell file must not reject install. */
      }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, API_CACHE]);
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => !keep.has(key)).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isSensitiveRequest(request, url)) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(request, API_CACHE));
    return;
  }
  if (isStaticAsset(url)) {
    event.respondWith(cacheFirst(request));
    return;
  }
  if (request.mode === "navigate" || (request.headers.get("accept") || "").indexOf("text/html") !== -1) {
    event.respondWith(networkFirst(request, SHELL_CACHE, "/"));
  }
});
