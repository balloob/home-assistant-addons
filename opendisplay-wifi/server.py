"""OpenDisplay Wi-Fi Home Assistant add-on server.

Runs an OpenDisplay Wi-Fi server and provides a web UI via Ingress
for managing connected screens, albums, and uploading images.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

from aiohttp import web
from PIL import Image

from opendisplay.wifi import DEFAULT_PORT, OpenDisplayServer
from opendisplay.wifi.protocol import DisplayAnnouncement
from opendisplay.encoding.images import fit_image
from opendisplay.models.enums import FitMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger(__name__)

LOCAL_DATA_DIR = Path.cwd() / "dev-data"
ADDON_DATA_DIR = Path("/data")
LOCAL_OPTIONS_FILE = LOCAL_DATA_DIR / "options-dev.json"
ADDON_OPTIONS_FILE = ADDON_DATA_DIR / "options.json"
OPTIONS_FILE = next(
    (path for path in (LOCAL_OPTIONS_FILE, ADDON_OPTIONS_FILE) if path.exists()),
    None,
)


def _load_options() -> dict:
    """Load the selected options file, defaulting to empty options."""
    if OPTIONS_FILE is None:
        _LOGGER.info("No options file found, using defaults")
        return {}

    _LOGGER.info("Loading options from %s", OPTIONS_FILE)

    try:
        raw = OPTIONS_FILE.read_text().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        _LOGGER.exception("Failed to load options from %s, using defaults", OPTIONS_FILE)
        return {}


IS_ADDON = OPTIONS_FILE == ADDON_OPTIONS_FILE
DATA_DIR = ADDON_DATA_DIR if IS_ADDON else LOCAL_DATA_DIR
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = DATA_DIR / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
ASSIGNMENTS_FILE = DATA_DIR / "assignments.json"
ALBUMS_FILE = DATA_DIR / "albums.json"
IMAGES_FILE = DATA_DIR / "images.json"

THUMB_MAX_SIZE = (200, 200)

# Display poll interval - kept short so UI changes are reflected quickly
DISPLAY_POLL_INTERVAL = 30
SLOW_OPERATION_LOG_THRESHOLD = 0.5

# --- State ---

# Connected screens: key = (width, height, colour_scheme)
screens: dict[tuple[int, int, int], dict] = {}
no_image_reasons: dict[tuple[int, int, int], str] = {}

# Assignments per screen key
# {"type": "image"|"album", "source": str|album_id, "fit": "contain"|"cover",
#  "poll_interval": int (for URL images only)}
assignments: dict[tuple[int, int, int], dict] = {}

# Albums: id -> {"id", "name", "images": [{"type","source"}], "transition_interval", "shuffle"}
albums: dict[str, dict] = {}
images: dict[str, dict] = {}

# Album playback state per screen key (not persisted - resets on restart)
# {"current_index": int, "last_transition": float, "order": list[int]}
album_state: dict[tuple[int, int, int], dict] = {}

# Image cache
image_cache: dict[str, bytes] = {}
url_pixel_hashes: dict[str, str] = {}
cache_lock = threading.Lock()
preprocess_lock = threading.Lock()
preprocess_tasks: dict[str, concurrent.futures.Future[None]] = {}
preprocess_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="opendisplay-preprocess",
)


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


def _save_images() -> None:
    IMAGES_FILE.write_text(json.dumps(images, indent=2))


def _load_albums() -> None:
    if not ALBUMS_FILE.exists():
        return
    try:
        albums.update(json.loads(ALBUMS_FILE.read_text()))
        changed = False
        for album in albums.values():
            images_in_album = album.get("images", [])
            normalized = _normalize_album_images(images_in_album)
            if normalized != images_in_album:
                album["images"] = normalized
                changed = True
        if changed:
            _save_albums()
        _LOGGER.info("Loaded %d saved albums", len(albums))
    except Exception:
        _LOGGER.exception("Failed to load saved albums")


def _load_images() -> None:
    if not IMAGES_FILE.exists():
        return
    try:
        raw = json.loads(IMAGES_FILE.read_text())
        if isinstance(raw, dict):
            images.update(raw)
        _LOGGER.info("Loaded %d saved images", len(images))
    except Exception:
        _LOGGER.exception("Failed to load saved images")


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


def _remember_no_image_reason(key: tuple[int, int, int], reason: str) -> None:
    if no_image_reasons.get(key) == reason:
        return
    no_image_reasons[key] = reason
    _LOGGER.info("%s", reason)


def _clear_no_image_reason(key: tuple[int, int, int]) -> None:
    no_image_reasons.pop(key, None)


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _log_source(source: str) -> str:
    if _is_url(source):
        return source
    return Path(source).name or source


def _log_duration(
    action: str,
    started: float,
    *,
    level: int | None = None,
    **details: object,
) -> float:
    elapsed = time.monotonic() - started
    if level is None:
        level = logging.INFO if elapsed >= SLOW_OPERATION_LOG_THRESHOLD else logging.DEBUG

    suffix = ""
    if details:
        detail_str = ", ".join(f"{key}={value}" for key, value in details.items())
        suffix = f" ({detail_str})"

    _LOGGER.log(level, "%s in %.2fs%s", action, elapsed, suffix)
    return elapsed


def _sanitize_filename(filename: str) -> str:
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    return safe_name or "upload.png"


def _make_unique_filename(filename: str) -> str:
    candidate = Path(_sanitize_filename(filename))
    stem = candidate.stem or "upload"
    suffix = candidate.suffix or ".png"
    counter = 1
    final_name = f"{stem}{suffix}"
    while (UPLOAD_DIR / final_name).exists():
        counter += 1
        final_name = f"{stem}-{counter}{suffix}"
    return final_name


def _image_thumb_path(image_id: str) -> Path:
    return THUMB_DIR / f"{image_id}.jpg"


def _image_display_name(item: dict) -> str:
    name = str(item.get("name", "")).strip()
    if name:
        return name
    if item.get("type") == "file":
        filename = item.get("filename")
        if filename:
            return Path(filename).stem
        return Path(str(item.get("source", ""))).stem
    return str(item.get("source", ""))


def _find_image(*, image_id: str | None = None, source: str | None = None) -> dict | None:
    if image_id:
        return images.get(image_id)
    if source:
        for item in images.values():
            if item.get("source") == source:
                return item
    return None


def _serialize_image(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "display_name": _image_display_name(item),
        "type": item["type"],
        "source": item["source"],
        "filename": item.get("filename"),
        "subtitle": item["source"] if item["type"] == "url" else item.get("filename", ""),
        "created_at": item.get("created_at", 0),
    }


def _write_thumbnail(img: Image.Image, thumb_path: Path) -> None:
    thumb = img.copy()
    thumb.thumbnail(THUMB_MAX_SIZE)
    thumb.convert("RGB").save(thumb_path, "JPEG", quality=80)


def _fetch_url_image(source: str, timeout: int = 60) -> Image.Image:
    with urlopen(source, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


def _load_image(source: str) -> Image.Image | None:
    """Load an image from a file path or URL."""
    started = time.monotonic()
    if _is_url(source):
        try:
            img = _fetch_url_image(source)
            _log_duration("Loaded URL image", started, source=_log_source(source))
            return img
        except Exception:
            _LOGGER.exception("Failed to fetch URL: %s", source)
            return None
    else:
        try:
            with Image.open(source) as img:
                img.load()
                loaded = img.copy()
            _log_duration("Loaded file image", started, source=_log_source(source))
            return loaded
        except Exception:
            _LOGGER.exception("Failed to load file: %s", source)
            return None


def _generate_thumbnail(source: Path) -> None:
    """Generate a legacy JPEG thumbnail for an uploaded image."""
    try:
        with Image.open(source) as img:
            img.load()
            _write_thumbnail(img, THUMB_DIR / (source.stem + ".jpg"))
    except Exception:
        _LOGGER.exception("Failed to generate thumbnail for %s", source.name)


def _generate_library_thumbnail(image_id: str, source: str) -> None:
    """Generate a JPEG thumbnail for an image library item."""
    img = _load_image(source)
    if img is None:
        raise ValueError(f"Unable to load image source {source}")
    _write_thumbnail(img, _image_thumb_path(image_id))


def _clear_caches_for_source(source: str) -> None:
    with cache_lock:
        to_remove = [key for key in image_cache if key.startswith(f"{source}_")]
        for key in to_remove:
            image_cache.pop(key, None)
            url_pixel_hashes.pop(_cache_hash_key(key), None)


def _clear_caches_for_screen(key: tuple[int, int, int]) -> None:
    width, height = key[0], key[1]
    with cache_lock:
        to_remove = [cache_key for cache_key in image_cache if f"_{width}x{height}_" in cache_key]
        for cache_key in to_remove:
            image_cache.pop(cache_key, None)
            url_pixel_hashes.pop(_cache_hash_key(cache_key), None)


def _sync_images() -> None:
    changed = False

    for image_id, item in list(images.items()):
        item.setdefault("id", image_id)
        item.setdefault("created_at", time.time())
        item_type = item.get("type")
        if item_type not in ("file", "url"):
            del images[image_id]
            changed = True
            continue
        if item_type == "file":
            filename = item.get("filename")
            if not filename:
                source = item.get("source")
                if source:
                    filename = Path(source).name
                    item["filename"] = filename
                    changed = True
            if filename:
                item["source"] = str(UPLOAD_DIR / filename)
            if not Path(str(item.get("source", ""))).is_file():
                del images[image_id]
                thumb_path = _image_thumb_path(image_id)
                if thumb_path.exists():
                    thumb_path.unlink()
                changed = True

    tracked_paths = {
        item["source"]
        for item in images.values()
        if item.get("type") == "file" and item.get("source")
    }
    for path in (sorted(UPLOAD_DIR.iterdir()) if UPLOAD_DIR.exists() else []):
        if not path.is_file():
            continue
        source = str(path)
        if source in tracked_paths:
            continue
        image_id = uuid.uuid4().hex[:8]
        images[image_id] = {
            "id": image_id,
            "name": path.stem,
            "type": "file",
            "source": source,
            "filename": path.name,
            "created_at": time.time(),
        }
        changed = True

    if changed:
        _save_images()


def _normalize_album_images(entries: list[dict]) -> list[dict]:
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        image_id = entry.get("image_id")
        item = _find_image(image_id=image_id) if image_id else None
        if item is None:
            source = str(entry.get("source", "")).strip()
            if not source:
                continue
            item = _find_image(source=source)
        if item is not None:
            normalized.append({
                "image_id": item["id"],
                "type": item["type"],
                "source": item["source"],
            })
            continue
        source = str(entry.get("source", "")).strip()
        if not source:
            continue
        normalized.append({
            "type": entry.get("type") or ("url" if _is_url(source) else "file"),
            "source": source,
        })
    return normalized


def _convert_image(img: Image.Image, width: int, height: int, fit: str) -> bytes:
    """Convert a PIL image to 1bpp with the given fit mode."""
    started = time.monotonic()
    fit_mode = FitMode.COVER if fit == "cover" else FitMode.CONTAIN

    fit_started = time.monotonic()
    fitted = fit_image(img, (width, height), fit_mode)
    _log_duration(
        "Fitted image",
        fit_started,
        width=width,
        height=height,
        fit=fit,
    )

    from epaper_dithering import MONO_4_26, DitherMode, dither_image

    dither_started = time.monotonic()
    dithered = dither_image(fitted, MONO_4_26, mode=DitherMode.FLOYD_STEINBERG)
    _log_duration(
        "Dithered image",
        dither_started,
        width=width,
        height=height,
        fit=fit,
    )

    pack_started = time.monotonic()
    data = dithered.convert("1").tobytes("raw", "1")
    _log_duration(
        "Packed image",
        pack_started,
        width=width,
        height=height,
        fit=fit,
        bytes=len(data),
    )
    _log_duration(
        "Converted image",
        started,
        width=width,
        height=height,
        fit=fit,
        bytes=len(data),
    )
    return data


def _cache_key(source: str, width: int, height: int, fit: str) -> str:
    return f"{source}_{width}x{height}_{fit}"


def _cache_hash_key(cache_key: str) -> str:
    return f"{cache_key}_hash"


def _get_cached_image(cache_key: str) -> bytes | None:
    with cache_lock:
        return image_cache.get(cache_key)


def _set_cached_image(cache_key: str, data: bytes, pixel_hash: str | None = None) -> None:
    with cache_lock:
        image_cache[cache_key] = data
        if pixel_hash is not None:
            url_pixel_hashes[_cache_hash_key(cache_key)] = pixel_hash


def _get_cached_pixel_hash(cache_key: str) -> str | None:
    with cache_lock:
        return url_pixel_hashes.get(_cache_hash_key(cache_key))


def _preprocess_image(
    cache_key: str,
    source: str,
    width: int,
    height: int,
    fit: str,
    source_type: str,
) -> None:
    started = time.monotonic()
    img = _load_image(source)
    if img is None:
        _LOGGER.warning("Unable to pre-process %s", source)
        return

    pixel_hash: str | None = None
    if source_type == "url" or _is_url(source):
        pixel_hash = hashlib.sha256(img.tobytes()).hexdigest()[:16]
        if (
            _get_cached_pixel_hash(cache_key) == pixel_hash
            and _get_cached_image(cache_key) is not None
        ):
            _log_duration(
                "Skipped image preprocessing",
                started,
                level=logging.INFO,
                source=_log_source(source),
                width=width,
                height=height,
                fit=fit,
                reason="unchanged",
            )
            return

    data = _convert_image(img, width, height, fit)
    _set_cached_image(cache_key, data, pixel_hash=pixel_hash)
    _log_duration(
        "Prepared cached image",
        started,
        level=logging.INFO,
        source=_log_source(source),
        width=width,
        height=height,
        fit=fit,
        type=source_type,
        bytes=len(data),
    )


def _on_preprocess_done(cache_key: str, future: concurrent.futures.Future[None]) -> None:
    with preprocess_lock:
        if preprocess_tasks.get(cache_key) is future:
            preprocess_tasks.pop(cache_key, None)

    try:
        future.result()
    except Exception:
        _LOGGER.exception("Failed to pre-process %s", cache_key)


def _schedule_preprocess(
    source: str,
    width: int,
    height: int,
    fit: str,
    source_type: str,
) -> None:
    cache_key = _cache_key(source, width, height, fit)

    with preprocess_lock:
        future = preprocess_tasks.get(cache_key)
        if future is not None and not future.done():
            return
        _LOGGER.info(
            "Queued image preprocessing for %s (%dx%d fit=%s type=%s)",
            _log_source(source),
            width,
            height,
            fit,
            source_type,
        )
        future = preprocess_executor.submit(
            _preprocess_image,
            cache_key,
            source,
            width,
            height,
            fit,
            source_type,
        )
        preprocess_tasks[cache_key] = future

    future.add_done_callback(lambda done, key=cache_key: _on_preprocess_done(key, done))


def _schedule_assignment_preprocess(
    key: tuple[int, int, int],
    assignment: dict,
) -> None:
    width, height = key[0], key[1]
    fit = assignment.get("fit", "contain")

    if assignment.get("type") == "album":
        album = albums.get(assignment.get("source", ""))
        if album is None:
            return

        current = _get_album_current_image(key, album)
        if current is None:
            return

        source = str(current.get("source", "")).strip()
        if not source:
            return
        source_type = str(current.get("type") or ("url" if _is_url(source) else "file"))
        _LOGGER.info(
            "Prewarming current album image for %s: %s",
            _screen_id(key),
            _log_source(source),
        )
        _schedule_preprocess(source, width, height, fit, source_type)
        return

    source = str(assignment.get("source", "")).strip()
    if not source:
        return
    source_type = str(assignment.get("source_type") or ("url" if _is_url(source) else "file"))
    _schedule_preprocess(source, width, height, fit, source_type)


def _warm_assignment_caches() -> None:
    for key, assignment in assignments.items():
        _schedule_assignment_preprocess(key, assignment)


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
            _remember_no_image_reason(
                key,
                f"No image for {_screen_id(key)}: album {assignment['source']} not found",
            )
            return None
        entry = _get_album_current_image(key, album)
        if entry is None:
            _remember_no_image_reason(
                key,
                f"No image for {_screen_id(key)}: album {album['name']} has no entries",
            )
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
        _remember_no_image_reason(
            key,
            f"No image for {_screen_id(key)}: no assignment configured",
        )
        return None

    resolved = _resolve_source(assignment, key)
    if resolved is None:
        return None

    source, source_type = resolved
    width = announcement.width
    height = announcement.height
    fit = assignment.get("fit", "contain")
    cache_key = _cache_key(source, width, height, fit)
    cached = _get_cached_image(cache_key)

    if source_type == "url" or _is_url(source):
        _schedule_preprocess(source, width, height, fit, source_type)
        if cached is None:
            _remember_no_image_reason(
                key,
                "No image for "
                f"{_screen_id(key)}: waiting for preprocessed cache of {_log_source(source)}",
            )
        else:
            _clear_no_image_reason(key)
        return cached

    if cached is not None:
        _clear_no_image_reason(key)
        return cached

    _schedule_preprocess(source, width, height, fit, source_type)
    _remember_no_image_reason(
        key,
        "No image for "
        f"{_screen_id(key)}: waiting for preprocessed cache of {_log_source(source)}",
    )
    return None


# --- Web UI routes ---


async def handle_index(request: web.Request) -> web.Response:
    template = (Path(__file__).parent / "templates" / "index.html").read_text()
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
                        current_image = _find_image(
                            image_id=current.get("image_id"),
                            source=current.get("source"),
                        )
                        if current_image:
                            entry["current_image_id"] = current_image["id"]
                            entry["current_image_name"] = _image_display_name(current_image)
            else:
                image = _find_image(
                    image_id=assignment.get("image_id"),
                    source=assignment.get("source"),
                )
                if image:
                    entry["image_name"] = _image_display_name(image)
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
    image_id = data.get("image_id")
    fit = data.get("fit", "contain")
    poll_interval = data.get("poll_interval", 5)

    if not screen_id:
        return web.json_response({"error": "screen_id required"}, status=400)

    key = _key_from_id(screen_id)
    if key is None:
        return web.json_response({"error": "Invalid screen_id"}, status=400)

    if fit not in ("contain", "cover"):
        fit = "contain"

    if assign_type == "album":
        if not source:
            return web.json_response({"error": "source required"}, status=400)
        if source not in albums:
            return web.json_response({"error": "Album not found"}, status=404)
        assignments[key] = {"type": "album", "source": source, "fit": fit}
        # Reset album playback state for this screen
        album_state.pop(key, None)
    else:
        image = _find_image(image_id=image_id) if image_id else None
        if image is not None:
            source = image["source"]
        if not source:
            return web.json_response({"error": "source required"}, status=400)
        if image is None:
            image = _find_image(source=source)
        is_url = image["type"] == "url" if image is not None else _is_url(source)
        assignments[key] = {
            "type": "image",
            "source": source,
            "image_id": image["id"] if image is not None else None,
            "source_type": "url" if is_url else "file",
            "fit": fit,
            "poll_interval": int(poll_interval),
        }

    _clear_caches_for_screen(key)
    _schedule_assignment_preprocess(key, assignments[key])

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
        "images": _normalize_album_images(data.get("images", [])),
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
        album["images"] = _normalize_album_images(data["images"])
    if "transition_interval" in data:
        album["transition_interval"] = int(data["transition_interval"])
    if "shuffle" in data:
        album["shuffle"] = bool(data["shuffle"])

    # Reset playback state for any screen showing this album
    for key, assignment in assignments.items():
        if assignment.get("type") == "album" and assignment.get("source") == album_id:
            album_state.pop(key, None)
            _clear_caches_for_screen(key)
            _schedule_assignment_preprocess(key, assignment)

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


async def handle_api_images(request: web.Request) -> web.Response:
    ordered = sorted(
        images.values(),
        key=lambda item: item.get("created_at", 0),
        reverse=True,
    )
    return web.json_response([_serialize_image(item) for item in ordered])


async def handle_api_upload(request: web.Request) -> web.Response:
    post = await request.post()
    image = post.get("image")

    if image is None or not hasattr(image, "file"):
        return web.json_response({"error": "No image field"}, status=400)

    safe_name = _make_unique_filename(image.filename or "upload.png")
    dest = UPLOAD_DIR / safe_name
    content = image.file.read()
    dest.write_bytes(content)

    image_id = uuid.uuid4().hex[:8]
    item = {
        "id": image_id,
        "name": Path(safe_name).stem,
        "type": "file",
        "source": str(dest),
        "filename": safe_name,
        "created_at": time.time(),
    }

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _generate_library_thumbnail, image_id, str(dest))
    except Exception as err:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": f"Failed to process image: {err}"}, status=400)

    images[image_id] = item
    _save_images()

    _LOGGER.info("Uploaded %s (%d bytes)", safe_name, len(content))
    return web.json_response({"ok": True, "image": _serialize_image(item)})


async def handle_api_image_url(request: web.Request) -> web.Response:
    data = await request.json()
    source = str(data.get("url", "")).strip()
    name = str(data.get("name", "")).strip()

    if not source:
        return web.json_response({"error": "url required"}, status=400)
    if not _is_url(source):
        return web.json_response({"error": "URL must start with http:// or https://"}, status=400)

    image_id = uuid.uuid4().hex[:8]
    item = {
        "id": image_id,
        "name": name,
        "type": "url",
        "source": source,
        "created_at": time.time(),
    }

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _generate_library_thumbnail, image_id, source)
    except Exception as err:
        _LOGGER.warning("Failed to fetch URL image %s: %s", source, err)
        return web.json_response({"error": f"Failed to fetch image: {err}"}, status=400)

    images[image_id] = item
    _save_images()
    return web.json_response({"ok": True, "image": _serialize_image(item)})


async def handle_api_uploads(request: web.Request) -> web.Response:
    files = []
    for item in sorted(images.values(), key=lambda entry: entry.get("created_at", 0), reverse=True):
        if item.get("type") == "file" and item.get("filename"):
            files.append({"name": item["filename"], "path": item["source"]})
    return web.json_response(files)


async def handle_api_image_update(request: web.Request) -> web.Response:
    image_id = request.match_info["image_id"]
    item = images.get(image_id)
    if item is None:
        return web.json_response({"error": "Not found"}, status=404)

    data = await request.json()
    if "name" not in data:
        return web.json_response({"error": "name required"}, status=400)

    item["name"] = str(data.get("name", "")).strip()
    _save_images()
    return web.json_response({"ok": True, "image": _serialize_image(item)})


async def handle_api_image_delete(request: web.Request) -> web.Response:
    image_id = request.match_info["image_id"]
    item = images.get(image_id)
    if item is None:
        return web.json_response({"error": "Not found"}, status=404)

    source = item["source"]
    if item.get("type") == "file":
        Path(source).unlink(missing_ok=True)

    thumb_path = _image_thumb_path(image_id)
    thumb_path.unlink(missing_ok=True)

    for key, assignment in list(assignments.items()):
        if assignment.get("type") != "image":
            continue
        if assignment.get("image_id") == image_id or assignment.get("source") == source:
            assignments.pop(key, None)
            _clear_caches_for_screen(key)

    changed_albums: set[str] = set()
    for album in albums.values():
        original_count = len(album.get("images", []))
        album["images"] = [
            entry for entry in album.get("images", [])
            if entry.get("image_id") != image_id and entry.get("source") != source
        ]
        if len(album["images"]) != original_count:
            changed_albums.add(album["id"])

    for key, assignment in assignments.items():
        if assignment.get("type") == "album" and assignment.get("source") in changed_albums:
            album_state.pop(key, None)
            _clear_caches_for_screen(key)

    _clear_caches_for_source(source)
    images.pop(image_id, None)
    _save_images()
    _save_albums()
    _save_assignments()

    return web.json_response({"ok": True})


async def handle_upload_file(request: web.Request) -> web.Response:
    """Serve an uploaded file (for preview in the UI)."""
    filename = request.match_info["filename"]
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    path = UPLOAD_DIR / safe_name
    if not path.is_file():
        return web.Response(status=404)
    return web.FileResponse(path)


async def handle_thumbnail(request: web.Request) -> web.Response:
    """Serve a thumbnail for an uploaded file."""
    filename = request.match_info["filename"]
    safe_name = "".join(c for c in filename if c.isalnum() or c in ".-_")
    # Thumbnail is always .jpg based on the stem
    stem = Path(safe_name).stem
    thumb_path = THUMB_DIR / (stem + ".jpg")
    if not thumb_path.is_file():
        # Fallback: try generating on-the-fly from the original
        original = UPLOAD_DIR / safe_name
        if original.is_file():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _generate_thumbnail, original)
        if not thumb_path.is_file():
            return web.Response(status=404)
    return web.FileResponse(thumb_path)


async def handle_thumbnail_by_id(request: web.Request) -> web.Response:
    """Serve a thumbnail for an image library item."""
    image_id = request.match_info["image_id"]
    item = images.get(image_id)
    if item is None:
        return web.Response(status=404)

    thumb_path = _image_thumb_path(image_id)
    if not thumb_path.is_file():
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _generate_library_thumbnail, image_id, item["source"])
        except Exception:
            return web.Response(status=404)
    return web.FileResponse(thumb_path)


# --- Main ---


async def run() -> int:
    _LOGGER.info(
        "Using %s data directory: %s",
        "add-on" if IS_ADDON else "local",
        DATA_DIR,
    )
    options = _load_options()

    _load_images()
    _sync_images()
    _load_albums()
    _load_assignments()
    _warm_assignment_caches()

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

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/screens", handle_api_screens)
    app.router.add_post("/api/assign", handle_api_assign)
    app.router.add_post("/api/unassign", handle_api_unassign)
    app.router.add_get("/api/images", handle_api_images)
    app.router.add_get("/api/albums", handle_api_albums)
    app.router.add_post("/api/albums", handle_api_album_create)
    app.router.add_put("/api/albums/{album_id}", handle_api_album_update)
    app.router.add_delete("/api/albums/{album_id}", handle_api_album_delete)
    app.router.add_post("/api/upload", handle_api_upload)
    app.router.add_post("/api/images/url", handle_api_image_url)
    app.router.add_patch("/api/images/{image_id}", handle_api_image_update)
    app.router.add_delete("/api/images/{image_id}", handle_api_image_delete)
    app.router.add_get("/api/uploads", handle_api_uploads)
    app.router.add_get("/uploads/{filename}", handle_upload_file)
    app.router.add_get("/thumbnails/{filename}", handle_thumbnail)
    app.router.add_get("/thumbnails/by-id/{image_id}", handle_thumbnail_by_id)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    _LOGGER.info("Web UI started on port 8099")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled_signals: list[signal.Signals] = []
    exit_code = 0

    def _request_shutdown(sig: signal.Signals) -> None:
        nonlocal exit_code
        exit_code = 130 if sig is signal.SIGINT else 143
        if shutdown_event.is_set():
            os._exit(exit_code)
            return
        _LOGGER.info("Shutdown requested via %s", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
        except (NotImplementedError, RuntimeError):
            continue
        handled_signals.append(sig)

    try:
        await shutdown_event.wait()
    finally:
        try:
            await od_server.stop()
            await runner.cleanup()
        finally:
            preprocess_executor.shutdown(wait=False, cancel_futures=True)
            for sig in handled_signals:
                loop.remove_signal_handler(sig)
    return exit_code


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_code: int | None = None

    try:
        exit_code = loop.run_until_complete(run())
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    if exit_code is not None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)


if __name__ == "__main__":
    main()
