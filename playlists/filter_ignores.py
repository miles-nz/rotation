from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# Songs Playlist Filter shouldn't offer to add to a given destination
# playlist again, keyed by destination playlist id, then by track uri. Only
# for existing destinations - a "new playlist" destination has no id until
# it's created, and is a different playlist every run anyway. Matched on uri,
# so a different version of the same song still turns up. Doesn't expire.
# Applies to every scan - standalone, Cascade and the scheduled auto-scan.
# On disk so it survives redeploys.
FILTER_IGNORES_PATH = DATA_DIR / "filter_ignores.json"

_lock = threading.Lock()


def _read() -> dict[str, dict]:
    if not FILTER_IGNORES_PATH.exists():
        return {}
    try:
        return json.loads(FILTER_IGNORES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("filter ignores file unreadable, starting fresh", exc_info=True)
        return {}


def _save(ignores: dict[str, dict]) -> None:
    FILTER_IGNORES_PATH.write_text(json.dumps(ignores))


def ignore(playlist_id: str, playlist_name: str, tracks: list[dict]) -> int:
    """tracks: Playlist Filter matches ({"uri", "name", "artists", ...}).
    Ignores each for playlist_id from now on. Returns how many were
    ignored."""
    if not tracks:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        ignores = _read()
        entry = ignores.setdefault(playlist_id, {"tracks": {}})
        # Refreshed every time, so a renamed playlist shows its current name.
        entry["playlist_name"] = playlist_name
        for track in tracks:
            entry["tracks"][track["uri"]] = {
                "uri": track["uri"],
                "name": track["name"],
                "artists": track["artists"],
                "ignored_at": now,
            }
        _save(ignores)
    logger.info("ignoring %d track(s) for playlist %s", len(tracks), playlist_id)
    return len(tracks)


def unignore(playlist_id: str, uri: str) -> None:
    with _lock:
        ignores = _read()
        entry = ignores.get(playlist_id)
        if entry is None or entry["tracks"].pop(uri, None) is None:
            return
        if not entry["tracks"]:
            del ignores[playlist_id]
        _save(ignores)


def ignored_uris(playlist_id: str | None) -> set[str]:
    if not playlist_id:
        return set()
    with _lock:
        return set(_read().get(playlist_id, {}).get("tracks", {}))


def list_active() -> list[dict]:
    """Every ignored track across all destinations, most recent first:
    [{"playlist_id", "playlist_name", "uri", "name", "artists",
    "ignored_at"}]."""
    with _lock:
        ignores = _read()
    rows = [
        {"playlist_id": pid, "playlist_name": entry.get("playlist_name", ""), **track}
        for pid, entry in ignores.items()
        for track in entry["tracks"].values()
    ]
    return sorted(rows, key=lambda r: r["ignored_at"], reverse=True)


def apply_to_scan(result: dict) -> dict:
    """Drops tracks ignored for the scan's destination from a Playlist
    Filter scan result's matches and similar_versions (in place, and
    returned), recording how many in result["ignored_count"]."""
    if result["destination_mode"] != "existing":
        result["ignored_count"] = 0
        return result
    ignored = ignored_uris(result["destination_playlist_id"])
    kept = [m for m in result["matches"] if m["uri"] not in ignored]
    result["ignored_count"] = len(result["matches"]) - len(kept)
    result["matches"] = kept
    result["similar_versions"] = {
        uri: v for uri, v in result.get("similar_versions", {}).items() if uri not in ignored
    }
    return result


def ignore_from_form(result: dict, form) -> tuple[int, set[str]]:
    """Saves the matches ticked "ignore" on the result page. Returns (how
    many were ignored, their uris) - the caller must leave those out of the
    add, even if the add box was somehow submitted too."""
    if result["destination_mode"] != "existing":
        return 0, set()
    chosen = set(form.getlist("ignore"))
    tracks = [m for m in result["matches"] if m["uri"] in chosen]
    count = ignore(
        result["destination_playlist_id"], result["destination_playlist_name"], tracks
    )
    return count, {t["uri"] for t in tracks}
