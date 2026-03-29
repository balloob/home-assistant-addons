from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from opendisplay.wifi.protocol import DisplayAnnouncement

from .models import (
    AlbumPlaybackState,
    AlbumRecord,
    ImageRecord,
    ScreenAssignment,
    ScreenInfo,
    ScreenKey,
)

LOGGER = logging.getLogger(__name__)

THUMB_MAX_SIZE = (200, 200)
DISPLAY_POLL_INTERVAL = 30
SLOW_OPERATION_LOG_THRESHOLD = 0.5


@dataclass(slots=True)
class AppPaths:
    root_dir: Path
    data_dir: Path
    upload_dir: Path
    thumb_dir: Path
    processed_cache_dir: Path
    assignments_file: Path
    albums_file: Path
    images_file: Path
    options_file: Path | None
    template_dir: Path
    is_addon: bool


@dataclass(slots=True)
class AppState:
    paths: AppPaths
    screens: dict[ScreenKey, ScreenInfo] = field(default_factory=dict)
    assignments: dict[ScreenKey, ScreenAssignment] = field(default_factory=dict)
    albums: dict[str, AlbumRecord] = field(default_factory=dict)
    images: dict[str, ImageRecord] = field(default_factory=dict)
    album_state: dict[ScreenKey, AlbumPlaybackState] = field(default_factory=dict)
    preprocess_lock: threading.Lock = field(default_factory=threading.Lock)
    preprocess_tasks: dict[str, concurrent.futures.Future[None]] = field(default_factory=dict)
    preprocess_executor: concurrent.futures.ThreadPoolExecutor = field(
        default_factory=lambda: concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="opendisplay-preprocess",
        )
    )


def detect_paths(root_dir: Path) -> AppPaths:
    local_data_dir = root_dir / "dev-data"
    addon_data_dir = Path("/data")
    local_options_file = local_data_dir / "options-dev.json"
    addon_options_file = addon_data_dir / "options.json"
    options_file = next(
        (path for path in (local_options_file, addon_options_file) if path.exists()),
        None,
    )

    is_addon = options_file == addon_options_file
    data_dir = addon_data_dir if is_addon else local_data_dir
    upload_dir = data_dir / "uploads"
    thumb_dir = data_dir / "thumbnails"
    processed_cache_dir = data_dir / "processed-cache"
    upload_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    processed_cache_dir.mkdir(parents=True, exist_ok=True)

    return AppPaths(
        root_dir=root_dir,
        data_dir=data_dir,
        upload_dir=upload_dir,
        thumb_dir=thumb_dir,
        processed_cache_dir=processed_cache_dir,
        assignments_file=data_dir / "assignments.json",
        albums_file=data_dir / "albums.json",
        images_file=data_dir / "images.json",
        options_file=options_file,
        template_dir=root_dir / "templates",
        is_addon=is_addon,
    )


def load_options(paths: AppPaths) -> dict:
    if paths.options_file is None:
        LOGGER.info("No options file found, using defaults")
        return {}

    LOGGER.info("Loading options from %s", paths.options_file)
    try:
        raw = paths.options_file.read_text().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        LOGGER.exception("Failed to load options from %s, using defaults", paths.options_file)
        return {}


def screen_key(announcement: DisplayAnnouncement) -> ScreenKey:
    return (announcement.width, announcement.height, announcement.colour_scheme)


def screen_id(key: ScreenKey) -> str:
    return f"{key[0]}x{key[1]}_cs{key[2]}"


def key_from_id(value: str) -> ScreenKey | None:
    try:
        dims, colour_scheme = value.rsplit("_cs", 1)
        width, height = dims.split("x")
        return (int(width), int(height), int(colour_scheme))
    except (ValueError, AttributeError):
        return None
