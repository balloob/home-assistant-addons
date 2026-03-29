from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

from .display import DisplayService
from .image_pipeline import ImagePipeline
from .library import LibraryStore
from .models import AlbumRecord, ImageRecord, ScreenAssignment
from .state import AppState, key_from_id, screen_id
from .utils import is_url, log_source

LOGGER = logging.getLogger(__name__)


class WebHandlers:
    def __init__(
        self,
        state: AppState,
        library: LibraryStore,
        pipeline: ImagePipeline,
        display: DisplayService,
    ) -> None:
        self.state = state
        self.library = library
        self.pipeline = pipeline
        self.display = display

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=20 * 1024 * 1024)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/screens", self.handle_api_screens)
        app.router.add_post("/api/assign", self.handle_api_assign)
        app.router.add_post("/api/unassign", self.handle_api_unassign)
        app.router.add_post("/api/album/next", self.handle_api_album_next)
        app.router.add_get("/api/images", self.handle_api_images)
        app.router.add_get("/api/albums", self.handle_api_albums)
        app.router.add_post("/api/albums", self.handle_api_album_create)
        app.router.add_put("/api/albums/{album_id}", self.handle_api_album_update)
        app.router.add_delete("/api/albums/{album_id}", self.handle_api_album_delete)
        app.router.add_post("/api/upload", self.handle_api_upload)
        app.router.add_post("/api/images/url", self.handle_api_image_url)
        app.router.add_patch("/api/images/{image_id}", self.handle_api_image_update)
        app.router.add_delete("/api/images/{image_id}", self.handle_api_image_delete)
        app.router.add_get("/api/uploads", self.handle_api_uploads)
        app.router.add_get("/uploads/{filename}", self.handle_upload_file)
        app.router.add_get("/thumbnails/{filename}", self.handle_thumbnail)
        app.router.add_get("/thumbnails/by-id/{image_id}", self.handle_thumbnail_by_id)
        return app

    async def handle_index(self, request: web.Request) -> web.Response:
        template = (self.state.paths.template_dir / "index.html").read_text()
        return web.Response(text=template, content_type="text/html")

    def _assignment_payload(self, key: tuple[int, int, int], assignment: ScreenAssignment) -> dict:
        entry = assignment.to_dict()
        if assignment.type == "album":
            album = self.state.albums.get(assignment.source)
            if album is not None:
                entry["album_name"] = album.name
                current = self.display.get_album_display_entry(key, album, assignment.fit)
                if current is not None:
                    entry["current_source"] = current.source
                    current_image = self.library.find_image(
                        image_id=current.image_id,
                        source=current.source,
                    )
                    if current_image is not None:
                        entry["current_image_id"] = current_image.id
                        entry["current_image_name"] = current_image.display_name
                status = self.display.get_album_preprocess_status(key, album, assignment.fit)
                if status is not None and status.active and status.pending_images > 0:
                    entry["preprocessing"] = status.to_dict()
        else:
            image = self.library.find_image(
                image_id=assignment.image_id,
                source=assignment.source,
            )
            if image is not None:
                entry["image_name"] = image.display_name
        return entry

    async def handle_api_screens(self, request: web.Request) -> web.Response:
        now = time.time()
        result = []
        for key, info in self.state.screens.items():
            assignment = self.state.assignments.get(key)
            result.append({
                "id": screen_id(key),
                "width": info.width,
                "height": info.height,
                "colour_scheme": info.colour_scheme,
                "firmware_id": info.firmware_id,
                "firmware_version": info.firmware_version,
                "last_seen_seconds_ago": round(now - info.last_seen),
                "assignment": self._assignment_payload(key, assignment) if assignment else None,
            })
        return web.json_response(result)

    async def handle_api_assign(self, request: web.Request) -> web.Response:
        data = await request.json()
        raw_screen_id = data.get("screen_id")
        assign_type = data.get("type", "image")
        source = str(data.get("source", "")).strip()
        image_id = data.get("image_id")
        fit = "cover" if data.get("fit") == "cover" else "contain"
        poll_interval = int(data.get("poll_interval", 5) or 5)

        if not raw_screen_id:
            return web.json_response({"error": "screen_id required"}, status=400)

        key = key_from_id(raw_screen_id)
        if key is None:
            return web.json_response({"error": "Invalid screen_id"}, status=400)

        if assign_type == "album":
            if not source:
                return web.json_response({"error": "source required"}, status=400)
            if source not in self.state.albums:
                return web.json_response({"error": "Album not found"}, status=404)
            assignment = ScreenAssignment(type="album", source=source, fit=fit)
            self.state.assignments[key] = assignment
            self.state.album_state.pop(key, None)
        else:
            image = self.library.find_image(image_id=image_id) if image_id else None
            if image is not None:
                source = image.source
            if not source:
                return web.json_response({"error": "source required"}, status=400)
            if image is None:
                image = self.library.find_image(source=source)
            source_type = image.type if image is not None else ("url" if is_url(source) else "file")
            assignment = ScreenAssignment(
                type="image",
                source=source,
                image_id=image.id if image is not None else None,
                source_type=source_type,
                fit=fit,
                poll_interval=poll_interval,
            )
            self.state.assignments[key] = assignment

        self.pipeline.clear_caches_for_screen(key)
        self.display.schedule_assignment_preprocess(key, assignment)
        self.library.save_assignments()

        LOGGER.info(
            "Assigned %s (%s) to screen %s [fit=%s]",
            source,
            assign_type,
            raw_screen_id,
            fit,
        )
        return web.json_response({"ok": True})

    async def handle_api_unassign(self, request: web.Request) -> web.Response:
        data = await request.json()
        raw_screen_id = data.get("screen_id")
        if not raw_screen_id:
            return web.json_response({"error": "screen_id required"}, status=400)

        key = key_from_id(raw_screen_id)
        if key is None:
            return web.json_response({"error": "Invalid screen_id"}, status=400)

        self.state.assignments.pop(key, None)
        self.state.album_state.pop(key, None)
        self.library.save_assignments()
        return web.json_response({"ok": True})

    async def handle_api_album_next(self, request: web.Request) -> web.Response:
        data = await request.json()
        raw_screen_id = data.get("screen_id")
        if not raw_screen_id:
            return web.json_response({"error": "screen_id required"}, status=400)

        key = key_from_id(raw_screen_id)
        if key is None:
            return web.json_response({"error": "Invalid screen_id"}, status=400)

        assignment = self.state.assignments.get(key)
        if assignment is None or assignment.type != "album":
            return web.json_response({"error": "Screen is not assigned an album"}, status=400)

        album = self.state.albums.get(assignment.source)
        if album is None:
            return web.json_response({"error": "Album not found"}, status=404)

        advanced = self.display.advance_album_state(key, album)
        if advanced is None:
            return web.json_response({"error": "Album has no images"}, status=400)

        self.display.schedule_assignment_preprocess(key, assignment)
        LOGGER.info("Advanced album for %s to %s", raw_screen_id, log_source(advanced.source))
        return web.json_response({"ok": True})

    async def handle_api_albums(self, request: web.Request) -> web.Response:
        return web.json_response([album.to_dict() for album in self.state.albums.values()])

    async def handle_api_album_create(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not name:
            return web.json_response({"error": "name required"}, status=400)

        album_id = uuid.uuid4().hex[:8]
        album = AlbumRecord(
            id=album_id,
            name=name,
            images=self.library.normalize_album_images(data.get("images", [])),
            transition_interval=int(data.get("transition_interval", 60) or 60),
            shuffle=bool(data.get("shuffle", False)),
        )
        self.state.albums[album_id] = album
        self.library.save_albums()
        return web.json_response(album.to_dict())

    async def handle_api_album_update(self, request: web.Request) -> web.Response:
        album_id = request.match_info["album_id"]
        album = self.state.albums.get(album_id)
        if album is None:
            return web.json_response({"error": "Not found"}, status=404)

        data = await request.json()
        if "name" in data:
            album.name = str(data["name"])

        old_sources = {entry.source for entry in album.images}
        if "images" in data:
            album.images = self.library.normalize_album_images(data["images"])
        new_sources = {entry.source for entry in album.images}
        images_changed = old_sources != new_sources

        if "transition_interval" in data:
            album.transition_interval = int(data["transition_interval"] or 60)
        if "shuffle" in data:
            album.shuffle = bool(data["shuffle"])

        # Only clear caches for sources that were removed from the album.
        # New/existing sources will be handled by schedule_assignment_preprocess.
        removed_sources = old_sources - new_sources
        for source in removed_sources:
            self.pipeline.clear_caches_for_source(source)

        for key, assignment in self.state.assignments.items():
            if assignment.type == "album" and assignment.source == album_id:
                if images_changed:
                    self.state.album_state.pop(key, None)
                self.display.schedule_assignment_preprocess(key, assignment)

        self.library.save_albums()
        return web.json_response(album.to_dict())

    async def handle_api_album_delete(self, request: web.Request) -> web.Response:
        album_id = request.match_info["album_id"]
        if album_id not in self.state.albums:
            return web.json_response({"error": "Not found"}, status=404)

        to_remove = [
            key
            for key, assignment in self.state.assignments.items()
            if assignment.type == "album" and assignment.source == album_id
        ]
        for key in to_remove:
            self.state.assignments.pop(key, None)
            self.state.album_state.pop(key, None)

        del self.state.albums[album_id]
        self.library.save_albums()
        self.library.save_assignments()
        return web.json_response({"ok": True})

    async def handle_api_images(self, request: web.Request) -> web.Response:
        ordered = sorted(
            self.state.images.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return web.json_response([self.library.serialize_image(item) for item in ordered])

    async def handle_api_upload(self, request: web.Request) -> web.Response:
        post = await request.post()
        image = post.get("image")
        if image is None or not hasattr(image, "file"):
            return web.json_response({"error": "No image field"}, status=400)

        safe_name = self.library.make_unique_filename(image.filename or "upload.png")
        dest = self.state.paths.upload_dir / safe_name
        content = image.file.read()
        dest.write_bytes(content)

        image_id = uuid.uuid4().hex[:8]
        item = ImageRecord(
            id=image_id,
            name=Path(safe_name).stem,
            type="file",
            source=str(dest),
            filename=safe_name,
            created_at=time.time(),
        )

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.library.generate_library_thumbnail, image_id, str(dest))
        except Exception as err:
            dest.unlink(missing_ok=True)
            return web.json_response({"error": f"Failed to process image: {err}"}, status=400)

        self.state.images[image_id] = item
        self.library.save_images()
        LOGGER.info("Uploaded %s (%d bytes)", safe_name, len(content))
        return web.json_response({"ok": True, "image": self.library.serialize_image(item)})

    async def handle_api_image_url(self, request: web.Request) -> web.Response:
        data = await request.json()
        source = str(data.get("url", "")).strip()
        name = str(data.get("name", "")).strip()

        if not source:
            return web.json_response({"error": "url required"}, status=400)
        if not is_url(source):
            return web.json_response({"error": "URL must start with http:// or https://"}, status=400)

        image_id = uuid.uuid4().hex[:8]
        item = ImageRecord(
            id=image_id,
            name=name,
            type="url",
            source=source,
            created_at=time.time(),
        )

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.library.generate_library_thumbnail, image_id, source)
        except Exception as err:
            LOGGER.warning("Failed to fetch URL image %s: %s", source, err)
            return web.json_response({"error": f"Failed to fetch image: {err}"}, status=400)

        self.state.images[image_id] = item
        self.library.save_images()
        return web.json_response({"ok": True, "image": self.library.serialize_image(item)})

    async def handle_api_uploads(self, request: web.Request) -> web.Response:
        files = []
        ordered = sorted(
            self.state.images.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        for item in ordered:
            if item.type == "file" and item.filename:
                files.append({"name": item.filename, "path": item.source})
        return web.json_response(files)

    async def handle_api_image_update(self, request: web.Request) -> web.Response:
        image_id = request.match_info["image_id"]
        item = self.state.images.get(image_id)
        if item is None:
            return web.json_response({"error": "Not found"}, status=404)

        data = await request.json()
        if "name" not in data:
            return web.json_response({"error": "name required"}, status=400)

        item.name = str(data.get("name", "")).strip()
        self.library.save_images()
        return web.json_response({"ok": True, "image": self.library.serialize_image(item)})

    async def handle_api_image_delete(self, request: web.Request) -> web.Response:
        image_id = request.match_info["image_id"]
        item = self.state.images.get(image_id)
        if item is None:
            return web.json_response({"error": "Not found"}, status=404)

        source = item.source
        if item.type == "file":
            Path(source).unlink(missing_ok=True)

        thumb_path = self.library.image_thumb_path(image_id)
        thumb_path.unlink(missing_ok=True)

        for key, assignment in list(self.state.assignments.items()):
            if assignment.type != "image":
                continue
            if assignment.image_id == image_id or assignment.source == source:
                self.state.assignments.pop(key, None)

        self.pipeline.clear_caches_for_source(source)

        changed_albums: set[str] = set()
        for album in self.state.albums.values():
            original_count = len(album.images)
            album.images = [
                entry
                for entry in album.images
                if entry.image_id != image_id and entry.source != source
            ]
            if len(album.images) != original_count:
                changed_albums.add(album.id)

        for key, assignment in self.state.assignments.items():
            if assignment.type == "album" and assignment.source in changed_albums:
                self.state.album_state.pop(key, None)
                self.display.schedule_assignment_preprocess(key, assignment)
        self.state.images.pop(image_id, None)
        self.library.save_images()
        self.library.save_albums()
        self.library.save_assignments()
        return web.json_response({"ok": True})

    async def handle_upload_file(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        safe_name = "".join(char for char in filename if char.isalnum() or char in ".-_")
        path = self.state.paths.upload_dir / safe_name
        if not path.is_file():
            return web.Response(status=404)
        return web.FileResponse(path)

    async def handle_thumbnail(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        safe_name = "".join(char for char in filename if char.isalnum() or char in ".-_")
        stem = Path(safe_name).stem
        thumb_path = self.state.paths.thumb_dir / f"{stem}.jpg"
        if not thumb_path.is_file():
            original = self.state.paths.upload_dir / safe_name
            if original.is_file():
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.library.generate_thumbnail, original)
            if not thumb_path.is_file():
                return web.Response(status=404)
        return web.FileResponse(thumb_path)

    async def handle_thumbnail_by_id(self, request: web.Request) -> web.Response:
        image_id = request.match_info["image_id"]
        item = self.state.images.get(image_id)
        if item is None:
            return web.Response(status=404)

        thumb_path = self.library.image_thumb_path(image_id)
        if not thumb_path.is_file():
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self.library.generate_library_thumbnail, image_id, item.source)
            except Exception:
                return web.Response(status=404)
        return web.FileResponse(thumb_path)
