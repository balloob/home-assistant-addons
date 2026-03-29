from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .models import ScreenKey

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessedImageRef:
    source: str
    width: int
    height: int
    fit: str

    @property
    def cache_key(self) -> str:
        return f"{self.source}_{self.width}x{self.height}_{self.fit}"


@dataclass(frozen=True, slots=True)
class ProcessedCacheMeta:
    source: str
    width: int
    height: int
    fit: str
    pixel_hash: str | None = None

    @property
    def ref(self) -> ProcessedImageRef:
        return ProcessedImageRef(
            source=self.source,
            width=self.width,
            height=self.height,
            fit=self.fit,
        )

    @property
    def cache_key(self) -> str:
        return self.ref.cache_key

    def to_dict(self) -> dict[str, object]:
        return {
            "cache_key": self.cache_key,
            "source": self.source,
            "width": self.width,
            "height": self.height,
            "fit": self.fit,
            "pixel_hash": self.pixel_hash,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ProcessedCacheMeta | None":
        if not isinstance(raw, dict):
            return None

        source = raw.get("source")
        fit = raw.get("fit")
        if not isinstance(source, str) or not source:
            return None
        if not isinstance(fit, str) or not fit:
            return None

        width = raw.get("width")
        height = raw.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            return None

        pixel_hash = raw.get("pixel_hash")
        if pixel_hash is not None and not isinstance(pixel_hash, str):
            pixel_hash = None

        return cls(
            source=source,
            width=width,
            height=height,
            fit=fit,
            pixel_hash=pixel_hash,
        )


class ProcessedImageCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._image_cache: dict[ProcessedImageRef, bytes] = {}
        self._pixel_hashes: dict[ProcessedImageRef, str] = {}
        self._lock = threading.Lock()

    def ref(self, source: str, width: int, height: int, fit: str) -> ProcessedImageRef:
        return ProcessedImageRef(source=source, width=width, height=height, fit=fit)

    def cache_key(self, source: str, width: int, height: int, fit: str) -> str:
        return self.ref(source, width, height, fit).cache_key

    def _cache_file_stem(self, ref: ProcessedImageRef) -> str:
        return hashlib.sha256(ref.cache_key.encode("utf-8")).hexdigest()

    def _data_path(self, ref: ProcessedImageRef) -> Path:
        return self.cache_dir / f"{self._cache_file_stem(ref)}.bin"

    def _meta_path(self, ref: ProcessedImageRef) -> Path:
        return self.cache_dir / f"{self._cache_file_stem(ref)}.json"

    def _read_meta_file(self, meta_path: Path) -> ProcessedCacheMeta | None:
        try:
            raw = json.loads(meta_path.read_text())
        except Exception:
            LOGGER.exception("Failed to read cache metadata %s", meta_path.name)
            return None

        meta = ProcessedCacheMeta.from_dict(raw)
        if meta is None:
            LOGGER.warning("Ignoring invalid cache metadata %s", meta_path.name)
        return meta

    def _read_meta(self, ref: ProcessedImageRef) -> ProcessedCacheMeta | None:
        meta_path = self._meta_path(ref)
        if not meta_path.is_file():
            return None

        meta = self._read_meta_file(meta_path)
        if meta is None:
            return None

        if meta.ref != ref:
            LOGGER.warning("Ignoring mismatched cache metadata for %s", ref.cache_key)
            return None

        return meta

    def _cache_entries(self) -> list[tuple[ProcessedCacheMeta, Path, Path]]:
        entries: list[tuple[ProcessedCacheMeta, Path, Path]] = []
        for meta_path in self.cache_dir.glob("*.json"):
            meta = self._read_meta_file(meta_path)
            if meta is None:
                continue
            entries.append((meta, meta_path.with_suffix(".bin"), meta_path))
        return entries

    def _delete_disk_entry(self, data_path: Path, meta_path: Path) -> None:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

    def get(self, source: str, width: int, height: int, fit: str) -> bytes | None:
        ref = self.ref(source, width, height, fit)
        with self._lock:
            cached = self._image_cache.get(ref)

        if cached is not None:
            return cached

        data_path = self._data_path(ref)
        if not data_path.is_file():
            return None

        meta = self._read_meta(ref)
        if meta is None:
            return None

        try:
            cached = data_path.read_bytes()
        except Exception:
            LOGGER.exception("Failed to read cached image for %s", ref.cache_key)
            return None

        with self._lock:
            self._image_cache[ref] = cached
            if meta.pixel_hash:
                self._pixel_hashes[ref] = meta.pixel_hash

        return cached

    def has(self, source: str, width: int, height: int, fit: str) -> bool:
        ref = self.ref(source, width, height, fit)
        with self._lock:
            if ref in self._image_cache:
                return True

        return self._data_path(ref).is_file() and self._read_meta(ref) is not None

    def set(
        self,
        source: str,
        width: int,
        height: int,
        fit: str,
        data: bytes,
        *,
        pixel_hash: str | None = None,
    ) -> None:
        ref = self.ref(source, width, height, fit)
        meta = ProcessedCacheMeta(
            source=source,
            width=width,
            height=height,
            fit=fit,
            pixel_hash=pixel_hash,
        )

        with self._lock:
            self._image_cache[ref] = data
            if pixel_hash is None:
                self._pixel_hashes.pop(ref, None)
            else:
                self._pixel_hashes[ref] = pixel_hash

        try:
            self._data_path(ref).write_bytes(data)
            self._meta_path(ref).write_text(json.dumps(meta.to_dict()))
        except Exception:
            self.remove(source, width, height, fit)
            LOGGER.exception("Failed to persist cached image for %s", ref.cache_key)

    def get_pixel_hash(self, source: str, width: int, height: int, fit: str) -> str | None:
        ref = self.ref(source, width, height, fit)
        with self._lock:
            pixel_hash = self._pixel_hashes.get(ref)

        if pixel_hash is not None:
            return pixel_hash

        meta = self._read_meta(ref)
        if meta is None or not meta.pixel_hash:
            return None

        with self._lock:
            self._pixel_hashes[ref] = meta.pixel_hash

        return meta.pixel_hash

    def remove(self, source: str, width: int, height: int, fit: str) -> None:
        ref = self.ref(source, width, height, fit)
        with self._lock:
            self._image_cache.pop(ref, None)
            self._pixel_hashes.pop(ref, None)

        self._delete_disk_entry(self._data_path(ref), self._meta_path(ref))

    def clear_for_source(self, source: str) -> None:
        with self._lock:
            to_remove = {
                ref for ref in self._image_cache if ref.source == source
            } | {
                ref for ref in self._pixel_hashes if ref.source == source
            }
            for ref in to_remove:
                self._image_cache.pop(ref, None)
                self._pixel_hashes.pop(ref, None)

        for meta, data_path, meta_path in self._cache_entries():
            if meta.source == source:
                self._delete_disk_entry(data_path, meta_path)

    def clear_for_screen(self, key: ScreenKey) -> None:
        width, height = key[0], key[1]
        with self._lock:
            to_remove = {
                ref for ref in self._image_cache if ref.width == width and ref.height == height
            } | {
                ref for ref in self._pixel_hashes if ref.width == width and ref.height == height
            }
            for ref in to_remove:
                self._image_cache.pop(ref, None)
                self._pixel_hashes.pop(ref, None)

        for meta, data_path, meta_path in self._cache_entries():
            if meta.width == width and meta.height == height:
                self._delete_disk_entry(data_path, meta_path)
