import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import main, tiles


class TileCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for name, value in (("TILE_CACHE_DIR", self.directory / "cache"),
                            ("TILE_DIR", self.directory / "offline")):
            patcher = patch.object(tiles, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.requests = []
        self.upstream_response = httpx.Response(200, content=b"tile image", headers={
            "content-type": "image/png", "etag": '"test-tile"', "cache-control": "max-age=3600",
        })
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        self.addCleanup(lambda: asyncio.run(upstream.aclose()))
        patcher = patch.object(tiles, "tile_http_client", upstream)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def upstream(self, request):
        self.requests.append(request)
        return self.upstream_response

    def test_provider_fetch_then_cached_hit_preserves_headers_and_content(self):
        for provider in ("osm", "earth"):
            with self.subTest(provider=provider):
                response = self.client.get(f"/tiles/{provider}/1/0/0.png")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"tile image")
                self.assertEqual(response.headers["x-tile-cache"], "miss")
                response = self.client.get(f"/tiles/{provider}/1/0/0.png")
                self.assertEqual(response.headers["x-tile-cache"], "hit")
        self.assertEqual(len(self.requests), 2)
        status = self.client.get("/api/tile-cache").json()
        self.assertEqual(status["tiles"], 2)
        self.assertEqual(status["bytes"], 2 * len(b"tile image"))

    def test_cache_only_and_invalid_requests_never_fetch(self):
        for route in ("cache", "earth-cache"):
            self.assertEqual(self.client.get(f"/tiles/{route}/1/0/0.png").headers["x-tile-cache"], "empty")
            self.assertEqual(self.client.get(f"/tiles/{route}/1/9/0.png").headers["x-tile-cache"], "invalid")
        self.assertEqual(self.client.get("/tiles/osm/1/9/0.png").status_code, 400)
        self.assertEqual(self.requests, [])

    def expire_tile(self):
        self.client.get("/tiles/osm/1/0/0.png")
        path = tiles.provider_metadata_path("osm", 1, 0, 0)
        metadata = json.loads(path.read_text())
        metadata.update(fetched_at=0, expires_at=0)
        path.write_text(json.dumps(metadata))

    def test_conditional_revalidation_retains_cached_bytes(self):
        self.expire_tile()
        self.upstream_response = httpx.Response(304, headers={"cache-control": "max-age=3600"})
        response = self.client.get("/tiles/osm/1/0/0.png")
        self.assertEqual(response.headers["x-tile-cache"], "revalidated")
        self.assertEqual(response.content, b"tile image")
        self.assertEqual(self.requests[-1].headers["if-none-match"], '"test-tile"')

    def test_provider_failure_serves_stale_cache_or_fallback(self):
        self.expire_tile()
        self.upstream_response = httpx.Response(503)
        response = self.client.get("/tiles/osm/1/0/0.png")
        self.assertEqual(response.headers["x-tile-cache"], "stale")
        self.assertEqual(response.content, b"tile image")
        response = self.client.get("/tiles/osm/1/1/0.png")
        self.assertEqual(response.headers["x-tile-cache"], "unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_preprovisioned_offline_tiles_remain_available(self):
        path = tiles.TILE_DIR / "1/0/0.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"offline tile")
        self.assertEqual(self.client.get("/tiles/1/0/0.png").content, b"offline tile")
        self.assertEqual(self.client.get("/tiles/1/1/0.png").status_code, 404)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
