from __future__ import annotations

import logging

from spotipy import Spotify

import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter

REMOVE_BATCH_SIZE = 100

logger = logging.getLogger(__name__)

FIELD_OPERATORS = playlist_filter.FIELD_OPERATORS

matches_criterion = playlist_filter.matches_criterion


def find_removals_from_tracks(
    playlist_name: str,
    tracks: list[dict],
    field: str,
    operator: str,
    value: str,
    value2: str | None,
) -> list[dict]:
    """tracks: already-fetched tracks for the playlist.

    Returns tracks in the playlist that do NOT match the keep-criterion,
    sorted by artist/name: [{"uri", "name", "artists", "added_at"}, ...]
    """
    removals = [
        {
            "uri": t["uri"],
            "name": t["name"],
            "artists": t["artists"],
            "added_at": t["added_at"],
        }
        for t in tracks
        if not matches_criterion(t, field, operator, value, value2)
    ]
    removals.sort(key=lambda t: (t["artists"].lower(), t["name"].lower()))
    logger.info(
        "playlist '%s': %d of %d track(s) will be removed",
        playlist_name,
        len(removals),
        len(tracks),
    )
    return removals


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def remove_tracks(sp: Spotify, playlist_id: str, uris: list[str]) -> None:
    if not uris:
        return

    current_tracks = playlist_cache_module.get_playlist(sp, playlist_id)["tracks"]

    batches = list(_chunks(uris, REMOVE_BATCH_SIZE))
    for i, batch in enumerate(batches, start=1):
        logger.info(
            "removing %d/%d track(s) from playlist %s (batch %d/%d)",
            len(batch),
            len(uris),
            playlist_id,
            i,
            len(batches),
        )
        sp.playlist_remove_all_occurrences_of_items(playlist_id, batch)

    removed_uris = set(uris)
    updated_tracks = [t for t in current_tracks if t["uri"] not in removed_uris]
    try:
        playlist_cache_module.refresh_after_mutation(sp, playlist_id, updated_tracks)
    except Exception:
        # The removal itself already succeeded; don't let a failure in this
        # housekeeping call make it look like the whole operation failed.
        # Worst case the cache stays stale until it self-heals on the next
        # read (snapshot_id won't match).
        logger.warning(
            "playlist %s: track(s) removed, but refreshing the cache afterwards "
            "failed - it'll self-correct on the next read",
            playlist_id,
            exc_info=True,
        )
