from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import playlists.playlist_cache as playlist_cache_module

BATCH_SIZE = 50

logger = logging.getLogger(__name__)


def get_liked_track_uris(
    sp: Spotify, cancel_check: CancelCheck | None = None
) -> set[str]:
    return playlist_cache_module.get_liked_songs(sp, cancel_check)


def compute_diff(
    playlist_uris: set[str], liked_uris: set[str]
) -> tuple[set[str], set[str]]:
    # Local files can't be saved to Liked Songs at all - Spotify doesn't
    # support it via this API (or even from the desktop client) - so they're
    # never a candidate to add, regardless of which playlists they're in.
    playlist_uris = {uri for uri in playlist_uris if not uri.startswith("spotify:local:")}
    to_add = playlist_uris - liked_uris
    to_remove = liked_uris - playlist_uris
    logger.info("diff computed: %d to add, %d to remove", len(to_add), len(to_remove))
    return to_add, to_remove


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def apply_diff(sp: Spotify, to_add: set[str], to_remove: set[str]) -> None:
    add_batches = list(_chunks(list(to_add), BATCH_SIZE))
    for i, batch in enumerate(add_batches, start=1):
        logger.info("adding batch %d/%d (%d tracks)", i, len(add_batches), len(batch))
        sp.current_user_saved_tracks_add(tracks=batch)

    remove_batches = list(_chunks(list(to_remove), BATCH_SIZE))
    for i, batch in enumerate(remove_batches, start=1):
        logger.info(
            "removing batch %d/%d (%d tracks)", i, len(remove_batches), len(batch)
        )
        sp.current_user_saved_tracks_delete(tracks=batch)

    playlist_cache_module.refresh_liked_songs_after_mutation(to_add, to_remove)


def get_target_diff(
    sp: Spotify,
    playlist_id_1: str,
    playlist_id_2: str,
    cancel_check: CancelCheck | None = None,
):
    playlists = playlist_cache_module.get_playlists(sp, [playlist_id_1, playlist_id_2], cancel_check)
    check_cancelled(cancel_check)
    playlist_uris = {t["uri"] for t in playlists[playlist_id_1]["tracks"]}
    playlist_uris |= {t["uri"] for t in playlists[playlist_id_2]["tracks"]}

    logger.info("fetching liked songs")
    liked_uris = get_liked_track_uris(sp, cancel_check)

    return compute_diff(playlist_uris, liked_uris)
