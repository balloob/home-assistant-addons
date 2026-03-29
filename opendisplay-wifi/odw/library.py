from __future__ import annotations

import io
import json
import logging
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

from PIL import Image

from .models import AlbumEntry, AlbumRecord, ImageRecord, ScreenAssignment
from .state import AppState, THUMB_MAX_SIZE, screen_id
from .utils import is_url

LOGGER = logging.getLogger(__name__)


class LibraryStore:
    def __init__(self, state: AppState) -> None:
        self.state = state

    def save_assignments(self) -> None:
        serializable = {
            screen_id(key): assignment.to_dict()
            for key, assignment in self.state.assignments.items()
        }
        self.state.paths.assignments_file.write_text(json.dumps(serializable, indent=2))

    def load_assignments(self) -> None:
        if not self.state.paths.assignments_file.exists():
            return
        try:
            raw = json.loads(self.state.paths.assignments_file.read_text())
            for raw_screen_id, value in raw.items():
                from .state import key_from_id

                key = key_from_id(raw_screen_id)
                if key is None or not isinstance(value, dict):
                    continue
                self.state.assignments[key] = ScreenAssignment.from_dict(value)
            LOGGER.info("Loaded %d saved assignments", len(self.state.assignments))
        except Exception:
            LOGGER.exception("Failed to load saved assignments")

    def save_albums(self) -> None:
        payload = {album_id: album.to_dict() for album_id, album in self.state.albums.items()}
        self.state.paths.albums_file.write_text(json.dumps(payload, indent=2))

    def save_images(self) -> None:
        payload = {image_id: image.to_dict() for image_id, image in self.state.images.items()}
        self.state.paths.images_file.write_text(json.dumps(payload, indent=2))

    def load_albums(self) -> None:
        if not self.state.paths.albums_file.exists():
            return
        try:
            raw = json.loads(self.state.paths.albums_file.read_text())
            changed = False
            for album_id, value in raw.items():
                if not isinstance(value, dict):
                    continue
                album = AlbumRecord.from_dict(album_id, value)
                normalized = self.normalize_album_images(album.images)
                if normalized != album.images:
                    album.images = normalized
                    changed = True
                self.state.albums[album_id] = album
            if changed:
                self.save_albums()
            LOGGER.info("Loaded %d saved albums", len(self.state.albums))
        except Exception:
            LOGGER.exception("Failed to load saved albums")

    def load_images(self) -> None:
        if not self.state.paths.images_file.exists():
            return
        try:
            raw = json.loads(self.state.paths.images_file.read_text())
            if isinstance(raw, dict):
                for image_id, value in raw.items():
                    if not isinstance(value, dict):
                        continue
                    self.state.images[image_id] = ImageRecord.from_dict(image_id, value)
            LOGGER.info("Loaded %d saved images", len(self.state.images))
        except Exception:
            LOGGER.exception("Failed to load saved images")

    def sanitize_filename(self, filename: str) -> str:
        safe_name = "".join(char for char in filename if char.isalnum() or char in ".-_")
        return safe_name or "upload.png"

    def make_unique_filename(self, filename: str) -> str:
        candidate = Path(self.sanitize_filename(filename))
        stem = candidate.stem or "upload"
        suffix = candidate.suffix or ".png"
        counter = 1
        final_name = f"{stem}{suffix}"
        while (self.state.paths.upload_dir / final_name).exists():
            counter += 1
            final_name = f"{stem}-{counter}{suffix}"
        return final_name

    def image_thumb_path(self, image_id: str) -> Path:
        return self.state.paths.thumb_dir / f"{image_id}.jpg"

    def find_image(
        self,
        *,
        image_id: str | None = None,
        source: str | None = None,
    ) -> ImageRecord | None:
        if image_id:
            return self.state.images.get(image_id)
        if source:
            for item in self.state.images.values():
                if item.source == source:
                    return item
        return None

    def serialize_image(self, item: ImageRecord) -> dict:
        return {
            "id": item.id,
            "name": item.name,
            "display_name": item.display_name,
            "type": item.type,
            "source": item.source,
            "filename": item.filename,
            "subtitle": item.subtitle,
            "created_at": item.created_at,
        }

    def write_thumbnail(self, img: Image.Image, thumb_path: Path) -> None:
        thumb = img.copy()
        thumb.thumbnail(THUMB_MAX_SIZE)
        thumb.convert("RGB").save(thumb_path, "JPEG", quality=80)

    def fetch_url_image(self, source: str, timeout: int = 60) -> Image.Image:
        with urlopen(source, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img

    def load_image(self, source: str) -> Image.Image | None:
        from .image_pipeline import log_duration

        started = time.monotonic()
        if is_url(source):
            try:
                img = self.fetch_url_image(source)
                log_duration("Loaded URL image", started, source=source)
                return img
            except Exception:
                LOGGER.exception("Failed to fetch URL: %s", source)
                return None

        try:
            with Image.open(source) as img:
                img.load()
                loaded = img.copy()
            log_duration("Loaded file image", started, source=Path(source).name or source)
            return loaded
        except Exception:
            LOGGER.exception("Failed to load file: %s", source)
            return None

    def generate_thumbnail(self, source: Path) -> None:
        try:
            with Image.open(source) as img:
                img.load()
                self.write_thumbnail(img, self.state.paths.thumb_dir / f"{source.stem}.jpg")
        except Exception:
            LOGGER.exception("Failed to generate thumbnail for %s", source.name)

    def generate_library_thumbnail(self, image_id: str, source: str) -> None:
        img = self.load_image(source)
        if img is None:
            raise ValueError(f"Unable to load image source {source}")
        self.write_thumbnail(img, self.image_thumb_path(image_id))

    def sync_images(self) -> None:
        changed = False

        for image_id, item in list(self.state.images.items()):
            item.id = image_id
            if not item.created_at:
                item.created_at = time.time()
                changed = True

            if item.type not in ("file", "url"):
                del self.state.images[image_id]
                changed = True
                continue

            if item.type == "file":
                if not item.filename and item.source:
                    item.filename = Path(item.source).name
                    changed = True
                if item.filename:
                    item.source = str(self.state.paths.upload_dir / item.filename)
                if not Path(item.source).is_file():
                    del self.state.images[image_id]
                    thumb_path = self.image_thumb_path(image_id)
                    if thumb_path.exists():
                        thumb_path.unlink()
                    changed = True

        tracked_paths = {
            item.source
            for item in self.state.images.values()
            if item.type == "file" and item.source
        }
        for path in sorted(self.state.paths.upload_dir.iterdir()) if self.state.paths.upload_dir.exists() else []:
            if not path.is_file():
                continue
            source = str(path)
            if source in tracked_paths:
                continue
            image_id = uuid.uuid4().hex[:8]
            self.state.images[image_id] = ImageRecord(
                id=image_id,
                name=path.stem,
                type="file",
                source=source,
                filename=path.name,
                created_at=time.time(),
            )
            changed = True

        if changed:
            self.save_images()

    def normalize_album_images(self, entries: list[dict | AlbumEntry]) -> list[AlbumEntry]:
        normalized: list[AlbumEntry] = []
        for raw_entry in entries:
            if isinstance(raw_entry, AlbumEntry):
                entry = raw_entry
            elif isinstance(raw_entry, dict):
                entry = AlbumEntry.from_dict(raw_entry)
            else:
                continue

            item = self.find_image(image_id=entry.image_id) if entry.image_id else None
            if item is None and entry.source:
                item = self.find_image(source=entry.source)

            if item is not None:
                normalized.append(
                    AlbumEntry(
                        image_id=item.id,
                        type=item.type,
                        source=item.source,
                    )
                )
                continue

            source = entry.source.strip()
            if not source:
                continue
            normalized.append(
                AlbumEntry(
                    image_id=entry.image_id,
                    type="url" if is_url(source) else "file",
                    source=source,
                )
            )
        return normalized
