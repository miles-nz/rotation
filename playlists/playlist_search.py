from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module
from playlists.playlist_filter import FIELD_OPERATORS, matches_all_criteria

logger = logging.getLogger(__name__)

# Re-exported so app.py only has one place to import the field/operator
# spec from, regardless of which tool's picker it's building.
__all__ = ["FIELD_OPERATORS", "search"]


def _signature(track: dict) -> tuple[str, str]:
    """Identifies "the same song" regardless of uri (e.g. a remaster or a
    different regional release) - exact artist + title match, case/
    whitespace insensitive."""
    return (track["artists"].strip().lower(), track["name"].strip().lower())


def search(
    sp: Spotify,
    include_playlist_ids: list[str],
    criteria: list[dict],
    exclude_playlist_ids: list[str] | None = None,
    cancel_check: CancelCheck | None = None,
) -> list[dict]:
    """Search: tracks in include_playlist_ids matching every criterion,
    that aren't already present in any of exclude_playlist_ids - either by
    uri, or by an exact artist + title match (so a different version of a
    track already in an excluded playlist isn't treated as new). Doesn't
    add, remove, or modify anything itself.

    Returns tracks deduped by uri, sorted by artist/name:
    [{"uri", "name", "artists", "album", "release_year", "added_at",
    "popularity", "explicit"}, ...]
    """
    all_ids = list(dict.fromkeys(list(include_playlist_ids) + list(exclude_playlist_ids or [])))
    playlists = playlist_cache_module.get_playlists(sp, all_ids, cancel_check)

    exclude_uris: set[str] = set()
    exclude_signatures: set[tuple[str, str]] = set()
    for playlist_id in exclude_playlist_ids or []:
        for track in playlists[playlist_id]["tracks"]:
            exclude_uris.add(track["uri"])
            exclude_signatures.add(_signature(track))

    seen: dict[str, dict] = {}
    for playlist_id in include_playlist_ids:
        for track in playlists[playlist_id]["tracks"]:
            if track["uri"] in seen:
                continue
            if track["uri"] in exclude_uris or _signature(track) in exclude_signatures:
                continue
            if matches_all_criteria(track, criteria):
                seen[track["uri"]] = track

    results = sorted(seen.values(), key=lambda t: (t["artists"].lower(), t["name"].lower()))
    logger.info(
        "playlist search: %d result(s) across %d include playlist(s), %d exclude playlist(s)",
        len(results),
        len(include_playlist_ids),
        len(exclude_playlist_ids or []),
    )
    return results
