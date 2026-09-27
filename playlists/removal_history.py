from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone

from spotipy import Spotify

from core.paths import DATA_DIR
import playlists.playlist_cache as playlist_cache_module

ADD_BATCH_SIZE = 100

# Enough to cover "that song's been missing for a week or two" without the
# file (and the history page) growing forever.
MAX_ENTRIES = 20

logger = logging.getLogger(__name__)

# Every track this app removes from a playlist (Duplicate Finder, Playlist
# Cleanup, and those same steps inside a Cascade) is logged here, newest
# first, so a wrong rule can still be spotted and reverted days later when
# the missing song is actually noticed. On disk so it survives redeploys.
HISTORY_PATH = DATA_DIR / "removal_history.json"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("removal history file unreadable, starting fresh", exc_info=True)
        return []


def _save(entries: list[dict]) -> None:
    HISTORY_PATH.write_text(json.dumps(entries[:MAX_ENTRIES]))


def snapshot_removed(
    playlist_id: str, playlist_name: str, tracks: list[dict], removed_uris
) -> dict:
    """tracks: the playlist's full track list *before* removal, in order.

    One "removed" entry per occurrence (remove_all_occurrences drops every
    copy of a uri, so a uri in the playlist twice yields two entries), each
    with its original index so a restore can put it back in the same spot
    if the playlist hasn't changed since."""
    removed_uris = set(removed_uris)
    return {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "original_uris": [t["uri"] for t in tracks],
        "removed": [
            {
                "position": i,
                "uri": t["uri"],
                "name": t["name"],
                "artists": t["artists"],
                "album": t.get("album") or "",
                "is_local": bool(t.get("is_local")),
                "status": "removed",
            }
            for i, t in enumerate(tracks)
            if t["uri"] in removed_uris
        ],
    }


def record(source: str, snapshots: list[dict]) -> str | None:
    """Adds a history entry for snapshots (from snapshot_removed) and
    returns its id, or None if nothing was removed. Callers record *before*
    touching Spotify: if the removal then fails partway, the entry lists
    some tracks that are still there, which restore just skips - better
    than a removal that happened but was never logged."""
    snapshots = [s for s in snapshots if s["removed"]]
    if not snapshots:
        return None
    entry = {
        "id": uuid.uuid4().hex,
        "source": source,
        "created_at": _now(),
        "playlists": snapshots,
    }
    with _lock:
        _save([entry] + _load())
    return entry["id"]


def load_history() -> list[dict]:
    with _lock:
        return _load()


def _contiguous_runs(entries: list[dict]) -> list[list[dict]]:
    """entries sorted by position -> runs of consecutive positions, so each
    run can go back in with one positioned add call."""
    runs: list[list[dict]] = []
    for entry in entries:
        if runs and entry["position"] == runs[-1][-1]["position"] + 1:
            runs[-1].append(entry)
        else:
            runs.append([entry])
    return runs


def _restore_playlist(sp: Spotify, snapshot: dict, selected: list[dict]) -> dict:
    """Re-adds selected (a subset of snapshot["removed"], status "removed")
    to the playlist and updates their status in place."""
    playlist_id = snapshot["playlist_id"]
    current_tracks = playlist_cache_module.get_playlist(sp, playlist_id)["tracks"]
    current_uris = [t["uri"] for t in current_tracks]

    # What the playlist should look like if nothing but this app's removal
    # (plus any earlier restores from this same entry) has touched it.
    missing_positions = {e["position"] for e in snapshot["removed"] if e["status"] == "removed"}
    expected_uris = [
        uri for i, uri in enumerate(snapshot["original_uris"]) if i not in missing_positions
    ]

    # Already back (e.g. re-added by hand) -> don't add another copy. Counts
    # rather than presence, since a track that was in the playlist twice
    # can have one copy restored and the other still missing.
    extra = Counter(current_uris)
    extra.subtract(expected_uris)
    already_back, to_add = [], []
    for e in sorted(selected, key=lambda e: e["position"]):
        if extra[e["uri"]] > 0:
            extra[e["uri"]] -= 1
            already_back.append(e)
        else:
            to_add.append(e)

    # Original positions are only trustworthy if the playlist is still
    # exactly that. Anything else - tracks added, reordered, removed in
    # Spotify since - and the recorded indices no longer line up, so the
    # tracks just go on the end instead.
    in_place = current_uris == expected_uris

    if in_place:
        # Inserting in ascending original-index order means every earlier
        # track is already back in place by the time a given run goes in,
        # so its original index is right - less one for each earlier
        # removed track that's staying out (not selected, or a local file).
        adding = {e["position"] for e in to_add}
        staying_out = sorted(p for p in missing_positions if p not in adding)
        for run in _contiguous_runs(to_add):
            for i in range(0, len(run), ADD_BATCH_SIZE):
                batch = run[i : i + ADD_BATCH_SIZE]
                start = batch[0]["position"] - sum(
                    1 for p in staying_out if p < batch[0]["position"]
                )
                sp.playlist_add_items(playlist_id, [e["uri"] for e in batch], position=start)
    else:
        uris = [e["uri"] for e in to_add]
        for i in range(0, len(uris), ADD_BATCH_SIZE):
            sp.playlist_add_items(playlist_id, uris[i : i + ADD_BATCH_SIZE])

    now = _now()
    for e in to_add:
        e["status"] = "restored"
        e["restored_at"] = now
    for e in already_back:
        e["status"] = "already_back"
        e["restored_at"] = now

    logger.info(
        "playlist %s: restored %d track(s) %s, %d already back",
        playlist_id,
        len(to_add),
        "at their original positions" if in_place else "at the end",
        len(already_back),
    )

    # No refresh_after_mutation here: the history only keeps enough metadata
    # to display a track, not the full dict the cache holds. The add changed
    # the playlist's snapshot_id, so the next read re-fetches it anyway -
    # restores are rare enough that the extra fetch doesn't matter.

    return {
        "playlist_name": snapshot["playlist_name"],
        "restored": len(to_add),
        "already_back": len(already_back),
        "in_place": in_place,
    }


def restore(sp: Spotify, entry_id: str, keys: set[str] | None = None) -> dict | None:
    """Restores tracks from history entry entry_id. keys: track keys
    ("playlist_id::position") to restore, or None for everything in the
    entry that's still removed. Local files are never restorable (the Web
    API can't add them). Returns {"source", "created_at", "playlists":
    [per-playlist summary]}, or None if the entry no longer exists."""
    with _lock:
        entries = _load()
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if entry is None:
            return None

        summaries = []
        try:
            for snapshot in entry["playlists"]:
                selected = [
                    e
                    for e in snapshot["removed"]
                    if e["status"] == "removed"
                    and not e["is_local"]
                    and (keys is None or f"{snapshot['playlist_id']}::{e['position']}" in keys)
                ]
                if selected:
                    summaries.append(_restore_playlist(sp, snapshot, selected))
        finally:
            # Save whatever got restored even if a later playlist failed,
            # so those tracks aren't offered (and re-added) a second time.
            _save(entries)

    return {"source": entry["source"], "created_at": entry["created_at"], "playlists": summaries}
