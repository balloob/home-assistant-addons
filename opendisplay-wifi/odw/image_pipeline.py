from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import time
from enum import Enum

from opendisplay.encoding.images import encode_image, fit_image
from opendisplay.models.enums import FitMode
from PIL import Image

from .library import LibraryStore
from .models import ScreenKey
from .processed_cache import ProcessedImageCache
from .state import AppState, SLOW_OPERATION_LOG_THRESHOLD
from .utils import is_url, log_source

LOGGER = logging.getLogger(__name__)


def encode_bitplanes(image: Image.Image) -> bytes:
    """Encode BWR/BWY palette images as concatenated black/white and accent bitplanes."""
    if image.mode != "P":
        raise ValueError(f"Expected palette image, got {image.mode}")

    width, height = image.size
    pixels = image.tobytes()
    bytes_per_row = (width + 7) // 8
    plane1 = bytearray(bytes_per_row * height)
    plane2 = bytearray(bytes_per_row * height)

    for y in range(height):
        row_offset = y * width
        byte_row_offset = y * bytes_per_row
        for x in range(width):
            palette_idx = pixels[row_offset + x]
            bit = 1 << (7 - (x % 8))
            byte_idx = byte_row_offset + x // 8

            if palette_idx == 1:
                plane1[byte_idx] |= bit
            elif palette_idx == 2:
                plane2[byte_idx] |= bit

    return bytes(plane1) + bytes(plane2)


def log_duration(
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

    LOGGER.log(level, "%s in %.2fs%s", action, elapsed, suffix)
    return elapsed


class PreprocessScheduleResult(Enum):
    QUEUED = "queued"
    ALREADY_CACHED = "already_cached"
    ALREADY_ACTIVE = "already_active"


class ImagePipeline:
    def __init__(self, state: AppState, library: LibraryStore) -> None:
        self.state = state
        self.library = library
        self.cache = ProcessedImageCache(state.paths.processed_cache_dir)

    def convert_image(
        self,
        img: Image.Image,
        width: int,
        height: int,
        fit: str,
        colour_scheme: int,
    ) -> bytes:
        started = time.monotonic()
        fit_mode = FitMode.COVER if fit == "cover" else FitMode.CONTAIN

        fit_started = time.monotonic()
        fitted = fit_image(img, (width, height), fit_mode)
        log_duration("Fitted image", fit_started, width=width, height=height, fit=fit)

        from epaper_dithering import MONO_4_26, ColorScheme, DitherMode, dither_image

        try:
            scheme = ColorScheme.from_value(colour_scheme)
        except ValueError:
            LOGGER.warning("Unsupported colour scheme %s; falling back to monochrome", colour_scheme)
            scheme = ColorScheme.MONO

        dither_started = time.monotonic()
        dither_palette = MONO_4_26 if scheme == ColorScheme.MONO else scheme
        dithered = dither_image(fitted, dither_palette, mode=DitherMode.FLOYD_STEINBERG)
        log_duration("Dithered image", dither_started, width=width, height=height, fit=fit, scheme=scheme.name)

        pack_started = time.monotonic()
        if scheme in (ColorScheme.BWR, ColorScheme.BWY):
            data = encode_bitplanes(dithered)
        else:
            data = encode_image(dithered, scheme)
        log_duration(
            "Packed image",
            pack_started,
            width=width,
            height=height,
            fit=fit,
            scheme=scheme.name,
            bytes=len(data),
        )
        log_duration(
            "Converted image",
            started,
            width=width,
            height=height,
            fit=fit,
            scheme=scheme.name,
            bytes=len(data),
        )
        return data

    def cache_key(self, source: str, width: int, height: int, fit: str, colour_scheme: int) -> str:
        return self.cache.cache_key(source, width, height, fit, colour_scheme)

    def get_cached_image_for(
        self,
        source: str,
        width: int,
        height: int,
        fit: str,
        colour_scheme: int,
    ) -> bytes | None:
        return self.cache.get(source, width, height, fit, colour_scheme)

    def clear_caches_for_source(self, source: str) -> None:
        self.cache.clear_for_source(source)

    def is_cached(self, source: str, key: ScreenKey, fit: str) -> bool:
        return self.cache.has(source, key[0], key[1], fit, key[2])

    def is_preprocess_active(self, source: str, key: ScreenKey, fit: str) -> bool:
        cache_key = self.cache_key(source, key[0], key[1], fit, key[2])
        with self.state.preprocess_lock:
            future = self.state.preprocess_tasks.get(cache_key)
            return future is not None and not future.done()

    def preprocess_image(
        self,
        source: str,
        width: int,
        height: int,
        fit: str,
        colour_scheme: int,
        source_type: str,
    ) -> None:
        started = time.monotonic()
        cache_key = self.cache_key(source, width, height, fit, colour_scheme)
        img = self.library.load_image(source)
        if img is None:
            LOGGER.warning("Unable to pre-process %s", source)
            return

        pixel_hash: str | None = None
        if source_type == "url" or is_url(source):
            pixel_hash = hashlib.sha256(img.tobytes()).hexdigest()[:16]
            if (
                self.cache.get_pixel_hash(source, width, height, fit, colour_scheme) == pixel_hash
                and self.cache.get(source, width, height, fit, colour_scheme) is not None
            ):
                log_duration(
                    "Skipped image preprocessing",
                    started,
                    level=logging.INFO,
                    source=log_source(source),
                    width=width,
                    height=height,
                    fit=fit,
                    scheme=colour_scheme,
                    reason="unchanged",
                )
                return

        data = self.convert_image(img, width, height, fit, colour_scheme)
        self.cache.set(source, width, height, fit, colour_scheme, data, pixel_hash=pixel_hash)
        log_duration(
            "Prepared cached image",
            started,
            level=logging.INFO,
            source=log_source(source),
            width=width,
            height=height,
            fit=fit,
            scheme=colour_scheme,
            type=source_type,
            bytes=len(data),
        )

    def on_preprocess_done(
        self,
        cache_key: str,
        future: concurrent.futures.Future[None],
    ) -> None:
        with self.state.preprocess_lock:
            if self.state.preprocess_tasks.get(cache_key) is future:
                self.state.preprocess_tasks.pop(cache_key, None)

        try:
            future.result()
        except concurrent.futures.CancelledError:
            LOGGER.debug("Cancelled image preprocessing for %s", cache_key)
        except Exception:
            LOGGER.exception("Failed to pre-process %s", cache_key)

    def schedule_preprocess(
        self,
        source: str,
        width: int,
        height: int,
        fit: str,
        colour_scheme: int,
        source_type: str,
    ) -> PreprocessScheduleResult:
        cache_key = self.cache_key(source, width, height, fit, colour_scheme)
        is_remote_source = source_type == "url" or is_url(source)

        if not is_remote_source and self.cache.get(source, width, height, fit, colour_scheme) is not None:
            return PreprocessScheduleResult.ALREADY_CACHED

        with self.state.preprocess_lock:
            future = self.state.preprocess_tasks.get(cache_key)
            if future is not None and not future.done():
                return PreprocessScheduleResult.ALREADY_ACTIVE

            LOGGER.info(
                "Queued image preprocessing for %s (%dx%d fit=%s scheme=%s type=%s)",
                log_source(source),
                width,
                height,
                fit,
                colour_scheme,
                source_type,
            )
            future = self.state.preprocess_executor.submit(
                self.preprocess_image,
                source,
                width,
                height,
                fit,
                colour_scheme,
                source_type,
            )
            self.state.preprocess_tasks[cache_key] = future

        future.add_done_callback(lambda done, key=cache_key: self.on_preprocess_done(key, done))
        return PreprocessScheduleResult.QUEUED
