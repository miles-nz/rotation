from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# Duplicate groups marked "not a duplicate", keyed by the group's key (see
# duplicates). Each remembers the exact copies (playlist + uri) it covered:
# the group stays hidden while every copy a scan finds is one of those, so
# adding the song to another playlist, or another release turning up,
# brings it back for a fresh look. Doesn't expire. Applies to every scan -
# standalone, Cascade and the scheduled auto-scan. On disk so it survives
# redeploys.
NOT_DUPLICATES_PATH = DATA_DIR / "not_duplicates.json"

_lock = threading.Lock()


def _read() -> dict[str, dict]:
    if not NOT_DUPLICATES_PATH.exists():
        return {}
    try:
        return json.loads(NOT_DUPLICATES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("not-duplicates file unreadable, starting fresh", exc_info=True)
        return {}


def _save(marks: dict[str, dict]) -> None:
    NOT_DUPLICATES_PATH.write_text(json.dumps(marks))


def copies(group: dict) -> list[str]:
    """A duplicate group's copies, as "playlist_id::uri" strings."""
    return [
        f"{occ['playlist_id']}::{release['uri']}"
        for release in group["releases"]
        for occ in release["occurrences"]
    ]


def mark(groups: list[dict]) -> int:
    """Marks duplicate groups as not duplicates (replacing any earlier mark
    for the same group). Returns how many were marked."""
    if not groups:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        marks = _read()
        for group in groups:
            marks[group["key"]] = {
                "key": group["key"],
                "name": group["name"],
                "artists": group["artists"],
                "copies": copies(group),
                "playlist_names": sorted(
                    {
                        occ["playlist_name"]
                        for release in group["releases"]
                        for occ in release["occurrences"]
                    }
                ),
                "marked_at": now,
            }
        _save(marks)
    return len(groups)


def unmark(key: str) -> None:
    with _lock:
        marks = _read()
        if marks.pop(key, None) is not None:
            _save(marks)


def active() -> dict[str, dict]:
    with _lock:
        return _read()


def list_active() -> list[dict]:
    """Every mark, most recent first."""
    return sorted(active().values(), key=lambda m: m["marked_at"], reverse=True)


def is_marked(group: dict, marks: dict[str, dict]) -> bool:
    """Whether some mark covers every copy of group - i.e. nothing new has
    turned up since it was marked. Matched on copies rather than key, so a
    group that's since lost a copy (and so has a different key) still
    counts."""
    group_copies = set(copies(group))
    return any(group_copies <= set(m["copies"]) for m in marks.values())
