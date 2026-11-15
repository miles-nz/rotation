from __future__ import annotations

import logging
from datetime import datetime, timezone

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.forgotten_favourites as forgotten_favourites
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history

logger = logging.getLogger(__name__)

MAX_RESULTS = forgotten_favourites.MAX_RESULTS

# How many candidates are resolved on Spotify per round. Candidates are
# resolved most-played first and the scan stops as soon as enough old songs
# turn up, so a small round keeps a "top 10" from resolving hundreds of
# songs it doesn't need (every resolve is cached permanently either way).
_RESOLVE_CHUNK = 50


def year_bounds(year: int) -> tuple[int, int]:
    """[start, end) unix timestamps of the calendar year, in UTC - the same
    convention as the first/last scrobbled date filters."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def find_old_songs(
    sp: Spotify,
    year: int,
    count: int,
    min_plays: int | None = None,
    discovered_only: bool = False,
    cancel_check: CancelCheck | None = None,
) -> dict:
    """The songs played most in `year` that were released before it, most
    played first. Returns:

    - results: up to `count` Spotify-matched songs, each with "playcount"
      (plays in `year`), "release_year" and "in_library" (the sorted names
      of the user's playlists and/or "Liked Songs" it's already in).
    - unmatched: songs with no Spotify match (or no release date), whose
      release year couldn't be checked - Last.fm name/artist and plays only.
    - truncated: True if the scan hit its cap before finding `count` songs.
    - scanned_count: how many of the year's songs were looked at.

    With discovered_only, songs with any scrobble before `year` are left
    out, so only ones first played in `year` remain.

    Release year is the earliest among the user's saved copies of the song
    (playlists only - Liked Songs aren't cached with release years) when
    there is one, since the Spotify search can land on a remaster or
    compilation that would make an old song look new; otherwise the
    Spotify search match's.
    """
    start_ts, end_ts = year_bounds(year)

    # Always syncs so plays right up to now are counted - a one-time full
    # history walk the first time (shared with the other Last.fm tools),
    # then a cheap incremental top-up.
    history = scrobble_history.sync(cancel_check)

    plays = scrobble_history.plays_between(start_ts, end_ts)
    if min_plays:
        plays = [p for p in plays if p["plays"] >= min_plays]
    if discovered_only:
        # "Discovered" = first-ever scrobble falls in the year. Every song
        # in `plays` has a scrobble in the year, so a missing first date
        # can't happen in practice; treat it as not discovered to be safe.
        plays = [
            p
            for p in plays
            if (scrobble_history.lookup(history, p["artist"], p["name"]).get("first") or 0) >= start_ts
        ]

    scan_cap = count * most_played.MAX_SCAN_MULTIPLIER
    candidates = plays[:scan_cap]

    results: list[dict] = []
    unmatched: list[dict] = []
    scanned = 0

    if candidates:
        by_uri, by_signature, saved_years = forgotten_favourites._library_locations(sp, cancel_check)

        cache = resolve_cache.load("track")
        try:
            for i in range(0, len(candidates), _RESOLVE_CHUNK):
                chunk = candidates[i : i + _RESOLVE_CHUNK]
                resolved_pairs = most_played._resolve_generic(
                    sp,
                    chunk,
                    cache,
                    key_fn=lambda item: resolve_cache.track_key(item["artist"], item["name"]),
                    search_fn=most_played._search_track,
                    cancel_check=cancel_check,
                )
                for item, resolved in resolved_pairs:
                    scanned += 1
                    unmatched_entry = {"name": item["name"], "artists": item["artist"], "playcount": item["plays"]}
                    if not resolved.get("found"):
                        unmatched.append(unmatched_entry)
                        continue

                    signature = forgotten_favourites._signature(resolved["artists"], resolved["name"])
                    release_year = saved_years.get(signature, resolved["release_year"])
                    if release_year is None:
                        unmatched.append(unmatched_entry)
                        continue
                    if release_year >= year:
                        continue

                    locations = by_uri.get(resolved["uri"], set()) | by_signature.get(signature, set())
                    results.append(
                        {
                            "uri": resolved["uri"],
                            "name": resolved["name"],
                            "artists": resolved["artists"],
                            "album": resolved["album"],
                            "release_year": release_year,
                            "explicit": resolved["explicit"],
                            "image_url": resolved.get("image_url"),
                            "playcount": item["plays"],
                            "in_library": sorted(
                                locations, key=lambda n: (n != forgotten_favourites.LIKED_SONGS_LABEL, n.lower())
                            ),
                        }
                    )
                    if len(results) >= count:
                        break
                if len(results) >= count:
                    break
                check_cancelled(cancel_check)
        finally:
            resolve_cache.save("track", cache)

    logger.info("old songs: %d found after looking at %d of %d song(s) played in %d", len(results), scanned, len(plays), year)
    return {
        "results": results,
        "unmatched": unmatched,
        "truncated": len(results) < count and len(plays) > scan_cap,
        "scanned_count": scanned,
    }
