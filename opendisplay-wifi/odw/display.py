from __future__ import annotations

import logging
import random
import time

from opendisplay.wifi.protocol import DisplayAnnouncement

from .image_pipeline import ImagePipeline, PreprocessScheduleResult
from .models import (
    AlbumEntry,
    AlbumPlaybackState,
    AlbumPreprocessStatus,
    AlbumRecord,
    ScreenAssignment,
    ScreenInfo,
    ScreenKey,
)
from .state import AppState, screen_key
from .utils import is_url

LOGGER = logging.getLogger(__name__)
ALBUM_PREWARM_LIMIT = 5


class DisplayService:
    def __init__(self, state: AppState, pipeline: ImagePipeline) -> None:
        self.state = state
        self.pipeline = pipeline

    def track_screen(self, announcement: DisplayAnnouncement) -> ScreenKey:
        key = screen_key(announcement)
        info = self.state.screens.get(key)
        if info is None:
            info = ScreenInfo(
                width=announcement.width,
                height=announcement.height,
                colour_scheme=announcement.colour_scheme,
                firmware_id=announcement.firmware_id,
                firmware_version=announcement.firmware_version,
            )
            self.state.screens[key] = info
        info.last_seen = time.time()
        info.firmware_id = announcement.firmware_id
        info.firmware_version = announcement.firmware_version
        return key

    def get_current_album_image(self, key: ScreenKey, album: AlbumRecord) -> AlbumEntry | None:
        images = album.images
        if not images:
            return None

        now = time.time()
        state = self.state.album_state.get(key)
        if state is None:
            order = list(range(len(images)))
            if album.shuffle:
                random.shuffle(order)
            state = AlbumPlaybackState(
                current_index=0,
                last_transition=now,
                order=order,
            )
            self.state.album_state[key] = state

        interval = album.transition_interval or 60
        elapsed = now - state.last_transition
        if elapsed >= interval and len(images) > 1:
            steps = int(elapsed // interval)
            state.current_index = (state.current_index + steps) % len(images)
            state.last_transition = now
            if album.shuffle and state.current_index < steps:
                order = list(range(len(images)))
                random.shuffle(order)
                state.order = order

        idx = state.order[state.current_index % len(state.order)]
        return images[idx] if idx < len(images) else images[0]

    def iter_album_entries_in_order(self, key: ScreenKey, album: AlbumRecord) -> list[AlbumEntry]:
        current = self.get_current_album_image(key, album)
        if current is None:
            return []

        state = self.state.album_state.get(key)
        if state is None or not album.images:
            return []

        ordered_entries: list[AlbumEntry] = []
        current_index = state.current_index % len(state.order)
        for offset in range(len(state.order)):
            idx = state.order[(current_index + offset) % len(state.order)]
            if idx < len(album.images):
                ordered_entries.append(album.images[idx])
        return ordered_entries

    def album_entry_ready(self, entry: AlbumEntry, key: ScreenKey, fit: str) -> bool:
        source = entry.source.strip()
        if not source:
            return False
        return self.pipeline.is_cached(source, key, fit)

    def get_album_display_entry(
        self,
        key: ScreenKey,
        album: AlbumRecord,
        fit: str,
    ) -> AlbumEntry | None:
        ordered_entries = self.iter_album_entries_in_order(key, album)
        if not ordered_entries:
            return None

        for entry in ordered_entries:
            if self.album_entry_ready(entry, key, fit):
                return entry

        return ordered_entries[0]

    def advance_album_state(
        self,
        key: ScreenKey,
        album: AlbumRecord,
        *,
        steps: int = 1,
    ) -> AlbumEntry | None:
        current = self.get_current_album_image(key, album)
        if current is None:
            return None

        state = self.state.album_state.get(key)
        if state is None or not album.images or not state.order:
            return None

        previous_index = state.current_index
        next_index = (previous_index + steps) % len(state.order)
        wrapped = previous_index + steps >= len(state.order)
        state.current_index = next_index
        state.last_transition = time.time()

        if album.shuffle and wrapped and len(album.images) > 1:
            new_order = list(range(len(album.images)))
            random.shuffle(new_order)
            state.order = new_order

        idx = state.order[state.current_index % len(state.order)]
        return album.images[idx] if idx < len(album.images) else album.images[0]

    def resolve_source(
        self,
        assignment: ScreenAssignment,
        key: ScreenKey,
    ) -> tuple[str, str] | None:
        if assignment.type == "album":
            album = self.state.albums.get(assignment.source)
            if album is None:
                return None
            entry = self.get_album_display_entry(key, album, assignment.fit)
            if entry is None:
                return None
            return (entry.source, entry.type)
        return (assignment.source, assignment.source_type)

    def image_provider(self, announcement: DisplayAnnouncement | None) -> bytes | None:
        if announcement is None:
            return None

        key = self.track_screen(announcement)
        assignment = self.state.assignments.get(key)
        if assignment is None:
            return None

        resolved = self.resolve_source(assignment, key)
        if resolved is None:
            return None

        if assignment.type == "album":
            self.schedule_assignment_preprocess(key, assignment)

        source, source_type = resolved
        width, height = announcement.width, announcement.height
        cached = self.pipeline.get_cached_image_for(source, width, height, assignment.fit)

        if source_type == "url" or is_url(source):
            self.pipeline.schedule_preprocess(source, width, height, assignment.fit, source_type)
            return cached

        if cached is not None:
            return cached

        self.pipeline.schedule_preprocess(source, width, height, assignment.fit, source_type)
        return None

    def schedule_assignment_preprocess(self, key: ScreenKey, assignment: ScreenAssignment) -> None:
        width, height = key[0], key[1]
        fit = assignment.fit

        if assignment.type == "album":
            album = self.state.albums.get(assignment.source)
            if album is None:
                return
            ordered_entries = self.iter_album_entries_in_order(key, album)
            if not ordered_entries:
                return
            prewarm_entries = ordered_entries[:ALBUM_PREWARM_LIMIT]
            from .state import screen_id

            queued = 0
            for entry in prewarm_entries:
                source = entry.source.strip()
                if not source:
                    continue
                source_type = entry.type or ("url" if is_url(source) else "file")
                result = self.pipeline.schedule_preprocess(source, width, height, fit, source_type)
                if result is PreprocessScheduleResult.QUEUED:
                    queued += 1
            if queued > 0:
                LOGGER.info(
                    "Prewarming album images for %s: %d queued (%d targeted, %d total in rotation)",
                    screen_id(key),
                    queued,
                    len(prewarm_entries),
                    len(ordered_entries),
                )
            return

        source = assignment.source.strip()
        if not source:
            return
        source_type = assignment.source_type or ("url" if is_url(source) else "file")
        self.pipeline.schedule_preprocess(source, width, height, fit, source_type)

    def warm_assignment_caches(self) -> None:
        for key, assignment in self.state.assignments.items():
            self.schedule_assignment_preprocess(key, assignment)

    def get_album_preprocess_status(
        self,
        key: ScreenKey,
        album: AlbumRecord,
        fit: str,
    ) -> AlbumPreprocessStatus | None:
        ordered_entries = self.iter_album_entries_in_order(key, album)
        if not ordered_entries:
            return None

        ready_images = 0
        active_cache_keys: set[str] = set()
        for entry in ordered_entries:
            source = entry.source.strip()
            if not source:
                continue
            if self.pipeline.is_cached(source, key, fit):
                ready_images += 1
            if self.pipeline.is_preprocess_active(source, key, fit):
                active_cache_keys.add(
                    self.pipeline.cache_key(source, key[0], key[1], fit),
                )

        return AlbumPreprocessStatus(
            total_images=len(ordered_entries),
            ready_images=ready_images,
            active_images=len(active_cache_keys),
        )
