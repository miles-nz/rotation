from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from core.paths import DATA_DIR

SNOOZE_DAYS = 30

logger = logging.getLogger(__name__)

# Tracks Playlist Diff shouldn't suggest for a while ("not right now", as
# opposed to adding them somewhere), keyed by uri. Applies to every diff -
# standalone, Cascade and the scheduled auto-scan - whichever playlists are
# picked. On disk so it survives redeploys.
SNOOZE_PATH = DATA_DIR / "diff_snoozes.json"

_lock = threading.Lock()


def _format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _format(datetime.now(timezone.utc))


def _read() -> dict[str, dict]:
    if not SNOOZE_PATH.exists():
        return {}
    try:
        return json.loads(SNOOZE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("diff snoozes file unreadable, starting fresh", exc_info=True)
        return {}


def _save(snoozes: dict[str, dict]) -> None:
    now = _now()
    SNOOZE_PATH.write_text(json.dumps({u: s for u, s in snoozes.items() if s["until"] > now}))


def _load() -> dict[str, dict]:
    """Unexpired snoozes. Expired ones are written out of the file as soon
    as they're noticed, so it doesn't keep growing between snoozes. Call
    with _lock held."""
    snoozes = _read()
    now = _now()
    active = {u: s for u, s in snoozes.items() if s["until"] > now}
    if len(active) != len(snoozes):
        logger.info("pruning %d expired diff snooze(s)", len(snoozes) - len(active))
        _save(active)
    return active


def snooze(tracks: list[dict]) -> int:
    """Hides tracks from Playlist Diff for SNOOZE_DAYS (restarting the clock
    for any already snoozed). Returns how many were snoozed."""
    if not tracks:
        return 0
    until = _format(datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS))
    with _lock:
        snoozes = _load()
        for t in tracks:
            snoozes[t["uri"]] = {
                "uri": t["uri"],
                "name": t["name"],
                "artists": t["artists"],
                "album": t.get("album") or "",
                "until": until,
            }
        _save(snoozes)
    return len(tracks)


def unsnooze(uri: str) -> None:
    with _lock:
        snoozes = _load()
        if snoozes.pop(uri, None) is not None:
            _save(snoozes)


def active() -> dict[str, dict]:
    with _lock:
        return _load()


def list_active() -> list[dict]:
    """Active snoozes, soonest to expire first."""
    return sorted(active().values(), key=lambda s: s["until"])
