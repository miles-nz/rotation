from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# On-disk cache of fetched playlist tracks, keyed by playlist id, so a
# playlist whose snapshot_id (Spotify's change-token for its track list)
# hasn't moved since the last run can skip the full paged re-fetch.
CACHE_DIR = DATA_DIR / ".playlist_cache"


def _cache_path(playlist_id: str) -> Path:
    return CACHE_DIR / f"{playlist_id}.json"


def _load_cached_tracks(playlist_id: str, snapshot_id: str) -> list[dict] | None:
    path = _cache_path(playlist_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("snapshot_id") != snapshot_id:
        return None
    return data.get("tracks")


def _save_cached_tracks(playlist_id: str, snapshot_id: str, tracks: list[dict]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(playlist_id).write_text(json.dumps({"snapshot_id": snapshot_id, "tracks": tracks}))


# Superset of every field any cascade step needs, so a playlist is only ever
# paged through once no matter how many steps use it.
_FIELDS = (
    "items(added_at,track(uri,name,artists,explicit,popularity,is_local,"
    "album(name,release_date))),next"
)


def _parse_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def _fetch_playlist_tracks(
    sp: Spotify,
    playlist_id: str,
    playlist_name: str,
    total: int,
    cancel_check: CancelCheck | None,
) -> list[dict]:
    tracks: list[dict] = []
    processed = 0
    # limit=100 is the API max for this endpoint (spotipy's own default is
    # only 50), so this halves the number of paging requests for anything
    # longer than one page.
    results = sp.playlist_items(playlist_id, fields=_FIELDS, limit=100, additional_types=["track"])
    while results:
        for item in results["items"]:
            processed += 1
            track = item.get("track")
            if track and track.get("uri"):
                tracks.append(
                    {
                        "uri": track["uri"],
                        "name": track["name"],
                        "artists": ", ".join(a["name"] for a in track["artists"]),
                        "album": (track.get("album") or {}).get("name") or "",
                        "is_local": bool(track.get("is_local")),
                        "explicit": bool(track.get("explicit")),
                        "popularity": track.get("popularity"),
                        "release_year": _parse_year(
                            (track.get("album") or {}).get("release_date")
                        ),
                        "added_at": item.get("added_at"),
                    }
                )
        # "progress" (rather than a plain message) lets a BackgroundJob
        # update this playlist's bar in place instead of appending a new
        # log line for every page.
        logger.info(
            "playlist '%s': %d/%d tracks fetched",
            playlist_name,
            processed,
            total,
            extra={
                "progress": {
                    "id": playlist_id,
                    "name": playlist_name,
                    "current": processed,
                    "total": total,
                }
            },
        )
        check_cancelled(cancel_check)
        results = sp.next(results) if results.get("next") else None
    return tracks


def get_playlist(
    sp: Spotify, playlist_id: str, cancel_check: CancelCheck | None = None
) -> dict:
    """Returns {"name", "tracks"} for a playlist - the one fetch path every
    tool (standalone or cascade) should go through. Uses the on-disk cache
    when the playlist's snapshot_id (Spotify's change-token for its track
    list) still matches what's cached; otherwise pages through the
    playlist fresh and updates the cache."""
    info = sp.playlist(playlist_id, fields="name,snapshot_id,tracks.total")
    name, snapshot_id = info["name"], info["snapshot_id"]
    total = (info.get("tracks") or {}).get("total") or 0
    tracks = _load_cached_tracks(playlist_id, snapshot_id)
    if tracks is not None:
        logger.info(
            "playlist '%s': unchanged since last fetch, using cached %d track(s)",
            name,
            len(tracks),
        )
    else:
        logger.info("fetching playlist '%s'", name)
        tracks = _fetch_playlist_tracks(sp, playlist_id, name, total, cancel_check)
        _save_cached_tracks(playlist_id, snapshot_id, tracks)
    return {"name": name, "tracks": tracks}


# Fetching N playlists is otherwise N sequential round-trips; a small pool
# overlaps their latency instead, without changing how many requests each
# individual fetch makes.
MAX_CONCURRENT_FETCHES = 5


def get_playlists(
    sp: Spotify, playlist_ids, cancel_check: CancelCheck | None = None
) -> dict[str, dict]:
    """Fetches multiple playlists concurrently - same caching behavior as
    get_playlist for each, just spread across a small thread pool so a
    multi-playlist scan's wall-clock time isn't the sum of each playlist's
    fetch time. Returns {playlist_id: {"name", "tracks"}}."""
    playlist_ids = list(dict.fromkeys(playlist_ids))
    if not playlist_ids:
        return {}
    if len(playlist_ids) == 1:
        return {playlist_ids[0]: get_playlist(sp, playlist_ids[0], cancel_check)}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_FETCHES, len(playlist_ids))) as pool:
        future_to_id = {
            pool.submit(get_playlist, sp, playlist_id, cancel_check): playlist_id
            for playlist_id in playlist_ids
        }
        for future in as_completed(future_to_id):
            results[future_to_id[future]] = future.result()
    return results


def invalidate(playlist_id: str) -> None:
    """Deletes any cached entry for playlist_id, forcing the next read to do
    a full, authoritative re-fetch instead of trusting data known to be
    incomplete (e.g. a just-added track whose metadata couldn't be
    resolved)."""
    _cache_path(playlist_id).unlink(missing_ok=True)


def refresh_after_mutation(sp: Spotify, playlist_id: str, tracks: list[dict]) -> None:
    """Call right after this app adds/removes tracks on playlist_id, passing
    the resulting full track list. Re-reads the snapshot_id the mutation
    just produced and saves the cache against *that* (not the pre-mutation
    one), so the next read's snapshot check matches immediately instead of
    paying for a full re-fetch of a playlist this app just wrote to."""
    snapshot_id = sp.playlist(playlist_id, fields="snapshot_id")["snapshot_id"]
    _save_cached_tracks(playlist_id, snapshot_id, tracks)


def track_details_for_uris(sp: Spotify, uris) -> dict[str, dict]:
    """Batch-fetches full metadata for uris, shaped like the cache's track
    dicts - for callers that only have bare uris (e.g. a standalone "add
    tracks" flow) and need full dicts to build the post-add list to pass to
    refresh_after_mutation. added_at is set to now, since that's when this
    app is adding them.

    Local files can't be looked up this way (sp.tracks only resolves
    catalog ids) and shouldn't reach this function anyway - the Web API
    can't add them to a playlist in the first place - so any local uri
    passed in is silently excluded from the result."""
    uris = [uri for uri in uris if not uri.startswith("spotify:local:")]
    ids = [uri.split(":")[-1] for uri in uris]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    details: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        for track in sp.tracks(ids[i : i + 50])["tracks"]:
            if not track:
                continue
            details[track["uri"]] = {
                "uri": track["uri"],
                "name": track["name"],
                "artists": ", ".join(a["name"] for a in track["artists"]),
                "album": (track.get("album") or {}).get("name") or "",
                "is_local": False,
                "explicit": bool(track.get("explicit")),
                "popularity": track.get("popularity"),
                "release_year": _parse_year((track.get("album") or {}).get("release_date")),
                "added_at": now,
            }
    return details


def _fetch_liked_track_uris(
    sp: Spotify, total: int, cancel_check: CancelCheck | None
) -> set[str]:
    uris: set[str] = set()
    processed = 0
    results = sp.current_user_saved_tracks(limit=50)
    while results:
        for item in results["items"]:
            processed += 1
            track = item.get("track")
            if track and track.get("uri"):
                uris.add(track["uri"])
        logger.info(
            "liked songs: %d/%d tracks fetched",
            processed,
            total,
            extra={
                "progress": {
                    "id": "liked_songs",
                    "name": "Liked Songs",
                    "current": processed,
                    "total": total,
                }
            },
        )
        check_cancelled(cancel_check)
        results = sp.next(results) if results.get("next") else None
    return uris


def _liked_cache_path() -> Path:
    return CACHE_DIR / "liked_songs.json"


def _load_liked_cache() -> dict | None:
    path = _liked_cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_liked_cache(uris) -> None:
    uris = list(uris)
    CACHE_DIR.mkdir(exist_ok=True)
    _liked_cache_path().write_text(json.dumps({"total": len(uris), "uris": uris}))


def get_liked_songs(sp: Spotify, cancel_check: CancelCheck | None = None) -> set[str]:
    """Liked Songs has no snapshot_id, but a single cheap request exposes
    its total count - if that matches what's cached, skip the full paged
    re-fetch. Not airtight (a same-count swap - one add plus one remove -
    slips through undetected), but catches the common "nothing changed"
    case for the cost of one lightweight request."""
    total = sp.current_user_saved_tracks(limit=1)["total"]
    cached = _load_liked_cache()
    if cached is not None and cached.get("total") == total:
        logger.info("liked songs: count unchanged (%d), using cached set", total)
        return set(cached["uris"])
    logger.info("fetching liked songs")
    uris = _fetch_liked_track_uris(sp, total, cancel_check)
    _save_liked_cache(uris)
    return uris


def refresh_liked_songs_after_mutation(to_add, to_remove) -> None:
    """Call right after this app adds/removes Liked Songs, so the cache
    reflects the resulting set (and its total) without a full re-fetch on
    the next read. No-op if nothing's cached yet - the next full read
    starts fresh."""
    cached = _load_liked_cache()
    if cached is None:
        return
    uris = (set(cached["uris"]) - set(to_remove)) | set(to_add)
    _save_liked_cache(uris)


class PlaylistCache:
    """Holds already-fetched playlist tracks (and optionally Liked Songs) so
    a cascade's steps can share one fetch per playlist instead of each step
    re-paging through the same tracks.

    Track dicts: {"uri", "name", "artists", "album", "is_local", "explicit",
    "popularity", "release_year", "added_at"} - a superset covering every
    field any of the five functions look at.
    """

    def __init__(self) -> None:
        self._playlists: dict[str, dict] = {}
        self._liked_songs: set[str] | None = None

    def ensure_playlists(
        self, sp: Spotify, playlist_ids, cancel_check: CancelCheck | None = None
    ) -> None:
        missing = [pid for pid in playlist_ids if pid not in self._playlists]
        if not missing:
            return
        self._playlists.update(get_playlists(sp, missing, cancel_check))
        check_cancelled(cancel_check)

    def ensure_liked_songs(self, sp: Spotify, cancel_check: CancelCheck | None = None) -> None:
        if self._liked_songs is None:
            self._liked_songs = get_liked_songs(sp, cancel_check)

    def playlist(self, playlist_id: str) -> dict:
        entry = self._playlists[playlist_id]
        return {"id": playlist_id, "name": entry["name"], "tracks": entry["tracks"]}

    def playlists(self, playlist_ids) -> list[dict]:
        return [self.playlist(playlist_id) for playlist_id in playlist_ids]

    def liked_song_uris(self) -> set[str]:
        return set(self._liked_songs or set())

    def register_new_playlist(self, playlist_id: str, name: str) -> None:
        self._playlists[playlist_id] = {"name": name, "tracks": []}

    def remove_tracks(self, playlist_id: str, uris) -> None:
        uris = set(uris)
        entry = self._playlists.get(playlist_id)
        if entry:
            entry["tracks"] = [t for t in entry["tracks"] if t["uri"] not in uris]

    def add_tracks(self, playlist_id: str, tracks: list[dict]) -> None:
        entry = self._playlists.setdefault(playlist_id, {"name": "", "tracks": []})
        existing = {t["uri"] for t in entry["tracks"]}
        for track in tracks:
            if track["uri"] not in existing:
                entry["tracks"].append(track)
                existing.add(track["uri"])

    def update_liked_songs(self, to_add, to_remove) -> None:
        if self._liked_songs is None:
            return
        self._liked_songs = (self._liked_songs - set(to_remove)) | set(to_add)
