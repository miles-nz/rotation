from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter

logger = logging.getLogger(__name__)


def _signature(track: dict) -> tuple[str, str, str]:
    """Identifies "the same song" regardless of uri (e.g. a remaster or a
    different regional release): artist + album + title, case/whitespace
    insensitive."""
    return (
        track["artists"].strip().lower(),
        track["album"].strip().lower(),
        track["name"].strip().lower(),
    )


def find_missing_from_tracks(
    source_playlists: list[dict], target_playlists: list[dict]
) -> list[dict]:
    """source_playlists / target_playlists: [{"id": ..., "name": ...,
    "tracks": [...]}, ...] with already-fetched tracks.

    Returns tracks present in the source playlists but absent from every
    target playlist - by uri, or by matching artist/album/title (so a
    different version of a track already in the target isn't treated as
    missing) - deduped by uri, in reverse source playlist order:
    [{"uri", "name", "artists", "album"}]
    """
    seen: dict[str, dict] = {}
    for playlist in source_playlists:
        for track in playlist["tracks"]:
            seen.setdefault(track["uri"], track)

    target_uris: set[str] = set()
    target_signatures: set[tuple[str, str, str]] = set()
    for playlist in target_playlists:
        for track in playlist["tracks"]:
            target_uris.add(track["uri"])
            target_signatures.add(_signature(track))

    missing = [
        t
        for uri, t in reversed(seen.items())
        if uri not in target_uris and _signature(t) not in target_signatures
    ]
    logger.info(
        "found %d track(s) in source playlist(s) missing from target playlist(s)",
        len(missing),
    )
    return missing


def find_missing(
    sp: Spotify,
    source_ids: list[str],
    target_ids: list[str],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """Returns {"missing": [...], "targets": [{"id", "name"}, ...]} - targets
    carries resolved names so callers can label playlists in the "add"
    step without a further lookup."""
    all_ids = list(dict.fromkeys(list(source_ids) + list(target_ids)))
    playlists = playlist_cache_module.get_playlists(sp, all_ids, cancel_check)

    def build(playlist_ids):
        return [
            {"id": pid, "name": playlists[pid]["name"], "tracks": playlists[pid]["tracks"]}
            for pid in playlist_ids
        ]

    sources = build(source_ids)
    targets = build(target_ids)
    missing = find_missing_from_tracks(sources, targets)
    return {"missing": missing, "targets": [{"id": t["id"], "name": t["name"]} for t in targets]}


def add_to_playlists(sp: Spotify, additions: list[dict], add_tracks=None) -> dict[str, int]:
    """additions: [{"playlist_id", "uri"}, ...]. add_tracks(playlist_id, uris)
    defaults to a live Spotify fetch of existing tracks; callers with a
    cache of existing playlist state (e.g. cascade) can pass their own.

    Adds each uri to its playlist (skipping ones already present). Returns
    {playlist_id: added_count}, omitting playlists with nothing added.
    """
    add_tracks = add_tracks or (
        lambda playlist_id, uris: playlist_filter.add_tracks_to_playlist(sp, playlist_id, uris)
    )

    by_playlist: dict[str, list[str]] = {}
    for addition in additions:
        by_playlist.setdefault(addition["playlist_id"], []).append(addition["uri"])

    added_counts: dict[str, int] = {}
    for playlist_id, uris in by_playlist.items():
        added, _skipped = add_tracks(playlist_id, uris)
        if added:
            added_counts[playlist_id] = added
    return added_counts
