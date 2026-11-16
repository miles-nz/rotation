from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from core.paths import DATA_DIR

SNOOZE_DAYS = 30

logger = logging.getLogger(__name__)

# What Find More Like This shouldn't suggest: snoozed tracks or albums
# (keyed by uri, for SNOOZE_DAYS) and "never suggest" artists (keyed by
# find_more_like_this.artist_key, kept until undone). Separate from Playlist
# Diff's snoozes - snoozing a suggestion here means "not this new song right
# now", which has nothing to do with which of your own songs Diff suggests.
HIDDEN_PATH = DATA_DIR / "find_more_hidden.json"

_lock = threading.Lock()


def _format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _format(datetime.now(timezone.utc))


def _read() -> dict:
    if not HIDDEN_PATH.exists():
        return {"snoozes": {}, "blocked": {}}
    try:
        data = json.loads(HIDDEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("find more hidden file unreadable, starting fresh", exc_info=True)
        return {"snoozes": {}, "blocked": {}}
    return {"snoozes": data.get("snoozes") or {}, "blocked": data.get("blocked") or {}}


def _write(data: dict) -> None:
    HIDDEN_PATH.write_text(json.dumps(data))


def _load() -> dict:
    """Current data with expired snoozes dropped (and written out, so the
    file doesn't keep growing). Call with _lock held."""
    data = _read()
    now = _now()
    active = {u: s for u, s in data["snoozes"].items() if s["until"] > now}
    if len(active) != len(data["snoozes"]):
        logger.info("pruning %d expired find more snooze(s)", len(data["snoozes"]) - len(active))
        data["snoozes"] = active
        _write(data)
    return data


def snooze(items: list[dict]) -> int:
    """Hides tracks/albums ({"uri", "name", "artists"}) for SNOOZE_DAYS,
    restarting the clock for any already snoozed. Returns how many."""
    if not items:
        return 0
    until = _format(datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS))
    with _lock:
        data = _load()
        for item in items:
            data["snoozes"][item["uri"]] = {
                "uri": item["uri"],
                "name": item["name"],
                "artists": item["artists"],
                "until": until,
            }
        _write(data)
    return len(items)


def unsnooze(uri: str) -> None:
    with _lock:
        data = _load()
        if data["snoozes"].pop(uri, None) is not None:
            _write(data)


def block(artists: list[dict]) -> int:
    """Never suggest these artists ({"key", "name"}) again. Returns how many."""
    if not artists:
        return 0
    now = _now()
    with _lock:
        data = _load()
        for artist in artists:
            data["blocked"][artist["key"]] = {"key": artist["key"], "name": artist["name"], "since": now}
        _write(data)
    return len(artists)


def unblock(key: str) -> None:
    with _lock:
        data = _load()
        if data["blocked"].pop(key, None) is not None:
            _write(data)


def snoozed_uris() -> set[str]:
    with _lock:
        return set(_load()["snoozes"])


def blocked_keys() -> set[str]:
    with _lock:
        return set(_load()["blocked"])


def list_snoozes() -> list[dict]:
    """Active snoozes, soonest to expire first."""
    with _lock:
        return sorted(_load()["snoozes"].values(), key=lambda s: s["until"])


def list_blocked() -> list[dict]:
    """Never-suggest artists, alphabetically."""
    with _lock:
        return sorted(_load()["blocked"].values(), key=lambda b: b["name"].lower())
