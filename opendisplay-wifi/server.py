"""OpenDisplay Wi-Fi Home Assistant add-on server.

Runs an OpenDisplay Wi-Fi server and provides a web UI via Ingress
for managing connected screens and uploading images.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from urllib.request import urlopen

from aiohttp import web
from PIL import Image

from opendisplay.wifi import DEFAULT_PORT, OpenDisplayServer
from opendisplay.wifi.imaging import image_to_1bpp
from opendisplay.wifi.protocol import DisplayAnnouncement

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path("/data")
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ASSIGNMENTS_FILE = DATA_DIR / "assignments.json"

# State: tracks connected screens and their image assignments
# Key: (width, height, colour_scheme) tuple from DisplayAnnouncement
# Value: dict with screen info
screens: dict[tuple[int, int, int], dict] = {}

# Image assignments per screen key
# Value: {"type": "file"|"url", "source": str, "poll_interval": int}
assignments: dict[tuple[int, int, int], dict] = {}

# Cache of converted images per screen resolution
image_cache: dict[tuple[int, int], bytes] = {}
url_pixel_hashes: dict[tuple[int, int], str] = {}

# Track the last announcement per screen for the provider
last_announcements: dict[tuple[int, int, int], DisplayAnnouncement] = {}


def _save_assignments() -> None:
    """Persist assignments to disk so they survive restarts."""
    serializable = {}
    for key, value in assignments.items():
        screen_id = _screen_id(key)
        serializable[screen_id] = value
    ASSIGNMENTS_FILE.write_text(json.dumps(serializable, indent=2))


def _load_assignments() -> None:
    """Load assignments from disk on startup."""
    if not ASSIGNMENTS_FILE.exists():
        return
    try:
        data = json.loads(ASSIGNMENTS_FILE.read_text())
        for screen_id, value in data.items():
            key = _key_from_id(screen_id)
            if key is not None:
                assignments[key] = value
        _LOGGER.info("Loaded %d saved assignments", len(assignments))
    except Exception:
        _LOGGER.exception("Failed to load saved assignments")


def _screen_key(ann: DisplayAnnouncement) -> tuple[int, int, int]:
    return (ann.width, ann.height, ann.colour_scheme)


def _screen_id(key: tuple[int, int, int]) -> str:
    return f"{key[0]}x{key[1]}_cs{key[2]}"


def _key_from_id(screen_id: str) -> tuple[int, int, int] | None:
    """Parse a screen_id like '800x600_cs0' back to a key tuple."""
    try:
        dims, cs = screen_id.rsplit("_cs", 1)
        w, h = dims.split("x")
        return (int(w), int(h), int(cs))
    except (ValueError, AttributeError):
        return None


def image_provider(announcement: DisplayAnnouncement | None) -> bytes | None:
    """Provide images to connected displays based on assignments."""
    if announcement is None:
        return None

    key = _screen_key(announcement)

    # Track this screen
    if key not in screens:
        screens[key] = {
            "width": announcement.width,
            "height": announcement.height,
            "colour_scheme": announcement.colour_scheme,
            "firmware_id": announcement.firmware_id,
            "firmware_version": announcement.firmware_version,
        }
    screens[key]["last_seen"] = time.time()
    last_announcements[key] = announcement

    assignment = assignments.get(key)
    if assignment is None:
        return None

    width = announcement.width
    height = announcement.height
    res_key = (width, height)

    source = assignment["source"]
    source_type = assignment["type"]

    if source_type == "url":
        try:
            _LOGGER.info("Fetching URL: %s", source)
            raw = urlopen(source, timeout=30).read()  # noqa: S310
            img = Image.open(io.BytesIO(raw))
            pixel_hash = hashlib.sha256(img.tobytes()).hexdigest()[:16]

            if url_pixel_hashes.get(res_key) == pixel_hash:
                return image_cache.get(res_key)

            data = image_to_1bpp(img, width, height)
            image_cache[res_key] = data
            url_pixel_hashes[res_key] = pixel_hash
            return data
        except Exception:
            _LOGGER.exception("Failed to fetch URL: %s", source)
            return image_cache.get(res_key)

    # Local file
    try:
        cache_key_str = f"{source}_{width}x{height}"
        cache_hash = hashlib.sha256(cache_key_str.encode()).hexdigest()[:16]
        if cache_hash in image_cache:
            return image_cache[cache_hash]

        img = Image.open(source)
        data = image_to_1bpp(img, width, height)
        image_cache[cache_hash] = data
        return data
    except Exception:
        _LOGGER.exception("Failed to load file: %s", source)
        return None


# --- Web UI routes ---


def _ingress_path() -> str:
    """Get the ingress path prefix from environment."""
    # Home Assistant sets this for ingress-enabled add-ons
    return os.environ.get("INGRESS_PATH", "")


async def handle_index(request: web.Request) -> web.Response:
    """Serve the main UI page."""
    ingress = _ingress_path()
    template = (Path(__file__).parent / "templates" / "index.html").read_text()
    template = template.replace("{{INGRESS_PATH}}", ingress)
    return web.Response(text=template, content_type="text/html")


async def handle_api_screens(request: web.Request) -> web.Response:
    """Return list of connected screens and their assignments."""
    now = time.time()
    result = []
    for key, info in screens.items():
        screen_id = _screen_id(key)
        assignment = assignments.get(key)
        result.append({
            "id": screen_id,
            "width": info["width"],
            "height": info["height"],
            "colour_scheme": info["colour_scheme"],
            "firmware_id": info.get("firmware_id", 0),
            "firmware_version": info.get("firmware_version", 0),
            "last_seen_seconds_ago": round(now - info.get("last_seen", now)),
            "assignment": assignment,
        })
    return web.json_response(result)


async def handle_api_assign(request: web.Request) -> web.Response:
    """Assign an image to a screen."""
    data = await request.json()
    screen_id = data.get("screen_id")
    source = data.get("source", "").strip()
    poll_interval = data.get("poll_interval", 5)

    if not screen_id or not source:
        return web.json_response({"error": "screen_id and source are required"}, status=400)

    key = _key_from_id(screen_id)
    if key is None:
        return web.json_response({"error": "Invalid screen_id"}, status=400)

    is_url = source.startswith("http://") or source.startswith("https://")

    assignments[key] = {
        "type": "url" if is_url else "file",
        "source": source,
        "poll_interval": int(poll_interval),
    }

    # Clear caches for this resolution so new image is picked up
    res_key = (key[0], key[1])
    image_cache.pop(res_key, None)
    url_pixel_hashes.pop(res_key, None)
    # Also clear file cache entries
    cache_key_str = f"{source}_{key[0]}x{key[1]}"
    cache_hash = hashlib.sha256(cache_key_str.encode()).hexdigest()[:16]
    image_cache.pop(cache_hash, None)

    _save_assignments()
    _LOGGER.info("Assigned %s to screen %s", source, screen_id)
    return web.json_response({"ok": True})


async def handle_api_unassign(request: web.Request) -> web.Response:
    """Remove image assignment from a screen."""
    data = await request.json()
    screen_id = data.get("screen_id")
    if not screen_id:
        return web.json_response({"error": "screen_id is required"}, status=400)

    key = _key_from_id(screen_id)
    if key is None:
        return web.json_response({"error": "Invalid screen_id"}, status=400)

    assignments.pop(key, None)
    _save_assignments()
    _LOGGER.info("Unassigned screen %s", screen_id)
    return web.json_response({"ok": True})


async def handle_api_upload(request: web.Request) -> web.Response:
    """Handle image file upload."""
    reader = await request.multipart()
    field = await reader.next()

    if field is None or field.name != "image":
        return web.json_response({"error": "No image field in upload"}, status=400)

    filename = field.filename or "upload.png"
    # Sanitize filename
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    if not safe_name:
        safe_name = "upload.png"

    dest = UPLOAD_DIR / safe_name
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            f.write(chunk)

    _LOGGER.info("Uploaded %s (%d bytes)", safe_name, size)
    return web.json_response({"ok": True, "path": str(dest)})


async def handle_api_uploads(request: web.Request) -> web.Response:
    """List uploaded files."""
    files = []
    if UPLOAD_DIR.exists():
        for p in sorted(UPLOAD_DIR.iterdir()):
            if p.is_file():
                files.append({"name": p.name, "path": str(p)})
    return web.json_response(files)


async def run() -> None:
    """Start the OpenDisplay server and web UI."""
    # Read add-on options
    options_path = Path("/data/options.json")
    if options_path.exists():
        options = json.loads(options_path.read_text())
    else:
        options = {}

    _load_assignments()

    poll_interval = options.get("poll_interval", 300)
    od_port = options.get("opendisplay_port", DEFAULT_PORT)

    # Start OpenDisplay WiFi server
    od_server = OpenDisplayServer(
        port=od_port,
        image_provider=image_provider,
        poll_interval=poll_interval,
        mdns=True,
    )
    await od_server.start()
    _LOGGER.info("OpenDisplay server started on port %d", od_server.actual_port)

    # Set up web UI
    app = web.Application()
    ingress = _ingress_path()

    # Register routes with and without ingress prefix
    prefixes = [""]
    if ingress:
        prefixes.append(ingress)
    for prefix in prefixes:
        app.router.add_get(f"{prefix}/", handle_index)
        app.router.add_get(f"{prefix}/api/screens", handle_api_screens)
        app.router.add_post(f"{prefix}/api/assign", handle_api_assign)
        app.router.add_post(f"{prefix}/api/unassign", handle_api_unassign)
        app.router.add_post(f"{prefix}/api/upload", handle_api_upload)
        app.router.add_get(f"{prefix}/api/uploads", handle_api_uploads)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    _LOGGER.info("Web UI started on port 8099")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await od_server.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
