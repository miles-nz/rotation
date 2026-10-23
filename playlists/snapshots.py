from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from spotipy import Spotify, SpotifyException

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR
from core.sync import apply_diff
import playlists.playlist_cache as playlist_cache_module

logger = logging.getLogger(__name__)

# One JSON file per snapshot under snapshots/<playlist_id>/, plus an index
# (names, dates, a hash of each snapshot's track order) so the Snapshots
# page and the daily "has it changed?" check never have to open every
# snapshot file. On the Railway volume via DATA_DIR, so it all survives
# redeploys.
SNAPSHOT_DIR = DATA_DIR / "snapshots"
INDEX_PATH = SNAPSHOT_DIR / "index.json"

LIKED_SONGS_ID = "liked_songs"
LIKED_SONGS_NAME = "Liked Songs"

RETENTION_DAYS = 90
ADD_BATCH_SIZE = 100
REMOVE_BATCH_SIZE = 100

# A rollback normally removes only what has to go and re-inserts only what
# has to come back, so every other track keeps its "date added". If the
# playlist has been shuffled around enough that this would take more API
# calls than this, it replaces the whole playlist in a few calls instead
# (resetting every track's date added) - the confirmation screen says which.
MAX_MINIMAL_ROLLBACK_CALLS = 50

_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

_index_lock = threading.Lock()
# The scheduled run and "Snapshot now" both go through take_snapshots;
# this stops them overlapping.
_run_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _uris_hash(tracks: list[dict]) -> str:
    return hashlib.sha256("\n".join(t["uri"] for t in tracks).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data) -> None:
    # Write-then-rename, so a crash or redeploy mid-write can't leave a
    # half-written index behind.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("snapshot index unreadable, starting fresh", exc_info=True)
        return {}


def _save_index(index: dict) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(INDEX_PATH, index)


def _snapshot_path(playlist_id: str, snapshot_id: str) -> Path:
    return SNAPSHOT_DIR / playlist_id / f"{snapshot_id}.json"


def _snapshot_track(track: dict) -> dict:
    return {
        "uri": track["uri"],
        "name": track["name"],
        "artists": track["artists"],
        "album": track.get("album") or "",
        "added_at": track.get("added_at"),
        "is_local": bool(track.get("is_local")),
    }


def record(
    playlist_id: str,
    name: str,
    tracks: list[dict],
    reason: str,
    now: datetime | None = None,
) -> dict:
    """Stores a snapshot of tracks (in order) unconditionally and returns
    its index entry. reason: "daily" or "pre_rollback"."""
    now = now or _now()
    entry = {
        # Microseconds so a pre-rollback snapshot can't collide with a
        # daily one taken in the same second.
        "id": now.strftime("%Y%m%dT%H%M%S%fZ"),
        "taken_at": _iso(now),
        "reason": reason,
        "track_count": len(tracks),
        "uris_hash": _uris_hash(tracks),
    }
    with _index_lock:
        (SNAPSHOT_DIR / playlist_id).mkdir(parents=True, exist_ok=True)
        _atomic_write(
            _snapshot_path(playlist_id, entry["id"]),
            {
                "playlist_id": playlist_id,
                "name": name,
                "taken_at": entry["taken_at"],
                "reason": reason,
                "tracks": [_snapshot_track(t) for t in tracks],
            },
        )
        index = _load_index()
        playlist = index.setdefault(playlist_id, {"snapshots": []})
        playlist["name"] = name
        playlist["last_seen"] = entry["taken_at"]
        playlist["snapshots"].append(entry)
        _save_index(index)
    return entry


def record_if_changed(
    playlist_id: str, name: str, tracks: list[dict], now: datetime | None = None
) -> str | None:
    """Stores a daily snapshot only if the track list or its order differs
    from the playlist's latest snapshot. Either way, notes the playlist as
    seen now (see prune). Returns "new" (its first snapshot), "changed", or
    None if nothing was stored."""
    now = now or _now()
    with _index_lock:
        index = _load_index()
        playlist = index.get(playlist_id)
        if playlist and playlist["snapshots"] and playlist["snapshots"][-1]["uris_hash"] == _uris_hash(tracks):
            playlist["name"] = name
            playlist["last_seen"] = _iso(now)
            _save_index(index)
            return None
    record(playlist_id, name, tracks, "daily", now)
    return "changed" if playlist and playlist["snapshots"] else "new"


def prune(now: datetime | None = None) -> int:
    """Deletes snapshots older than RETENTION_DAYS, and returns how many.

    A snapshot shows what the playlist looked like from when it was taken
    until the next snapshot replaced it, so its age is counted from that
    replacement. The latest snapshot of a playlist still being seen is
    never deleted - it's still what the playlist looks like - but once a
    playlist stops turning up (deleted, or no longer yours) its last
    snapshot goes RETENTION_DAYS after it was last seen."""
    now = now or _now()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    deleted = 0
    with _index_lock:
        index = _load_index()
        for playlist_id in list(index):
            playlist = index[playlist_id]
            snapshots = playlist["snapshots"]
            kept = []
            for i, entry in enumerate(snapshots):
                replaced_at = (
                    snapshots[i + 1]["taken_at"] if i + 1 < len(snapshots) else playlist["last_seen"]
                )
                if _parse_iso(replaced_at) < cutoff:
                    _snapshot_path(playlist_id, entry["id"]).unlink(missing_ok=True)
                    deleted += 1
                else:
                    kept.append(entry)
            if kept:
                playlist["snapshots"] = kept
            else:
                del index[playlist_id]
                try:
                    (SNAPSHOT_DIR / playlist_id).rmdir()
                except OSError:
                    pass
        _save_index(index)
    return deleted


def _owned_playlists(sp: Spotify) -> dict[str, str]:
    user_id = sp.current_user()["id"]
    owned: dict[str, str] = {}
    results = sp.current_user_playlists(limit=50)
    while results:
        for playlist in results["items"]:
            if playlist and (playlist.get("owner") or {}).get("id") == user_id:
                owned[playlist["id"]] = playlist["name"]
        results = sp.next(results) if results.get("next") else None
    return owned


def take_snapshots(sp: Spotify, cancel_check: CancelCheck | None = None) -> dict:
    """Snapshots every playlist the logged-in user owns plus Liked Songs,
    storing a new snapshot only for those that changed, then prunes old
    ones. Read-only as far as Spotify is concerned. One playlist failing to
    fetch is logged and skipped rather than losing everyone else's
    snapshot for the day."""
    with _run_lock:
        now = _now()
        owned = _owned_playlists(sp)
        logger.info("snapshots: checking %d playlist(s) you own, plus Liked Songs", len(owned))

        changed: list[str] = []
        new: list[str] = []
        failed: list[str] = []
        for playlist_id, name in owned.items():
            check_cancelled(cancel_check)
            try:
                playlist = playlist_cache_module.get_playlist(sp, playlist_id, cancel_check)
            except SpotifyException:
                logger.exception("snapshots: couldn't fetch playlist '%s', skipping", name)
                failed.append(name)
                continue
            outcome = record_if_changed(playlist_id, playlist["name"], playlist["tracks"], now)
            if outcome:
                (new if outcome == "new" else changed).append(playlist["name"])

        liked = playlist_cache_module.get_liked_songs_tracks(sp, cancel_check)
        outcome = record_if_changed(LIKED_SONGS_ID, LIKED_SONGS_NAME, liked, now)
        if outcome:
            (new if outcome == "new" else changed).append(LIKED_SONGS_NAME)

        pruned = prune(now)
        logger.info(
            "snapshots: %d changed, %d snapshotted for the first time, %d old snapshot(s) deleted",
            len(changed),
            len(new),
            pruned,
        )
        return {
            "checked": len(owned) + 1 - len(failed),
            "changed": changed,
            "new": new,
            "failed": failed,
            "pruned": pruned,
        }


def list_playlists() -> list[dict]:
    """Every playlist with snapshots, Liked Songs first then by name, each
    with its snapshots newest first."""
    with _index_lock:
        index = _load_index()
    playlists = [
        {
            "id": playlist_id,
            "name": playlist["name"],
            "last_seen": playlist["last_seen"],
            "snapshots": list(reversed(playlist["snapshots"])),
        }
        for playlist_id, playlist in index.items()
    ]
    playlists.sort(key=lambda p: (p["id"] != LIKED_SONGS_ID, p["name"].lower()))
    return playlists


def load_snapshot(playlist_id: str, snapshot_id: str) -> dict | None:
    """The snapshot's file contents plus its index entry fields, or None if
    there's no such snapshot. Only ids that are in the index are ever read,
    so a crafted id can't reach outside SNAPSHOT_DIR."""
    if not _ID_RE.match(playlist_id) or not _ID_RE.match(snapshot_id):
        return None
    with _index_lock:
        playlist = _load_index().get(playlist_id)
    if playlist is None:
        return None
    entry = next((e for e in playlist["snapshots"] if e["id"] == snapshot_id), None)
    if entry is None:
        return None
    try:
        data = json.loads(_snapshot_path(playlist_id, snapshot_id).read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("snapshot %s/%s unreadable", playlist_id, snapshot_id, exc_info=True)
        return None
    return {**entry, **data, "id": snapshot_id}


def current_state(
    sp: Spotify, playlist_id: str, cancel_check: CancelCheck | None = None
) -> dict:
    """{"name", "tracks"} for the playlist as it is right now."""
    if playlist_id == LIKED_SONGS_ID:
        return {
            "name": LIKED_SONGS_NAME,
            "tracks": playlist_cache_module.get_liked_songs_tracks(sp, cancel_check),
        }
    try:
        return playlist_cache_module.get_playlist(sp, playlist_id, cancel_check)
    except SpotifyException as exc:
        if exc.http_status == 404:
            raise RuntimeError("This playlist no longer exists on Spotify.") from exc
        raise


def diff(snapshot_tracks: list[dict], current_tracks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (added, removed) since the snapshot. Counts copies rather
    than just presence, so a track that was in the playlist twice and is
    now in it once shows as one removed. Each removed track carries
    "index", its position in the snapshot, which identifies it for
    restore."""
    current_counts = Counter(t["uri"] for t in current_tracks)
    removed = []
    for i, track in enumerate(snapshot_tracks):
        if current_counts[track["uri"]] > 0:
            current_counts[track["uri"]] -= 1
        else:
            removed.append({**track, "index": i})

    snapshot_counts = Counter(t["uri"] for t in snapshot_tracks)
    added = []
    for track in current_tracks:
        if snapshot_counts[track["uri"]] > 0:
            snapshot_counts[track["uri"]] -= 1
        else:
            added.append(track)
    return added, removed


def compare(
    sp: Spotify, playlist_id: str, snapshot_id: str, cancel_check: CancelCheck | None = None
) -> dict:
    snapshot = load_snapshot(playlist_id, snapshot_id)
    if snapshot is None:
        raise RuntimeError("That snapshot no longer exists.")
    current = current_state(sp, playlist_id, cancel_check)
    added, removed = diff(snapshot["tracks"], current["tracks"])
    return {
        "playlist_id": playlist_id,
        "snapshot": snapshot,
        "name": current["name"],
        "current_tracks": current["tracks"],
        "added": added,
        "removed": removed,
    }


def restore(sp: Spotify, playlist_id: str, snapshot_id: str, indices: set[int]) -> dict:
    """Adds back the snapshot tracks at indices (see diff) that are still
    missing - at the end of a playlist, or re-liked for Liked Songs. The
    playlist is re-read first, so anything put back since the comparison
    isn't added twice. Local files are skipped (the Web API can't add
    them)."""
    snapshot = load_snapshot(playlist_id, snapshot_id)
    if snapshot is None:
        raise RuntimeError("That snapshot no longer exists.")
    current = current_state(sp, playlist_id)
    _, removed = diff(snapshot["tracks"], current["tracks"])
    still_missing = {t["index"] for t in removed}
    selected = [t for t in removed if t["index"] in indices and not t["is_local"]]
    uris = [t["uri"] for t in selected]

    if playlist_id == LIKED_SONGS_ID:
        apply_diff(sp, set(uris), set())
    else:
        for i in range(0, len(uris), ADD_BATCH_SIZE):
            sp.playlist_add_items(playlist_id, uris[i : i + ADD_BATCH_SIZE])
        # Restores are rare; let the next read re-fetch rather than building
        # full cache dicts for the added tracks.
        playlist_cache_module.invalidate(playlist_id)

    logger.info("snapshots: restored %d track(s) to '%s'", len(uris), current["name"])
    return {
        "playlist_id": playlist_id,
        "name": current["name"],
        "restored": len(uris),
        "already_back": sum(1 for i in indices if i not in still_missing),
    }


def _runs(indices: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in indices:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def plan_rollback(playlist_id: str, target: list[dict], current: list[dict]) -> dict:
    """Works out what rolling the playlist back from current to target
    (the snapshot's tracks) involves, without touching Spotify.

    For a playlist, it aligns the two track lists (difflib) to find the
    smallest set of tracks to remove and re-insert at their snapshot
    positions, so tracks that stay put keep their date added. A track that
    has only moved is removed and re-inserted, so its date added resets.
    If that would take too many API calls, it replaces the whole playlist
    instead.

    "blocked" lists local files currently in the playlist that the
    rollback would have to remove and re-add - the Web API can't add local
    files, so they'd be lost, and the rollback isn't allowed."""
    extra, missing = diff(target, current)
    plan = {
        "to_add": [t for t in missing if not t["is_local"]],
        "to_remove": extra,
        "unrestorable": [t for t in missing if t["is_local"]],
        "current_signature": _uris_hash(current),
        "unchanged": [t["uri"] for t in target] == [t["uri"] for t in current],
        "method": "liked",
        "moved": 0,
        "blocked": [],
    }
    if playlist_id == LIKED_SONGS_ID:
        return plan

    current_uris = [t["uri"] for t in current]
    target_uris = [t["uri"] for t in target]
    matcher = SequenceMatcher(None, current_uris, target_uris, autojunk=False)
    kept_positions: set[int] = set()
    delete_uris: set[str] = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            kept_positions.update(range(j1, j2))
        else:
            delete_uris.update(current_uris[i1:i2])

    # Removal is by uri (every copy goes), so any target position holding a
    # deleted uri has to be re-inserted too, even inside a matched block.
    inserts = [
        j for j, uri in enumerate(target_uris) if j not in kept_positions or uri in delete_uris
    ]
    skipped = [j for j in inserts if target[j]["is_local"]]
    skipped_set = set(skipped)
    addable = [j for j in inserts if not target[j]["is_local"]]
    target_uri_set = set(target_uris)

    insert_steps = []
    for run in _runs(addable):
        for i in range(0, len(run), ADD_BATCH_SIZE):
            batch = run[i : i + ADD_BATCH_SIZE]
            # Inserting in ascending snapshot order means everything before
            # this batch is already in place, less the local files that
            # can't come back.
            position = batch[0] - sum(1 for s in skipped if s < batch[0])
            insert_steps.append({"position": position, "uris": [target_uris[j] for j in batch]})
    delete_list = sorted(delete_uris)
    minimal_calls = math.ceil(len(delete_list) / REMOVE_BATCH_SIZE) + len(insert_steps)

    if minimal_calls <= MAX_MINIMAL_ROLLBACK_CALLS:
        plan.update(
            {
                "method": "minimal",
                "delete_uris": delete_list,
                "insert_steps": insert_steps,
                "moved": len(addable) - len(plan["to_add"]),
                "blocked": [
                    t for t in current if t["is_local"] and t["uri"] in delete_uris and t["uri"] in target_uri_set
                ],
                "expected_uris": [uri for j, uri in enumerate(target_uris) if j not in skipped_set],
            }
        )
    else:
        addable_uris = [t["uri"] for t in target if not t["is_local"]]
        plan.update(
            {
                "method": "replace",
                "replace_uris": addable_uris,
                "moved": len(addable_uris) - len(plan["to_add"]),
                # Replacing drops every local file in the playlist.
                "blocked": [t for t in current if t["is_local"] and t["uri"] in target_uri_set],
                "expected_uris": addable_uris,
            }
        )
    return plan


def _execute_rollback(sp: Spotify, playlist_id: str, plan: dict) -> None:
    if plan["method"] == "liked":
        apply_diff(
            sp,
            {t["uri"] for t in plan["to_add"]},
            {t["uri"] for t in plan["to_remove"]},
        )
        return

    if plan["method"] == "replace":
        uris = plan["replace_uris"]
        sp.playlist_replace_items(playlist_id, uris[:ADD_BATCH_SIZE])
        for i in range(ADD_BATCH_SIZE, len(uris), ADD_BATCH_SIZE):
            sp.playlist_add_items(playlist_id, uris[i : i + ADD_BATCH_SIZE])
        return

    delete_uris = plan["delete_uris"]
    for i in range(0, len(delete_uris), REMOVE_BATCH_SIZE):
        sp.playlist_remove_all_occurrences_of_items(playlist_id, delete_uris[i : i + REMOVE_BATCH_SIZE])
    for step in plan["insert_steps"]:
        sp.playlist_add_items(playlist_id, step["uris"], position=step["position"])


def rollback(sp: Spotify, playlist_id: str, snapshot_id: str, signature: str) -> dict:
    """Rolls the playlist back to the snapshot. signature is the
    "current_signature" from the plan shown on the confirmation screen; if
    the playlist has changed since, nothing is done and the result's status
    is "changed", so the new plan can be confirmed first.

    The playlist's current state is snapshotted before anything changes,
    so the rollback can itself be rolled back."""
    snapshot = load_snapshot(playlist_id, snapshot_id)
    if snapshot is None:
        raise RuntimeError("That snapshot no longer exists.")
    current = current_state(sp, playlist_id)
    plan = plan_rollback(playlist_id, snapshot["tracks"], current["tracks"])
    result = {"playlist_id": playlist_id, "name": current["name"], "snapshot": snapshot, "plan": plan}
    if plan["current_signature"] != signature:
        return {**result, "status": "changed"}
    if plan["blocked"]:
        return {**result, "status": "blocked"}

    before = record(playlist_id, current["name"], current["tracks"], "pre_rollback")
    try:
        _execute_rollback(sp, playlist_id, plan)
    except Exception as exc:
        raise RuntimeError(
            f"The rollback stopped partway through ({exc}). The playlist as it "
            f"was before the rollback is saved as a snapshot ({before['taken_at']}), "
            "so you can roll back to that from the Snapshots page."
        ) from exc
    logger.info("snapshots: rolled '%s' back to %s", current["name"], snapshot["taken_at"])

    # Re-read the result and check it's what the snapshot says it should
    # be - this also refreshes the cache for the playlist.
    matches = None
    if playlist_id != LIKED_SONGS_ID:
        playlist_cache_module.invalidate(playlist_id)
        after = playlist_cache_module.get_playlist(sp, playlist_id)
        matches = [t["uri"] for t in after["tracks"]] == plan["expected_uris"]
        if not matches:
            logger.warning("snapshots: '%s' doesn't match the snapshot after rollback", current["name"])

    return {**result, "status": "done", "before": before, "matches": matches}
