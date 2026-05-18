const TILE_CACHE_NAME = "yp-demo-map-tiles-v1";
const TILE_HOSTS = new Set(["tile.openstreetmap.org", "services.arcgisonline.com"]);

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (!TILE_HOSTS.has(url.hostname)) {
    return;
  }

  event.respondWith(cacheFirstTile(event.request));
});

async function cacheFirstTile(request) {
  const cache = await caches.open(TILE_CACHE_NAME);
  const cached = await cache.match(request, { ignoreVary: true });
  if (cached) {
    return cached;
  }

  const response = await fetch(request);
  if (response && (response.ok || response.type === "opaque")) {
    await cache.put(request, response.clone());
  }
  return response;
}
