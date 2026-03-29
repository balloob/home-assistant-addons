from __future__ import annotations

from pathlib import Path


def is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def log_source(source: str) -> str:
    if is_url(source):
        return source
    return Path(source).name or source

