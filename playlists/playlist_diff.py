from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.diff_snoozes as diff_snoozes
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

    Snoozed tracks (see diff_snoozes) are left out, matched the same way,
    so every caller - standalone, Cascade and the scheduled auto-scan -
    stops suggesting them.
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

    snoozes = diff_snoozes.active()
    snoozed_signatures = {_signature(s) for s in snoozes.values()}
    suggested = [
        t for t in missing if t["uri"] not in snoozes and _signature(t) not in snoozed_signatures
    ]
    logger.info(
        "found %d track(s) in source playlist(s) missing from target playlist(s), %d snoozed",
        len(suggested),
        len(missing) - len(suggested),
    )
    return suggested


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


def snoozes_from_form(result: dict, form) -> list[dict]:
    """The tracks from a find_missing result ticked "don't suggest" in the
    submitted form. A track that's also being added isn't snoozed - it
    won't be missing next time anyway."""
    snooze_uris = set(form.getlist("snooze"))
    adding_uris = {item.partition("::")[0] for item in form.getlist("add")}
    return [
        t for t in result["missing"] if t["uri"] in snooze_uris and t["uri"] not in adding_uris
    ]


def add_to_playlists(sp: Spotify, additions: list[dict], add_tracks=None) -> dict[str, dict]:
    """additions: [{"playlist_id", "uri"}, ...]. add_tracks(playlist_id, uris)
    defaults to a live Spotify fetch of existing tracks; callers with a
    cache of existing playlist state (e.g. cascade) can pass their own.

    Adds each uri to its playlist (skipping ones already present, and any
    local file - the Web API can't add those to a playlist at all). Returns
    {playlist_id: {"added": n, "local_skipped": n}}, omitting playlists with
    nothing added and nothing local-skipped.
    """
    add_tracks = add_tracks or (
        lambda playlist_id, uris: playlist_filter.add_tracks_to_playlist(sp, playlist_id, uris)
    )

    by_playlist: dict[str, list[str]] = {}
    for addition in additions:
        by_playlist.setdefault(addition["playlist_id"], []).append(addition["uri"])

    added_counts: dict[str, dict] = {}
    for playlist_id, uris in by_playlist.items():
        added, _skipped, local_skipped = add_tracks(playlist_id, uris)
        if added or local_skipped:
            added_counts[playlist_id] = {"added": added, "local_skipped": local_skipped}
    return added_counts
