from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web
from opendisplay.wifi import DEFAULT_PORT, OpenDisplayServer

from .display import DisplayService
from .image_pipeline import ImagePipeline
from .library import LibraryStore
from .state import AppState, DISPLAY_POLL_INTERVAL, detect_paths, load_options
from .web import WebHandlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)


async def run() -> int:
    root_dir = Path(__file__).resolve().parent.parent
    paths = detect_paths(root_dir)
    state = AppState(paths=paths)
    library = LibraryStore(state)
    pipeline = ImagePipeline(state, library)
    display = DisplayService(state, pipeline)
    web_handlers = WebHandlers(state, library, pipeline, display)

    LOGGER.info(
        "Using %s data directory: %s",
        "add-on" if paths.is_addon else "local",
        paths.data_dir,
    )
    options = load_options(paths)

    library.load_images()
    library.sync_images()
    library.load_albums()
    library.load_assignments()
    display.warm_assignment_caches()

    od_port = options.get("opendisplay_port", DEFAULT_PORT)
    od_server = OpenDisplayServer(
        port=od_port,
        image_provider=display.image_provider,
        poll_interval=DISPLAY_POLL_INTERVAL,
        mdns=True,
    )
    await od_server.start()
    LOGGER.info("OpenDisplay server started on port %d", od_server.actual_port)

    app = web_handlers.create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    LOGGER.info("Web UI started on port 8099")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled_signals: list[signal.Signals] = []
    exit_code = 0

    def request_shutdown(sig: signal.Signals) -> None:
        nonlocal exit_code
        exit_code = 130 if sig is signal.SIGINT else 143
        if shutdown_event.is_set():
            os._exit(exit_code)
            return
        LOGGER.info("Shutdown requested via %s", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig)
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
            state.preprocess_executor.shutdown(wait=False, cancel_futures=True)
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
