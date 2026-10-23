from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter_module
import playlists.removal_history as removal_history_module

REMOVE_BATCH_SIZE = 100
# How far apart two releases' lengths can be and still count as the same
# song - reissues and remasters often drift by a second or so.
RELEASE_DURATION_TOLERANCE_MS = 2000

logger = logging.getLogger(__name__)


def _release_clusters(tracks: list[dict]) -> list[list[dict]]:
    """Groups tracks (distinct uris) that look like the same song on
    different releases: same artists, same title once version suffixes are
    stripped, and lengths within RELEASE_DURATION_TOLERANCE_MS of each
    other. Local files and tracks with no length are left out - they only
    ever match by exact uri. Returns only clusters of 2+ tracks."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for track in tracks:
        if track.get("is_local") or track.get("duration_ms") is None:
            continue
        key = (
            track["artists"].strip().lower(),
            playlist_filter_module._normalize_title(track["name"]),
        )
        by_key.setdefault(key, []).append(track)

    clusters = []
    for candidates in by_key.values():
        # Anchoring each cluster on its shortest track (rather than chaining
        # neighbours) keeps every pair in it within the tolerance.
        cluster: list[dict] = []
        for track in sorted(candidates, key=lambda t: t["duration_ms"]):
            gap = track["duration_ms"] - cluster[0]["duration_ms"] if cluster else 0
            if gap > RELEASE_DURATION_TOLERANCE_MS:
                clusters.append(cluster)
                cluster = []
            cluster.append(track)
        clusters.append(cluster)
    return [c for c in clusters if len(c) > 1]


def find_duplicates_from_tracks(playlists: list[dict]) -> list[dict]:
    """playlists: [{"id": ..., "name": ..., "tracks": [...]}, ...] with
    already-fetched tracks (each needing "uri", "name", "artists",
    "added_at", and ideally "album", "duration_ms", "is_local").

    Returns duplicate groups, sorted by artist/name. Every group lists one
    or more releases (distinct uris) and where each one appears:
    [{"kind": "exact" | "release", "uri", "name", "artists",
      "releases": [{"uri", "name", "album", "duration_ms",
                    "occurrences": [{"playlist_id", "playlist_name",
                                     "added_at"}, ...]}, ...]}]

    "exact" groups are one uri in more than one place. "release" groups are
    the same song on two or more different releases (see _release_clusters),
    with every occurrence of each of them - a song that matches both ways
    only appears as a "release" group."""
    occurrences_by_uri: dict[str, list[dict]] = {}
    track_by_uri: dict[str, dict] = {}

    for playlist in playlists:
        for track in playlist["tracks"]:
            occurrences_by_uri.setdefault(track["uri"], []).append(
                {
                    "playlist_id": playlist["id"],
                    "playlist_name": playlist["name"],
                    "added_at": track["added_at"],
                }
            )
            track_by_uri[track["uri"]] = track

    def release(uri: str) -> dict:
        track = track_by_uri[uri]
        return {
            "uri": uri,
            "name": track["name"],
            "album": track.get("album") or "",
            "duration_ms": track.get("duration_ms"),
            "occurrences": sorted(occurrences_by_uri[uri], key=lambda o: o["added_at"] or ""),
        }

    def group(kind: str, releases: list[dict]) -> dict:
        # The shortest title is usually the plain one ("Dreams" rather than
        # "Dreams - Single Version"), so it makes the best heading.
        first = track_by_uri[min(releases, key=lambda r: len(r["name"]))["uri"]]
        return {
            "kind": kind,
            "uri": first["uri"],
            "name": first["name"],
            "artists": first["artists"],
            "releases": releases,
        }

    duplicates = []
    grouped_uris: set[str] = set()
    for cluster in _release_clusters(list(track_by_uri.values())):
        releases = sorted(
            (release(t["uri"]) for t in cluster),
            key=lambda r: r["occurrences"][0]["added_at"] or "",
        )
        duplicates.append(group("release", releases))
        grouped_uris.update(t["uri"] for t in cluster)

    for uri, occurrences in occurrences_by_uri.items():
        if len(occurrences) > 1 and uri not in grouped_uris:
            duplicates.append(group("exact", [release(uri)]))

    duplicates.sort(key=lambda d: (d["artists"].lower(), d["name"].lower()))
    logger.info(
        "found %d duplicate song(s) across %d playlists (%d on different releases)",
        len(duplicates),
        len(playlists),
        sum(1 for d in duplicates if d["kind"] == "release"),
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


def remove_from_playlists(
    sp: Spotify, removals: list[dict], source: str = "Duplicate Finder"
) -> str | None:
    """removals: [{"playlist_id": ..., "uri": ...}, ...]

    Logs the removal to removal_history under source first; returns that
    history entry's id (None if there was nothing to remove)."""
    by_playlist: dict[str, list[str]] = {}
    for removal in removals:
        by_playlist.setdefault(removal["playlist_id"], []).append(removal["uri"])

    playlists = {pid: playlist_cache_module.get_playlist(sp, pid) for pid in by_playlist}
    history_id = removal_history_module.record(
        source,
        [
            removal_history_module.snapshot_removed(
                pid, playlists[pid]["name"], playlists[pid]["tracks"], uris
            )
            for pid, uris in by_playlist.items()
        ],
    )

    for playlist_id, uris in by_playlist.items():
        current_tracks = playlists[playlist_id]["tracks"]

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

    return history_id
