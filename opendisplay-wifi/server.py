"""OpenDisplay Wi-Fi Home Assistant add-on server.

Runs an OpenDisplay Wi-Fi server and provides a web UI via Ingress
for managing connected screens, albums, and uploading images.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

from aiohttp import web
from PIL import Image

from opendisplay.wifi import DEFAULT_PORT, OpenDisplayServer
from opendisplay.wifi.imaging import image_to_1bpp
from opendisplay.wifi.protocol import DisplayAnnouncement
from opendisplay.encoding.images import fit_image
from opendisplay.models.enums import FitMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path("/data")
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ASSIGNMENTS_FILE = DATA_DIR / "assignments.json"
ALBUMS_FILE = DATA_DIR / "albums.json"

# Display poll interval - kept short so UI changes are reflected quickly
DISPLAY_POLL_INTERVAL = 30

# --- State ---

# Connected screens: key = (width, height, colour_scheme)
screens: dict[tuple[int, int, int], dict] = {}

# Assignments per screen key
# {"type": "image"|"album", "source": str|album_id, "fit": "contain"|"cover",
#  "poll_interval": int (for URL images only)}
assignments: dict[tuple[int, int, int], dict] = {}

# Albums: id -> {"id", "name", "images": [{"type","source"}], "transition_interval", "shuffle"}
albums: dict[str, dict] = {}

# Album playback state per screen key (not persisted - resets on restart)
# {"current_index": int, "last_transition": float, "order": list[int]}
album_state: dict[tuple[int, int, int], dict] = {}

# Image cache
image_cache: dict[str, bytes] = {}
url_pixel_hashes: dict[str, str] = {}


# --- Persistence ---


def _save_assignments() -> None:
    serializable = {_screen_id(k): v for k, v in assignments.items()}
    ASSIGNMENTS_FILE.write_text(json.dumps(serializable, indent=2))


def _load_assignments() -> None:
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


def _save_albums() -> None:
    ALBUMS_FILE.write_text(json.dumps(albums, indent=2))


def _load_albums() -> None:
    if not ALBUMS_FILE.exists():
        return
    try:
        albums.update(json.loads(ALBUMS_FILE.read_text()))
        _LOGGER.info("Loaded %d saved albums", len(albums))
    except Exception:
        _LOGGER.exception("Failed to load saved albums")


# --- Helpers ---


def _screen_key(ann: DisplayAnnouncement) -> tuple[int, int, int]:
    return (ann.width, ann.height, ann.colour_scheme)


def _screen_id(key: tuple[int, int, int]) -> str:
    return f"{key[0]}x{key[1]}_cs{key[2]}"


def _key_from_id(screen_id: str) -> tuple[int, int, int] | None:
    try:
        dims, cs = screen_id.rsplit("_cs", 1)
        w, h = dims.split("x")
        return (int(w), int(h), int(cs))
    except (ValueError, AttributeError):
        return None


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _load_image(source: str) -> Image.Image | None:
    """Load an image from a file path or URL."""
    if _is_url(source):
        try:
            raw = urlopen(source, timeout=30).read()  # noqa: S310
            return Image.open(io.BytesIO(raw))
        except Exception:
            _LOGGER.exception("Failed to fetch URL: %s", source)
            return None
    else:
        try:
            return Image.open(source)
        except Exception:
            _LOGGER.exception("Failed to load file: %s", source)
            return None


def _convert_image(img: Image.Image, width: int, height: int, fit: str) -> bytes:
    """Convert a PIL image to 1bpp with the given fit mode."""
    fit_mode = FitMode.COVER if fit == "cover" else FitMode.CONTAIN
    fitted = fit_image(img, (width, height), fit_mode)

    from epaper_dithering import MONO_4_26, DitherMode, dither_image
    dithered = dither_image(fitted, MONO_4_26, mode=DitherMode.FLOYD_STEINBERG)
    return dithered.convert("1").tobytes("raw", "1")


def _get_album_current_image(key: tuple[int, int, int], album: dict) -> dict | None:
    """Get the current image entry from an album for a screen, advancing if needed."""
    images = album.get("images", [])
    if not images:
        return None

    now = time.time()
    state = album_state.get(key)

    if state is None:
        # Initialize playback
        order = list(range(len(images)))
        if album.get("shuffle"):
            random.shuffle(order)
        album_state[key] = {
            "current_index": 0,
            "last_transition": now,
            "order": order,
        }
        state = album_state[key]

    # Check if it's time to transition
    interval = album.get("transition_interval", 60)
    elapsed = now - state["last_transition"]
    if elapsed >= interval and len(images) > 1:
        steps = int(elapsed // interval)
        state["current_index"] = (state["current_index"] + steps) % len(images)
        state["last_transition"] = now

        # Re-shuffle when we wrap around
        if album.get("shuffle") and state["current_index"] < steps:
            order = list(range(len(images)))
            random.shuffle(order)
            state["order"] = order

    idx = state["order"][state["current_index"] % len(state["order"])]
    return images[idx] if idx < len(images) else images[0]


def _resolve_source(assignment: dict, key: tuple[int, int, int]) -> tuple[str, str] | None:
    """Resolve an assignment to (source_path_or_url, source_type).

    Returns None if nothing to show.
    """
    if assignment["type"] == "album":
        album = albums.get(assignment["source"])
        if album is None:
            return None
        entry = _get_album_current_image(key, album)
        if entry is None:
            return None
        return (entry["source"], entry["type"])
    else:
        return (assignment["source"], assignment.get("source_type", "file"))


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

    assignment = assignments.get(key)
    if assignment is None:
        return None

    resolved = _resolve_source(assignment, key)
    if resolved is None:
        return None

    source, source_type = resolved
    width = announcement.width
    height = announcement.height
    fit = assignment.get("fit", "contain")
    cache_key = f"{source}_{width}x{height}_{fit}"

    if source_type == "url" or _is_url(source):
        img = _load_image(source)
        if img is None:
            return image_cache.get(cache_key)

        pixel_hash = hashlib.sha256(img.tobytes()).hexdigest()[:16]
        hash_key = f"{cache_key}_hash"
        if url_pixel_hashes.get(hash_key) == pixel_hash and cache_key in image_cache:
            return image_cache[cache_key]

        data = _convert_image(img, width, height, fit)
        image_cache[cache_key] = data
        url_pixel_hashes[hash_key] = pixel_hash
        return data

    # Local file
    if cache_key in image_cache:
        return image_cache[cache_key]

    img = _load_image(source)
    if img is None:
        return None

    data = _convert_image(img, width, height, fit)
    image_cache[cache_key] = data
    return data


# --- Web UI routes ---


def _ingress_path() -> str:
    return os.environ.get("INGRESS_PATH", "")


async def handle_index(request: web.Request) -> web.Response:
    ingress = _ingress_path()
    template = (Path(__file__).parent / "templates" / "index.html").read_text()
    template = template.replace("{{INGRESS_PATH}}", ingress)
    return web.Response(text=template, content_type="text/html")


async def handle_api_screens(request: web.Request) -> web.Response:
    now = time.time()
    result = []
    for key, info in screens.items():
        screen_id = _screen_id(key)
        assignment = assignments.get(key)
        entry = None
        if assignment:
            entry = dict(assignment)
            # For album assignments, include current image info
            if assignment["type"] == "album":
                album = albums.get(assignment["source"])
                if album:
                    entry["album_name"] = album["name"]
                    current = _get_album_current_image(key, album)
                    if current:
                        entry["current_source"] = current["source"]
        result.append({
            "id": screen_id,
            "width": info["width"],
            "height": info["height"],
            "colour_scheme": info["colour_scheme"],
            "firmware_id": info.get("firmware_id", 0),
            "firmware_version": info.get("firmware_version", 0),
            "last_seen_seconds_ago": round(now - info.get("last_seen", now)),
            "assignment": entry,
        })
    return web.json_response(result)


async def handle_api_assign(request: web.Request) -> web.Response:
    data = await request.json()
    screen_id = data.get("screen_id")
    assign_type = data.get("type", "image")  # "image" or "album"
    source = data.get("source", "").strip()
    fit = data.get("fit", "contain")
    poll_interval = data.get("poll_interval", 5)

    if not screen_id or not source:
        return web.json_response({"error": "screen_id and source required"}, status=400)

    key = _key_from_id(screen_id)
    if key is None:
        return web.json_response({"error": "Invalid screen_id"}, status=400)

    if fit not in ("contain", "cover"):
        fit = "contain"

    if assign_type == "album":
        if source not in albums:
            return web.json_response({"error": "Album not found"}, status=404)
        assignments[key] = {"type": "album", "source": source, "fit": fit}
        # Reset album playback state for this screen
        album_state.pop(key, None)
    else:
        is_url = _is_url(source)
        assignments[key] = {
            "type": "image",
            "source": source,
            "source_type": "url" if is_url else "file",
            "fit": fit,
            "poll_interval": int(poll_interval),
        }

    # Clear image caches for this screen
    width, height = key[0], key[1]
    to_remove = [k for k in image_cache if f"_{width}x{height}_" in k]
    for k in to_remove:
        image_cache.pop(k, None)
        url_pixel_hashes.pop(f"{k}_hash", None)

    _save_assignments()
    _LOGGER.info("Assigned %s (%s) to screen %s [fit=%s]", source, assign_type, screen_id, fit)
    return web.json_response({"ok": True})


async def handle_api_unassign(request: web.Request) -> web.Response:
    data = await request.json()
    screen_id = data.get("screen_id")
    if not screen_id:
        return web.json_response({"error": "screen_id required"}, status=400)

    key = _key_from_id(screen_id)
    if key is None:
        return web.json_response({"error": "Invalid screen_id"}, status=400)

    assignments.pop(key, None)
    album_state.pop(key, None)
    _save_assignments()
    return web.json_response({"ok": True})


# --- Album API ---


async def handle_api_albums(request: web.Request) -> web.Response:
    return web.json_response(list(albums.values()))


async def handle_api_album_create(request: web.Request) -> web.Response:
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    album_id = str(uuid.uuid4())[:8]
    albums[album_id] = {
        "id": album_id,
        "name": name,
        "images": data.get("images", []),
        "transition_interval": int(data.get("transition_interval", 60)),
        "shuffle": bool(data.get("shuffle", False)),
    }
    _save_albums()
    return web.json_response(albums[album_id])


async def handle_api_album_update(request: web.Request) -> web.Response:
    album_id = request.match_info["album_id"]
    if album_id not in albums:
        return web.json_response({"error": "Not found"}, status=404)

    data = await request.json()
    album = albums[album_id]
    if "name" in data:
        album["name"] = data["name"]
    if "images" in data:
        album["images"] = data["images"]
    if "transition_interval" in data:
        album["transition_interval"] = int(data["transition_interval"])
    if "shuffle" in data:
        album["shuffle"] = bool(data["shuffle"])

    # Reset playback state for any screen showing this album
    for key, assignment in assignments.items():
        if assignment.get("type") == "album" and assignment.get("source") == album_id:
            album_state.pop(key, None)
            # Clear image caches
            width, height = key[0], key[1]
            to_remove = [k for k in image_cache if f"_{width}x{height}_" in k]
            for k in to_remove:
                image_cache.pop(k, None)

    _save_albums()
    return web.json_response(album)


async def handle_api_album_delete(request: web.Request) -> web.Response:
    album_id = request.match_info["album_id"]
    if album_id not in albums:
        return web.json_response({"error": "Not found"}, status=404)

    # Unassign from any screens using this album
    to_remove = [k for k, v in assignments.items()
                 if v.get("type") == "album" and v.get("source") == album_id]
    for key in to_remove:
        assignments.pop(key, None)
        album_state.pop(key, None)

    del albums[album_id]
    _save_albums()
    _save_assignments()
    return web.json_response({"ok": True})


# --- Upload / file serving ---


async def handle_api_upload(request: web.Request) -> web.Response:
    post = await request.post()
    image = post.get("image")

    if image is None or not hasattr(image, "file"):
        return web.json_response({"error": "No image field"}, status=400)

    filename = image.filename or "upload.png"
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    if not safe_name:
        safe_name = "upload.png"

    dest = UPLOAD_DIR / safe_name
    content = image.file.read()
    dest.write_bytes(content)

    _LOGGER.info("Uploaded %s (%d bytes)", safe_name, len(content))
    return web.json_response({"ok": True, "path": str(dest), "name": safe_name})


async def handle_api_uploads(request: web.Request) -> web.Response:
    files = []
    if UPLOAD_DIR.exists():
        for p in sorted(UPLOAD_DIR.iterdir()):
            if p.is_file():
                files.append({"name": p.name, "path": str(p)})
    return web.json_response(files)


async def handle_upload_file(request: web.Request) -> web.Response:
    """Serve an uploaded file (for preview in the UI)."""
    filename = request.match_info["filename"]
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    path = UPLOAD_DIR / safe_name
    if not path.is_file():
        return web.Response(status=404)
    return web.FileResponse(path)


# --- Main ---


async def run() -> None:
    options_path = Path("/data/options.json")
    if options_path.exists():
        options = json.loads(options_path.read_text())
    else:
        options = {}

    _load_albums()
    _load_assignments()

    od_port = options.get("opendisplay_port", DEFAULT_PORT)

    od_server = OpenDisplayServer(
        port=od_port,
        image_provider=image_provider,
        poll_interval=DISPLAY_POLL_INTERVAL,
        mdns=True,
    )
    await od_server.start()
    _LOGGER.info("OpenDisplay server started on port %d", od_server.actual_port)

    app = web.Application(client_max_size=20 * 1024 * 1024)
    ingress = _ingress_path()

    prefixes = [""]
    if ingress:
        prefixes.append(ingress)

    for prefix in prefixes:
        app.router.add_get(f"{prefix}/", handle_index)
        app.router.add_get(f"{prefix}/api/screens", handle_api_screens)
        app.router.add_post(f"{prefix}/api/assign", handle_api_assign)
        app.router.add_post(f"{prefix}/api/unassign", handle_api_unassign)
        app.router.add_get(f"{prefix}/api/albums", handle_api_albums)
        app.router.add_post(f"{prefix}/api/albums", handle_api_album_create)
        app.router.add_put(f"{prefix}/api/albums/{{album_id}}", handle_api_album_update)
        app.router.add_delete(f"{prefix}/api/albums/{{album_id}}", handle_api_album_delete)
        app.router.add_post(f"{prefix}/api/upload", handle_api_upload)
        app.router.add_get(f"{prefix}/api/uploads", handle_api_uploads)
        app.router.add_get(f"{prefix}/uploads/{{filename}}", handle_upload_file)

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
