from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module

logger = logging.getLogger(__name__)

ADD_BATCH_SIZE = 100


def find_prependable_from_tracks(
    source_tracks: list[dict], destination_tracks: list[dict]
) -> dict:
    """source_tracks: playlist A's tracks, in playlist order. destination_tracks:
    playlist B's current tracks.

    Returns {"to_add": [...source tracks not already in destination, still in
    source order...], "duplicates": [...source tracks skipped because
    they're already in destination...], "local_skipped": [...source tracks
    skipped because they're local files, which the Web API can't add to a
    playlist at all...]} - duplicates/local_skipped are detected by uri.
    """
    existing_uris = {t["uri"] for t in destination_tracks}
    to_add: list[dict] = []
    duplicates: list[dict] = []
    local_skipped: list[dict] = []
    seen: set[str] = set()
    for track in source_tracks:
        if track["uri"] in seen:
            continue
        seen.add(track["uri"])
        if track["uri"] in existing_uris:
            duplicates.append(track)
        elif track.get("is_local"):
            local_skipped.append(track)
        else:
            to_add.append(track)
    logger.info(
        "found %d track(s) to prepend, %d already present (skipped), "
        "%d local file(s) skipped (can't be added via the API)",
        len(to_add),
        len(duplicates),
        len(local_skipped),
    )
    return {"to_add": to_add, "duplicates": duplicates, "local_skipped": local_skipped}


def find_prependable(
    sp: Spotify,
    source_id: str,
    destination_id: str,
    cancel_check: CancelCheck | None = None,
) -> dict:
    """Returns find_prependable_from_tracks's result plus display names and
    the destination id, so callers don't need a further lookup."""
    source = playlist_cache_module.get_playlist(sp, source_id, cancel_check)
    destination = playlist_cache_module.get_playlist(sp, destination_id, cancel_check)
    result = find_prependable_from_tracks(source["tracks"], destination["tracks"])
    return {
        **result,
        "source_name": source["name"],
        "destination_id": destination_id,
        "destination_name": destination["name"],
    }


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def prepend_tracks_to_playlist(
    sp: Spotify,
    playlist_id: str,
    uris: list[str],
    track_details: list[dict],
) -> int:
    """Inserts uris at the top of playlist_id, in the given order, pushing
    everything already there down. Callers are expected to have already
    filtered out duplicates (e.g. via find_prependable). track_details must
    cover every uri, in the same order, with full track dicts (as scanned by
    playlist_cache) so the on-disk cache can be updated without an extra
    Spotify lookup.

    Each 100-uri batch is inserted right after the previous one (position =
    batch_index * ADD_BATCH_SIZE), so multi-batch adds come out in the same
    order as uris instead of reversed.
    """
    if not uris:
        return 0

    current_tracks = playlist_cache_module.get_playlist(sp, playlist_id)["tracks"]

    batches = list(_chunks(uris, ADD_BATCH_SIZE))
    for i, batch in enumerate(batches):
        logger.info(
            "prepending %d/%d track(s) to playlist %s (batch %d/%d)",
            len(batch),
            len(uris),
            playlist_id,
            i + 1,
            len(batches),
        )
        sp.playlist_add_items(playlist_id, batch, position=i * ADD_BATCH_SIZE)

    try:
        # The add itself already succeeded above; nothing from here on
        # should turn into an exception that makes it look like it failed.
        details_by_uri = {t["uri"]: t for t in track_details}
        new_tracks = [details_by_uri[uri] for uri in uris]
        playlist_cache_module.refresh_after_mutation(sp, playlist_id, new_tracks + current_tracks)
    except Exception:
        logger.warning(
            "playlist %s: %d track(s) prepended, but refreshing the cache afterwards "
            "failed - it'll self-correct on the next read",
            playlist_id,
            len(uris),
            exc_info=True,
        )

    return len(uris)
