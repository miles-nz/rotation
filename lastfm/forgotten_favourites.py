from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.lastfm_client as lastfm_client_module
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter

logger = logging.getLogger(__name__)

# Same page size as scrobble_history's getRecentTracks walk - a candidate
# scan pages through all-time top tracks until play counts drop below the
# minimum, so bigger pages mean fewer round-trips for a low minimum.
_TOP_PAGE_LIMIT = 200

# Every candidate needs a Spotify search the first time it's seen (cached
# permanently after that), so a very loose form (e.g. 1 play, 1 month) can't
# kick off thousands of searches in one go. Candidates come in most-played
# order, so hitting this keeps the most-played ones and drops the tail.
MAX_RESULTS = 500

LIKED_SONGS_LABEL = "Liked Songs"


def months_ago(now: datetime, months: int) -> datetime:
    """now minus `months` calendar months, clamping the day to the target
    month's length (e.g. 31 March minus 1 month is 28/29 February)."""
    month_index = now.year * 12 + (now.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def _find_candidates(
    min_plays: int, cutoff_ts: int, history: dict, cancel_check: CancelCheck | None
) -> dict:
    """Pages through all-time top tracks (most-played first) until play
    counts drop below min_plays, keeping any whose last scrobble is before
    cutoff_ts. The top list is sorted by play count, so the first track
    under the minimum means every track after it is too - no need to walk
    the rest of the library.

    A track with no entry in the history index (Last.fm's top list and
    scrobble log occasionally spell the same track differently) can't be
    judged either way, so it's counted in no_history_count rather than
    guessed at."""
    candidates: list[dict] = []
    no_history_count = 0
    page = 1
    while True:
        logger.info("forgotten favourites: fetching top tracks page %d", page)
        top = lastfm_client_module.call_with_retry(
            lambda: lastfm_client_module.get_top_tracks(period="overall", limit=_TOP_PAGE_LIMIT, page=page),
            description=f"forgotten favourites: top tracks page {page}",
            cancel_check=cancel_check,
        )
        if not top:
            return {"candidates": candidates, "no_history_count": no_history_count, "truncated": False}
        for item in top:
            if item["playcount"] < min_plays:
                return {"candidates": candidates, "no_history_count": no_history_count, "truncated": False}
            history_entry = scrobble_history.lookup(history, item["artist"], item["name"])
            last = history_entry.get("last")
            if last is None:
                no_history_count += 1
                continue
            if last < cutoff_ts:
                candidates.append(
                    {**item, "first_scrobbled": history_entry.get("first"), "last_scrobbled": last}
                )
                if len(candidates) >= MAX_RESULTS:
                    return {"candidates": candidates, "no_history_count": no_history_count, "truncated": True}
        logger.info(
            "forgotten favourites: %d candidate(s) so far, down to %d plays",
            len(candidates),
            top[-1]["playcount"],
        )
        check_cancelled(cancel_check)
        page += 1


def _signature(artists: str, name: str) -> tuple[str, str]:
    # Same "different version of the same song" key as Playlist Filter's
    # similar-version check, so a Spotify search hit that's a different
    # release (remaster, compilation, single) of what's in a playlist still
    # counts as already there.
    return playlist_filter._primary_artist(artists), playlist_filter._normalize_title(name)


def _library_locations(sp: Spotify, cancel_check: CancelCheck | None):
    """Returns (by_uri, by_signature), each mapping to the set of playlist
    names (plus LIKED_SONGS_LABEL) a track is in. Covers playlists the user
    owns or collaborates on - followed playlists belong to someone else, so
    a track being in one of those doesn't mean it's in the user's library.
    Liked Songs is only cached as bare uris, so it matches on uri alone."""
    user_id = sp.current_user()["id"]
    playlist_ids = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for playlist in results["items"]:
            if (playlist.get("owner") or {}).get("id") == user_id or playlist.get("collaborative"):
                playlist_ids.append(playlist["id"])
        results = sp.next(results) if results.get("next") else None

    logger.info("checking %d playlist(s) and Liked Songs for tracks already in your library", len(playlist_ids))
    by_uri: dict[str, set[str]] = {}
    by_signature: dict[tuple[str, str], set[str]] = {}
    for playlist in playlist_cache_module.get_playlists(sp, playlist_ids, cancel_check).values():
        for track in playlist["tracks"]:
            by_uri.setdefault(track["uri"], set()).add(playlist["name"])
            by_signature.setdefault(_signature(track["artists"], track["name"]), set()).add(playlist["name"])

    for uri in playlist_cache_module.get_liked_songs(sp, cancel_check):
        by_uri.setdefault(uri, set()).add(LIKED_SONGS_LABEL)

    return by_uri, by_signature


def find_forgotten_favourites(
    sp: Spotify, min_plays: int, months: int, cancel_check: CancelCheck | None = None
) -> dict:
    """Tracks with at least min_plays all-time scrobbles whose most recent
    scrobble is more than `months` months before now. Returns:

    - results: Spotify-matched tracks, most-played first, each with
      "in_library" - the sorted names of the user's playlists (and/or
      "Liked Songs") it's already in, empty if none.
    - unmatched: candidates with no Spotify match (Last.fm name/artist,
      play count and dates only).
    - no_history_count: tracks with enough plays but no last-played date
      in the history index, so they couldn't be checked.
    - truncated: True if candidates were capped at MAX_RESULTS.
    """
    cutoff = months_ago(datetime.now(timezone.utc), months)
    cutoff_ts = int(cutoff.timestamp())

    # Needs every track's last-played date, so this always syncs - a
    # one-time full history walk the first time (shared with Most Played's
    # First/Last Played filters), a cheap incremental top-up after that.
    history = scrobble_history.sync(cancel_check)

    scan = _find_candidates(min_plays, cutoff_ts, history, cancel_check)
    candidates = scan["candidates"]

    cache = resolve_cache.load("track")
    try:
        resolved_pairs = most_played._resolve_generic(
            sp,
            candidates,
            cache,
            key_fn=lambda item: resolve_cache.track_key(item["artist"], item["name"]),
            search_fn=most_played._search_track,
            cancel_check=cancel_check,
        )
    finally:
        resolve_cache.save("track", cache)

    results = []
    unmatched = []
    for item, resolved in resolved_pairs:
        dates = {"first_scrobbled": item["first_scrobbled"], "last_scrobbled": item["last_scrobbled"]}
        if resolved.get("found"):
            results.append(
                {
                    "uri": resolved["uri"],
                    "name": resolved["name"],
                    "artists": resolved["artists"],
                    "album": resolved["album"],
                    "release_year": resolved["release_year"],
                    "explicit": resolved["explicit"],
                    "image_url": resolved.get("image_url"),
                    "playcount": item["playcount"],
                    **dates,
                }
            )
        else:
            unmatched.append(
                {"name": item["name"], "artists": item["artist"], "playcount": item["playcount"], **dates}
            )

    if results:
        by_uri, by_signature = _library_locations(sp, cancel_check)
        for entry in results:
            locations = by_uri.get(entry["uri"], set()) | by_signature.get(
                _signature(entry["artists"], entry["name"]), set()
            )
            entry["in_library"] = sorted(locations, key=lambda n: (n != LIKED_SONGS_LABEL, n.lower()))

    return {
        "results": results,
        "unmatched": unmatched,
        "no_history_count": scan["no_history_count"],
        "truncated": scan["truncated"],
        "cutoff": cutoff_ts,
    }
