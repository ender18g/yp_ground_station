"""Offline map tiles and shared provider caching, independent of vehicle control."""
from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

TILE_DIR = Path(os.getenv("TILE_DIR", "/data/tiles"))
TILE_CACHE_DIR = Path(os.getenv("TILE_CACHE_DIR", "/data/tile-cache"))
OSM_TILE_URL = os.getenv("OSM_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
OSM_USER_AGENT = os.getenv("OSM_USER_AGENT", "YPGroundStation/0.1")
OSM_REFERER = os.getenv("OSM_REFERER", "http://localhost:8080/")
EARTH_TILE_URL = os.getenv("EARTH_TILE_URL", "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")
EARTH_USER_AGENT = os.getenv("EARTH_USER_AGENT", OSM_USER_AGENT)
EARTH_REFERER = os.getenv("EARTH_REFERER", OSM_REFERER)
MIN_TILE_TTL_SECONDS = int(os.getenv("MIN_TILE_TTL_SECONDS", str(7 * 24 * 60 * 60)))
TILE_MAX_CACHE_AGE_SECONDS = int(os.getenv("TILE_MAX_CACHE_AGE_SECONDS", str(365 * 24 * 60 * 60)))
MAX_TILE_ZOOM = int(os.getenv("MAX_TILE_ZOOM", "20"))
FALLBACK_TILE_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect width="256" height="256" fill="#dbeafe"/></svg>"""
KNOWN_BLOCKED_TILE_SHA1 = {
    "0cfb5f443183efc5921f61005aaa7f341fcfd143",
}

router = APIRouter()
tile_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
tile_http_client: Optional[httpx.AsyncClient] = None


@router.on_event("startup")
async def startup() -> None:
    global tile_http_client
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tile_http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)


@router.on_event("shutdown")
async def shutdown() -> None:
    global tile_http_client
    if tile_http_client:
        await tile_http_client.aclose()
        tile_http_client = None


@router.get("/api/tile-cache")
async def tile_cache_status() -> dict[str, Any]:
    """Report tile cache disk usage and settings per map provider."""
    providers = {
        "osm": {
            "name": "OpenStreetMap",
            "source_url": OSM_TILE_URL,
        },
        "earth": {
            "name": "Earth View",
            "source_url": EARTH_TILE_URL,
        },
    }
    provider_status = {}
    total_tiles = 0
    total_bytes = 0
    for provider, details in providers.items():
        files = list((TILE_CACHE_DIR / provider).glob("*/*/*.png"))
        bytes_used = sum(path.stat().st_size for path in files)
        total_tiles += len(files)
        total_bytes += bytes_used
        provider_status[provider] = details | {"tiles": len(files), "bytes": bytes_used}
    return {
        "cache_dir": str(TILE_CACHE_DIR),
        "providers": provider_status,
        "tiles": total_tiles,
        "bytes": total_bytes,
        "min_ttl_seconds": MIN_TILE_TTL_SECONDS,
        "tile_max_cache_age_seconds": TILE_MAX_CACHE_AGE_SECONDS,
    }


@router.get("/tiles/{z}/{x}/{y}.png", response_model=None)
async def tiles(z: int, x: int, y: int):
    """Serve a pre-provisioned offline tile from TILE_DIR."""
    tile_path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    if not tile_path.is_file():
        return JSONResponse({"error": "offline tile not found"}, status_code=404)
    if hashlib.sha1(tile_path.read_bytes()).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
        return JSONResponse({"error": "offline tile is a known blocked placeholder"}, status_code=404)
    return FileResponse(tile_path, media_type="image/png")


@router.get("/tiles/osm/{z}/{x}/{y}.png", response_model=None)
async def cached_osm_tile(z: int, x: int, y: int):
    """Serve an OpenStreetMap tile, fetching and caching it if needed."""
    return await cached_provider_tile(
        provider="osm",
        source_name="openstreetmap",
        source_url=OSM_TILE_URL,
        user_agent=OSM_USER_AGENT,
        referer=OSM_REFERER,
        z=z,
        x=x,
        y=y,
    )


@router.get("/tiles/earth/{z}/{x}/{y}.png", response_model=None)
async def cached_earth_tile(z: int, x: int, y: int):
    """Serve a satellite imagery tile, fetching and caching it if needed."""
    return await cached_provider_tile(
        provider="earth",
        source_name="earth-view",
        source_url=EARTH_TILE_URL,
        user_agent=EARTH_USER_AGENT,
        referer=EARTH_REFERER,
        z=z,
        x=x,
        y=y,
    )


@router.get("/tiles/cache/{z}/{x}/{y}.png", response_model=None)
async def cache_only_tile(z: int, x: int, y: int):
    """Serve an OSM tile only if already cached; never fetch remotely."""
    return cache_only_provider_tile("osm", "openstreetmap", z, x, y)


@router.get("/tiles/earth-cache/{z}/{x}/{y}.png", response_model=None)
async def earth_cache_only_tile(z: int, x: int, y: int):
    """Serve a satellite tile only if already cached; never fetch remotely."""
    return cache_only_provider_tile("earth", "earth-view", z, x, y)


async def cached_provider_tile(
    provider: str,
    source_name: str,
    source_url: str,
    user_agent: str,
    referer: str,
    z: int,
    x: int,
    y: int,
):
    """Return a cached tile if fresh, else fetch, cache, and return it (with stale/fallback handling)."""
    validation_error = validate_tile_coordinates(z, x, y)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=400)

    cache_path = provider_tile_path(provider, z, x, y)
    metadata_path = provider_metadata_path(provider, z, x, y)
    lock = tile_locks[f"{provider}/{z}/{x}/{y}"]

    async with lock:
        metadata = read_tile_metadata(metadata_path)
        if is_usable_cached_tile(cache_path) and not tile_expired(metadata, cache_path):
            return tile_file_response(cache_path, metadata, cache_status="hit", source_name=source_name)

        result = await fetch_and_cache_tile(source_name, source_url, user_agent, referer, z, x, y, cache_path, metadata_path, metadata)
        if result:
            return result

        if is_usable_cached_tile(cache_path):
            stale_metadata = read_tile_metadata(metadata_path)
            return tile_file_response(cache_path, stale_metadata, cache_status="stale", source_name=source_name)

    return fallback_tile_response("unavailable")


def cache_only_provider_tile(provider: str, source_name: str, z: int, x: int, y: int):
    """Return a cached tile for the given provider, or a fallback placeholder."""
    validation_error = validate_tile_coordinates(z, x, y)
    if validation_error:
        return fallback_tile_response("invalid")

    cache_path = provider_tile_path(provider, z, x, y)
    metadata = read_tile_metadata(provider_metadata_path(provider, z, x, y))
    if is_usable_cached_tile(cache_path):
        return tile_file_response(cache_path, metadata, cache_status="hit", source_name=source_name)
    return fallback_tile_response("empty")


def validate_tile_coordinates(z: int, x: int, y: int) -> Optional[str]:
    """Return an error message if z/x/y are outside the valid slippy-map range, else None."""
    if z < 0 or z > MAX_TILE_ZOOM:
        return f"zoom must be between 0 and {MAX_TILE_ZOOM}"
    limit = 2**z
    if x < 0 or x >= limit or y < 0 or y >= limit:
        return "tile coordinates are outside the valid slippy-map range"
    return None


def provider_tile_path(provider: str, z: int, x: int, y: int) -> Path:
    """Return the on-disk cache path for a provider's tile image."""
    return TILE_CACHE_DIR / provider / str(z) / str(x) / f"{y}.png"


def provider_metadata_path(provider: str, z: int, x: int, y: int) -> Path:
    """Return the on-disk cache path for a provider's tile metadata JSON."""
    return TILE_CACHE_DIR / provider / str(z) / str(x) / f"{y}.json"


def read_tile_metadata(path: Path) -> dict[str, Any]:
    """Read and parse a tile's metadata JSON, returning {} if missing or invalid."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def write_tile_metadata(path: Path, metadata: dict[str, Any]) -> None:
    """Persist tile metadata JSON alongside the cached tile image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True))


def tile_expired(metadata: dict[str, Any], cache_path: Path) -> bool:
    """Return whether a cached tile should be revalidated against its source."""
    now = time.time()
    fetched_at = metadata.get("fetched_at")
    if fetched_at is None:
        try:
            fetched_at = cache_path.stat().st_mtime
        except OSError:
            fetched_at = 0
    if now - float(fetched_at) < float(TILE_MAX_CACHE_AGE_SECONDS):
        return False
    return now >= float(metadata.get("expires_at", 0))


def is_usable_cached_tile(path: Path) -> bool:
    """Return whether a cached tile file exists and isn't a known blocked placeholder."""
    if not path.is_file():
        return False
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest() not in KNOWN_BLOCKED_TILE_SHA1
    except Exception:
        return False


async def fetch_and_cache_tile(
    source_name: str,
    source_url: str,
    user_agent: str,
    referer: str,
    z: int,
    x: int,
    y: int,
    cache_path: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
):
    """Fetch a tile from its source, cache it to disk, and return the HTTP response, or None on failure."""
    if not tile_http_client:
        return None

    headers = {
        "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        "User-Agent": user_agent,
        "Referer": referer,
    }
    if metadata.get("etag"):
        headers["If-None-Match"] = str(metadata["etag"])
    if metadata.get("last_modified"):
        headers["If-Modified-Since"] = str(metadata["last_modified"])

    url = source_url.format(z=z, x=x, y=y)
    try:
        response = await tile_http_client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: {exc}")
        return None

    if response.status_code == 304 and is_usable_cached_tile(cache_path):
        refreshed = metadata | {"fetched_at": time.time(), "expires_at": tile_expires_at(response.headers)}
        write_tile_metadata(metadata_path, refreshed)
        return tile_file_response(cache_path, refreshed, cache_status="revalidated", source_name=source_name)

    if response.status_code != 200:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: HTTP {response.status_code}")
        return None

    content_type = response.headers.get("content-type", "")
    if "image" not in content_type:
        print(f"{source_name} tile fetch failed {z}/{x}/{y}: unexpected content-type {content_type}")
        return None

    data = response.content
    if hashlib.sha1(data).hexdigest() in KNOWN_BLOCKED_TILE_SHA1:
        print(f"{source_name} tile fetch blocked {z}/{x}/{y}: provider returned access-blocked placeholder")
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".tmp")
    temp_path.write_bytes(data)
    temp_path.replace(cache_path)

    new_metadata = {
        "source_url": url,
        "fetched_at": time.time(),
        "expires_at": tile_expires_at(response.headers),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "cache_control": response.headers.get("cache-control"),
        "content_type": content_type,
    }
    write_tile_metadata(metadata_path, new_metadata)
    return tile_file_response(cache_path, new_metadata, cache_status="miss", source_name=source_name)


def tile_expires_at(headers: httpx.Headers) -> float:
    """Compute a tile's cache expiry time from response headers, or a default TTL."""
    cache_control = headers.get("cache-control", "")
    for part in cache_control.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return time.time() + max(0, int(part.split("=", 1)[1]))
            except ValueError:
                pass

    expires = headers.get("expires")
    if expires:
        try:
            return email.utils.parsedate_to_datetime(expires).timestamp()
        except Exception:
            pass

    return time.time() + MIN_TILE_TTL_SECONDS


def tile_file_response(path: Path, metadata: dict[str, Any], cache_status: str, source_name: str) -> FileResponse:
    """Build a FileResponse for a cached tile with cache-status headers."""
    max_age = max(60, int(float(metadata.get("expires_at", time.time() + 60)) - time.time()))
    return FileResponse(
        path,
        media_type=str(metadata.get("content_type") or "image/png").split(";", 1)[0],
        headers={
            "Cache-Control": f"public, max-age={max_age}",
            "X-Tile-Cache": cache_status,
            "X-Tile-Source": source_name,
        },
    )


def fallback_tile_response(cache_status: str) -> Response:
    """Return a placeholder SVG tile for use when no cached or fetched tile is available."""
    return Response(
        content=FALLBACK_TILE_SVG,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-Tile-Cache": cache_status,
            "X-Tile-Source": "fallback",
        },
    )
