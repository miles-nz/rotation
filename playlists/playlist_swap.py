from __future__ import annotations

import logging
import re

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import playlists.playlist_cache as playlist_cache_module

logger = logging.getLogger(__name__)

# Strips a trailing single-release title suffix so "Song Name - Single",
# "Song Name (Single)" and "Song Name (Single Version)" all normalize to
# "Song Name" for matching against the album cut. Extend only as real
# false-negatives show up - the review-and-confirm step before anything is
# swapped is the actual safety net for an imperfect match, not this regex.
_SINGLE_SUFFIX_RE = re.compile(r"\s*[-(]\s*single(\s+version)?\)?\s*$", re.IGNORECASE)


def _normalized_signature(artists: str, name: str) -> tuple[str, str]:
    """Identifies "the same song" for single-vs-album matching: artist +
    title, case/whitespace insensitive, with a trailing single-release
    suffix stripped from the title (see _SINGLE_SUFFIX_RE)."""
    name = _SINGLE_SUFFIX_RE.sub("", name).strip()
    return (artists.strip().lower(), name.strip().lower())


def get_album_tracks(sp: Spotify, album_id: str) -> dict:
    """Fetches an album's own metadata and full tracklist (in album order)
    straight from Spotify's catalog - not from any playlist. Returns
    {"id", "uri", "name", "artists", "image_url",
     "tracks": [{"uri", "name", "artists"}, ...]}.
    """
    album = sp.album(album_id)
    artists = album.get("artists") or []
    images = album.get("images") or []

    tracks: list[dict] = []
    page = album.get("tracks")
    while page:
        for t in page.get("items") or []:
            track_artists = t.get("artists") or []
            tracks.append(
                {
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": ", ".join(a["name"] for a in track_artists),
                }
            )
        page = sp.next(page) if page.get("next") else None

    return {
        "id": album["id"],
        "uri": album["uri"],
        "name": album["name"],
        "artists": ", ".join(a["name"] for a in artists),
        "image_url": images[0]["url"] if images else None,
        "tracks": tracks,
    }


def find_swaps(
    sp: Spotify,
    album_id: str,
    playlist_ids: list[str],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """The scan job target. Finds, in each of playlist_ids, any track that
    is a single-version match (_normalized_signature) for a track on the
    given album, where the playlist track's uri isn't already that album
    track's uri (nothing to swap if it's already the album version). Local
    files are never candidates - the Web API can't remove/reinsert them at
    a specific position any more than it can add them.

    A track's position is just its index in playlist_cache's already-
    ordered track list - there's no separate position field on a track
    dict.

    Returns {"album": {"id","name","artists","image_url"},
             "swaps": [{"id", "playlist_id", "playlist_name", "position",
                        "single": {"uri","name","artists"},
                        "album_track": {"uri","name","artists"}}, ...]}
    "id" (f"{playlist_id}:{position}") is a stable per-row key used to
    round-trip which swaps the user confirmed on the review screen - the
    apply step re-reads this same result from server memory rather than
    trusting track data sent back from the client.
    """
    album = get_album_tracks(sp, album_id)
    album_by_signature: dict[tuple[str, str], dict] = {}
    for track in album["tracks"]:
        album_by_signature.setdefault(
            _normalized_signature(track["artists"], track["name"]), track
        )

    playlists = playlist_cache_module.get_playlists(sp, playlist_ids, cancel_check)

    swaps: list[dict] = []
    for playlist_id in playlist_ids:
        playlist = playlists[playlist_id]
        for position, track in enumerate(playlist["tracks"]):
            if track.get("is_local"):
                continue
            album_track = album_by_signature.get(
                _normalized_signature(track["artists"], track["name"])
            )
            if album_track is None or album_track["uri"] == track["uri"]:
                continue
            swaps.append(
                {
                    "id": f"{playlist_id}:{position}",
                    "playlist_id": playlist_id,
                    "playlist_name": playlist["name"],
                    "position": position,
                    "single": {
                        "uri": track["uri"],
                        "name": track["name"],
                        "artists": track["artists"],
                    },
                    "album_track": album_track,
                }
            )
        check_cancelled(cancel_check)

    logger.info(
        "album swap scan: found %d swap(s) across %d playlist(s) for '%s'",
        len(swaps),
        len(playlist_ids),
        album["name"],
    )
    return {
        "album": {
            "id": album["id"],
            "name": album["name"],
            "artists": album["artists"],
            "image_url": album["image_url"],
        },
        "swaps": swaps,
    }


def apply_swaps(sp: Spotify, swaps: list[dict]) -> dict[str, dict]:
    """swaps: the confirmed subset of find_swaps()'s "swaps" list. Per
    playlist, removes each swap's single by its exact recorded position
    (playlist_remove_specific_occurrences_of_items - not
    remove_all_occurrences, since only that one occurrence should go) and
    immediately re-inserts the album track at that same position, one swap
    at a time. Removing and reinserting at the same index is a net-zero
    shift for every other track in the playlist, so swaps within one
    playlist can be issued in any order without recomputing positions - but
    each removal MUST be immediately followed by its own insertion before
    moving to the next swap; batching all removals first would let
    positions drift between the two passes.

    Returns {playlist_id: {"playlist_name", "swapped": n}}.
    """
    by_playlist: dict[str, list[dict]] = {}
    for swap in swaps:
        by_playlist.setdefault(swap["playlist_id"], []).append(swap)

    album_uris = list({s["album_track"]["uri"] for s in swaps})
    album_track_details = playlist_cache_module.track_details_for_uris(sp, album_uris)

    summary: dict[str, dict] = {}
    for playlist_id, playlist_swaps in by_playlist.items():
        current_tracks = playlist_cache_module.get_playlist(sp, playlist_id)["tracks"]

        for swap in playlist_swaps:
            sp.playlist_remove_specific_occurrences_of_items(
                playlist_id,
                [{"uri": swap["single"]["uri"], "positions": [swap["position"]]}],
            )
            sp.playlist_add_items(
                playlist_id, [swap["album_track"]["uri"]], position=swap["position"]
            )
            logger.info(
                "playlist %s: swapped '%s' for '%s' at position %d",
                playlist_id,
                swap["single"]["name"],
                swap["album_track"]["name"],
                swap["position"],
            )

        # The removal/insertion pairs above already succeeded; nothing from
        # here on should turn into an exception that makes it look like the
        # swap failed. Worst case the cache stays stale until it
        # self-corrects on the next read (snapshot_id won't match).
        try:
            new_tracks = list(current_tracks)
            for swap in playlist_swaps:
                details = album_track_details.get(swap["album_track"]["uri"])
                if details:
                    new_tracks[swap["position"]] = details
            playlist_cache_module.refresh_after_mutation(sp, playlist_id, new_tracks)
        except Exception:
            logger.warning(
                "playlist %s: swap(s) applied, but refreshing the cache afterwards "
                "failed - it'll self-correct on the next read",
                playlist_id,
                exc_info=True,
            )

        summary[playlist_id] = {
            "playlist_name": playlist_swaps[0]["playlist_name"],
            "swapped": len(playlist_swaps),
        }

    return summary
