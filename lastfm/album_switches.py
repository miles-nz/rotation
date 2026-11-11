from __future__ import annotations

import logging
from datetime import datetime, timedelta

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history
from playlists.playlist_swap import normalized_signature

logger = logging.getLogger(__name__)

# Album Swap's "Detect switched albums" time windows, in the order the
# picker shows them. A window bounds when the first album-version play
# (after the single) happened, not when the single was played.
WINDOWS: list[tuple[str, str]] = [
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("180", "Last 180 days"),
    ("365", "Last 365 days"),
    ("ytd", "Since the start of this year"),
    ("all", "All time"),
]
DEFAULT_WINDOW = "ytd"
WINDOW_LABELS = dict(WINDOWS)

# A switch only counts once the album version has more than one play after
# the single - one album play is as likely a shuffle or an autoplay as a
# change of habit.
MIN_ALBUM_PLAYS = 2


def window_start(window: str, now: datetime | None = None) -> int:
    """Unix timestamp the window starts at (0 for "all"). "ytd" is local
    midnight on 1 January of the current year."""
    now = now or datetime.now().astimezone()
    if window == "all":
        return 0
    if window == "ytd":
        return int(now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    return int((now - timedelta(days=int(window))).timestamp())


def _qualifying_times(times: list[int], after: int, from_ts: int) -> list[int] | None:
    """The plays in `times` (oldest first) later than `after`, if there are
    at least MIN_ALBUM_PLAYS of them and the first is inside the window."""
    later = [t for t in times if t > after]
    if len(later) >= MIN_ALBUM_PLAYS and later[0] >= from_ts:
        return later
    return None


def find_switched_albums(
    sp: Spotify, window: str, cancel_check: CancelCheck | None = None
) -> list[dict]:
    """Full albums whose tracks you've switched to from the single/EP
    version, based on the album each scrobble was played from (playing a
    Spotify single scrobbles the single's release as the album).

    Songs are grouped across releases by playlist_swap's
    normalized_signature, the same "same song" rule the swap itself uses. A
    song has switched to album B when it has plays under some single/EP,
    and B (a full album) has at least MIN_ALBUM_PLAYS plays after the
    song's first single play, the first of them inside the window. Release
    types come from Spotify's album_type, resolved through the shared
    resolve_cache "album" cache.

    Syncs scrobble history first. Returns, most recent switch first,
    [{"id", "name", "artists", "image_url", "switched_at",
      "songs": [{"name", "artists", "switched_at", "album_plays"}, ...]}, ...]
    where switched_at is the first album-version play after the single.
    """
    scrobble_history.sync(cancel_check)
    check_cancelled(cancel_check)
    from_ts = window_start(window)

    # signature -> release key -> {"artist", "album", "times": [...]}
    songs: dict[tuple[str, str], dict[str, dict]] = {}
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    for s in scrobble_history.scrobbles_between(0, 2**62):
        if not s["album"]:
            continue
        sig = normalized_signature(s["artist"], s["name"])
        key = resolve_cache.album_key(s["artist"], s["album"])
        release = songs.setdefault(sig, {}).setdefault(
            key, {"artist": s["artist"], "album": s["album"], "times": []}
        )
        release["times"].append(s["timestamp"])
        labels[sig] = (s["artist"], s["name"])
    check_cancelled(cancel_check)

    # Before any Spotify lookups, narrow to songs that could possibly
    # qualify whatever the release types turn out to be: some release with
    # enough plays, inside the window, after the song's first play under a
    # different release. Only those songs' releases get resolved.
    candidates: dict[tuple[str, str], dict[str, dict]] = {}
    for sig, releases in songs.items():
        if len(releases) < 2:
            continue
        for key, release in releases.items():
            earliest_other = min(r["times"][0] for k, r in releases.items() if k != key)
            if _qualifying_times(release["times"], earliest_other, from_ts):
                candidates[sig] = releases
                break

    to_resolve = {
        key: {"artist": r["artist"], "name": r["album"]}
        for releases in candidates.values()
        for key, r in releases.items()
    }
    logger.info(
        "detect switched albums: %d song(s) played from more than one release; checking %d release(s) on Spotify",
        len(candidates),
        len(to_resolve),
    )

    cache = resolve_cache.load("album")
    try:
        looked_up = 0
        for key, item in to_resolve.items():
            if key in cache:
                continue
            check_cancelled(cancel_check)
            try:
                cache[key] = most_played._search_album(sp, item) or {"found": False}
            except Exception:
                logger.warning("detect switched albums: Spotify lookup failed for %r", key, exc_info=True)
                continue
            looked_up += 1
            if looked_up % 25 == 0:
                logger.info("detect switched albums: looked up %d new release(s)", looked_up)
    finally:
        resolve_cache.save("album", cache)

    albums: dict[str, dict] = {}
    for sig, releases in candidates.items():
        typed = {key: cache.get(key) or {} for key in releases}
        single_starts = [
            releases[key]["times"][0]
            for key, info in typed.items()
            if info.get("found") and info.get("album_type") == "single"
        ]
        if not single_starts:
            continue
        first_single = min(single_starts)
        for key, info in typed.items():
            if not (info.get("found") and info.get("album_type") == "album"):
                continue
            later = _qualifying_times(releases[key]["times"], first_single, from_ts)
            if not later:
                continue
            album = albums.setdefault(
                info["spotify_id"],
                {
                    "id": info["spotify_id"],
                    "name": info["name"],
                    "artists": info["artists"],
                    "image_url": info.get("image_url"),
                    "switched_at": later[0],
                    "songs": [],
                },
            )
            album["switched_at"] = max(album["switched_at"], later[0])
            artist, name = labels[sig]
            album["songs"].append(
                {"name": name, "artists": artist, "switched_at": later[0], "album_plays": len(later)}
            )

    result = sorted(albums.values(), key=lambda a: a["switched_at"], reverse=True)
    for album in result:
        album["songs"].sort(key=lambda s: s["switched_at"], reverse=True)
    logger.info(
        "detect switched albums: found %d album(s) you've switched to from the single (%s)",
        len(result),
        WINDOW_LABELS.get(window, window).lower(),
    )
    return result
