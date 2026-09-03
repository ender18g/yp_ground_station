const TILE_CACHE_NAME = "yp-demo-map-tiles-v1";
const TILE_HOSTS = new Set(["tile.openstreetmap.org", "services.arcgisonline.com", "mapservices.weather.noaa.gov"]);
const TRANSPARENT_TILE_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";

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

  try {
    const response = await fetch(request);
    if (response && (response.ok || response.type === "opaque")) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    return transparentTileResponse();
  }
}

function transparentTileResponse() {
  const bytes = Uint8Array.from(atob(TRANSPARENT_TILE_BASE64), (char) => char.charCodeAt(0));
  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "image/png",
      "Cache-Control": "no-store",
    },
  });
}
