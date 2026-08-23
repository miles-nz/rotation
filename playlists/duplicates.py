from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module

REMOVE_BATCH_SIZE = 100

logger = logging.getLogger(__name__)


def find_duplicates_from_tracks(playlists: list[dict]) -> list[dict]:
    """playlists: [{"id": ..., "name": ..., "tracks": [...]}, ...] with
    already-fetched tracks (each needing "uri", "name", "artists",
    "added_at").

    Returns duplicate groups, sorted by artist/name:
    [{"uri", "name", "artists",
      "occurrences": [{"playlist_id", "playlist_name", "added_at"}, ...]}]
    """
    occurrences_by_uri: dict[str, list[dict]] = {}
    track_meta: dict[str, tuple[str, str]] = {}

    for playlist in playlists:
        for track in playlist["tracks"]:
            occurrences_by_uri.setdefault(track["uri"], []).append(
                {
                    "playlist_id": playlist["id"],
                    "playlist_name": playlist["name"],
                    "added_at": track["added_at"],
                }
            )
            track_meta[track["uri"]] = (track["name"], track["artists"])

    duplicates = []
    for uri, occurrences in occurrences_by_uri.items():
        if len(occurrences) > 1:
            name, artists = track_meta[uri]
            duplicates.append(
                {
                    "uri": uri,
                    "name": name,
                    "artists": artists,
                    "occurrences": sorted(occurrences, key=lambda o: o["added_at"] or ""),
                }
            )

    duplicates.sort(key=lambda d: (d["artists"].lower(), d["name"].lower()))
    logger.info(
        "found %d duplicate track(s) across %d playlists", len(duplicates), len(playlists)
    )
    return duplicates


def find_duplicates(
    sp: Spotify, playlist_ids: list[str], cancel_check: CancelCheck | None = None
) -> list[dict]:
    playlists = playlist_cache_module.get_playlists(sp, playlist_ids, cancel_check)
    fetched = [
        {"id": pid, "name": playlists[pid]["name"], "tracks": playlists[pid]["tracks"]}
        for pid in playlist_ids
    ]
    return find_duplicates_from_tracks(fetched)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def remove_from_playlists(sp: Spotify, removals: list[dict]) -> None:
    """removals: [{"playlist_id": ..., "uri": ...}, ...]"""
    by_playlist: dict[str, list[str]] = {}
    for removal in removals:
        by_playlist.setdefault(removal["playlist_id"], []).append(removal["uri"])

    for playlist_id, uris in by_playlist.items():
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
            # The removal itself already succeeded; don't let a failure in
            # this housekeeping call make it look like the whole operation
            # failed. Worst case the cache stays stale until it self-heals
            # on the next read (snapshot_id won't match).
            logger.warning(
                "playlist %s: track(s) removed, but refreshing the cache afterwards "
                "failed - it'll self-correct on the next read",
                playlist_id,
                exc_info=True,
            )
