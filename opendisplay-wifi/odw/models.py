from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ScreenKey = tuple[int, int, int]
SourceType = Literal["file", "url"]
AssignmentType = Literal["image", "album"]
FitName = Literal["contain", "cover"]


@dataclass(slots=True)
class ImageRecord:
    id: str
    type: SourceType
    source: str
    name: str = ""
    filename: str | None = None
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, image_id: str, raw: dict) -> "ImageRecord":
        item_type = "url" if raw.get("type") == "url" else "file"
        return cls(
            id=image_id,
            name=str(raw.get("name", "")),
            type=item_type,
            source=str(raw.get("source", "")),
            filename=str(raw["filename"]) if raw.get("filename") else None,
            created_at=float(raw.get("created_at", 0) or 0),
        )

    @property
    def display_name(self) -> str:
        if self.name.strip():
            return self.name.strip()
        if self.type == "file":
            if self.filename:
                return Path(self.filename).stem
            return Path(self.source).stem
        return self.source

    @property
    def subtitle(self) -> str:
        return self.source if self.type == "url" else (self.filename or "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "source": self.source,
            "filename": self.filename,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class AlbumEntry:
    source: str
    type: SourceType = "file"
    image_id: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "AlbumEntry":
        entry_type = "url" if raw.get("type") == "url" else "file"
        image_id = raw.get("image_id")
        return cls(
            image_id=str(image_id) if image_id else None,
            type=entry_type,
            source=str(raw.get("source", "")),
        )

    def to_dict(self) -> dict:
        return {
            "image_id": self.image_id,
            "type": self.type,
            "source": self.source,
        }


@dataclass(slots=True)
class AlbumRecord:
    id: str
    name: str
    images: list[AlbumEntry] = field(default_factory=list)
    transition_interval: int = 60
    shuffle: bool = False

    @classmethod
    def from_dict(cls, album_id: str, raw: dict) -> "AlbumRecord":
        images = [AlbumEntry.from_dict(entry) for entry in raw.get("images", []) if isinstance(entry, dict)]
        return cls(
            id=album_id,
            name=str(raw.get("name", "")),
            images=images,
            transition_interval=int(raw.get("transition_interval", 60) or 60),
            shuffle=bool(raw.get("shuffle", False)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "images": [entry.to_dict() for entry in self.images],
            "transition_interval": self.transition_interval,
            "shuffle": self.shuffle,
        }


@dataclass(slots=True)
class ScreenAssignment:
    type: AssignmentType
    source: str
    fit: FitName = "contain"
    image_id: str | None = None
    source_type: SourceType = "file"
    poll_interval: int = 5

    @classmethod
    def from_dict(cls, raw: dict) -> "ScreenAssignment":
        assignment_type = "album" if raw.get("type") == "album" else "image"
        source_type = "url" if raw.get("source_type") == "url" else "file"
        fit = "cover" if raw.get("fit") == "cover" else "contain"
        image_id = raw.get("image_id")
        return cls(
            type=assignment_type,
            source=str(raw.get("source", "")),
            fit=fit,
            image_id=str(image_id) if image_id else None,
            source_type=source_type,
            poll_interval=int(raw.get("poll_interval", 5) or 5),
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "source": self.source,
            "fit": self.fit,
            "image_id": self.image_id,
            "source_type": self.source_type,
            "poll_interval": self.poll_interval,
        }


@dataclass(slots=True)
class ScreenInfo:
    width: int
    height: int
    colour_scheme: int
    firmware_id: int = 0
    firmware_version: int = 0
    last_seen: float = 0.0


@dataclass(slots=True)
class AlbumPlaybackState:
    current_index: int
    last_transition: float
    order: list[int]


@dataclass(slots=True)
class AlbumPreprocessStatus:
    total_images: int
    ready_images: int
    active_images: int

    @property
    def pending_images(self) -> int:
        return max(0, self.total_images - self.ready_images)

    @property
    def partial(self) -> bool:
        return 0 < self.ready_images < self.total_images

    @property
    def active(self) -> bool:
        return self.active_images > 0

    def to_dict(self) -> dict:
        return {
            "total_images": self.total_images,
            "ready_images": self.ready_images,
            "active_images": self.active_images,
            "pending_images": self.pending_images,
            "partial": self.partial,
            "active": self.active,
        }

