from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable

from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# Settings changed from the app's UI - per-tool default playlists and the
# scheduler times. On the volume via DATA_DIR so they survive redeploys.
# Locally DATA_DIR is the repo root, so this file is gitignored: it holds
# playlist IDs, which must never land in the public repo.
#
# A key that's missing falls back to its env var (the old way of setting
# these, kept so a lost volume degrades to those values). A key stored as
# null means "explicitly none" and wins over the env var.
SETTINGS_PATH = DATA_DIR / "settings.json"

_lock = threading.Lock()


def _read_strict() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    data = json.loads(SETTINGS_PATH.read_text())
    if not isinstance(data, dict):
        raise ValueError("settings file isn't a JSON object")
    return data


def _read() -> dict[str, Any]:
    try:
        return _read_strict()
    except (ValueError, OSError):
        logger.warning("settings file unreadable, falling back to env vars", exc_info=True)
        return {}


def get(key: str, fallback: Callable[[], Any] = lambda: None) -> Any:
    """The stored value for key, or fallback() if it was never set here.
    Read fresh every call - the file is tiny, and this way a change from the
    UI reaches the schedulers' threads with nothing to invalidate."""
    data = _read()
    if key in data:
        return data[key]
    return fallback()


def _update(changes: Callable[[dict[str, Any]], None]) -> None:
    """Applies changes(data) to the stored settings and writes them back.
    Write-then-rename, so a crash or redeploy mid-write can't leave a
    half-written file behind."""
    with _lock:
        try:
            data = _read_strict()
        except (ValueError, OSError):
            # Keep the unreadable file rather than silently overwriting
            # every other saved setting with just this change.
            bad = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".unreadable")
            logger.warning("settings file unreadable, moving it to %s and starting fresh", bad, exc_info=True)
            os.replace(SETTINGS_PATH, bad)
            data = {}
        changes(data)
        tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, SETTINGS_PATH)


def put(key: str, value: Any) -> None:
    _update(lambda data: data.__setitem__(key, value))


def seed_missing(values: dict[str, Any]) -> list[str]:
    """Stores each of values whose key has never been set here, leaving
    every key that has (even to null) untouched. Returns the keys added -
    empty, with no write, when there's nothing new."""
    if all(key in _read() for key in values):
        return []
    added: list[str] = []

    def add_missing(data: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in data:
                data[key] = value
                added.append(key)

    _update(add_missing)
    return added
