from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from spotipy import Spotify

from core.cancellation import CancelCheck
from playlists.playlist_cache import PlaylistCache
import playlists.duplicates as duplicates_module
import playlists.playlist_filter as playlist_filter_module
import playlists.playlist_cleanup as playlist_cleanup_module
import playlists.playlist_diff as playlist_diff_module
import core.sync as sync_module

logger = logging.getLogger(__name__)

STEP_LABELS = {
    "duplicates": "Duplicate Finder",
    "playlist_filter": "Playlist Filter",
    "playlist_cleanup": "Playlist Cleanup",
    "playlist_diff": "Playlist Diff",
    "sync": "Liked Songs Sync",
}


def needed_playlist_ids(step: dict) -> list[str]:
    step_type = step["type"]
    if step_type == "duplicates":
        return list(step["playlist_ids"])
    if step_type == "playlist_filter":
        ids = list(step["playlist_ids"])
        if step["destination_mode"] == "existing":
            ids.append(step["destination_playlist_id"])
        return ids
    if step_type == "playlist_cleanup":
        return [step["playlist_id"]]
    if step_type == "playlist_diff":
        return list(step["source_ids"]) + list(step["target_ids"])
    if step_type == "sync":
        return [step["playlist_id_1"], step["playlist_id_2"]]
    raise ValueError(f"unknown step type {step_type!r}")


def needs_liked_songs(step: dict) -> bool:
    return step["type"] == "sync"


def _lookup_tracks(cache: PlaylistCache, source_playlist_ids, uris) -> list[dict]:
    """Finds the already-fetched track dict for each uri by searching the
    given source playlists - used when tracks move from a source into a
    destination playlist, so the destination's cached copy has full details
    (not just uri/name/artists) for any later cascade step."""
    remaining = set(uris)
    found: dict[str, dict] = {}
    for playlist_id in source_playlist_ids:
        for track in cache.playlist(playlist_id)["tracks"]:
            if track["uri"] in remaining:
                found[track["uri"]] = track
                remaining.discard(track["uri"])
    return [found[uri] for uri in uris if uri in found]


def scan_step(cache: PlaylistCache, step: dict) -> Any:
    step_type = step["type"]

    if step_type == "duplicates":
        playlists = cache.playlists(step["playlist_ids"])
        return duplicates_module.find_duplicates_from_tracks(playlists)

    if step_type == "playlist_filter":
        playlists = cache.playlists(step["playlist_ids"])
        matches = playlist_filter_module.find_matches_from_tracks(
            playlists, step["criteria"]
        )
        already_in_destination = 0
        similar_versions: dict = {}
        if step["destination_mode"] == "existing":
            destination_tracks = cache.playlist(step["destination_playlist_id"])["tracks"]
            existing_uris = {t["uri"] for t in destination_tracks}
            before_count = len(matches)
            matches = playlist_filter_module.exclude_existing(matches, existing_uris)
            already_in_destination = before_count - len(matches)
            similar_versions = playlist_filter_module.find_similar_versions(
                matches, destination_tracks
            )
        return {
            "matches": matches,
            "already_in_destination": already_in_destination,
            "similar_versions": similar_versions,
            "destination_mode": step["destination_mode"],
            "destination_playlist_id": step.get("destination_playlist_id"),
            "destination_playlist_name": step.get("destination_playlist_name"),
            "destination_name": step.get("destination_name"),
        }

    if step_type == "playlist_cleanup":
        playlist = cache.playlist(step["playlist_id"])
        removals = playlist_cleanup_module.find_removals_from_tracks(
            playlist["name"],
            playlist["tracks"],
            step["field"],
            step["operator"],
            step["value"],
            step.get("value2"),
        )
        return {
            "playlist_id": step["playlist_id"],
            "playlist_name": playlist["name"],
            "removals": removals,
        }

    if step_type == "playlist_diff":
        sources = cache.playlists(step["source_ids"])
        targets = cache.playlists(step["target_ids"])
        missing = playlist_diff_module.find_missing_from_tracks(sources, targets)
        return {
            "missing": missing,
            "targets": [{"id": p["id"], "name": p["name"]} for p in targets],
        }

    if step_type == "sync":
        uris_1 = {t["uri"] for t in cache.playlist(step["playlist_id_1"])["tracks"]}
        uris_2 = {t["uri"] for t in cache.playlist(step["playlist_id_2"])["tracks"]}
        to_add, to_remove = sync_module.compute_diff(uris_1 | uris_2, cache.liked_song_uris())
        return {"to_add": to_add, "to_remove": to_remove}

    raise ValueError(f"unknown step type {step_type!r}")


def apply_step(sp: Spotify, cache: PlaylistCache, step: dict, result: Any, form) -> dict:
    step_type = step["type"]
    label = STEP_LABELS[step_type]

    if step_type == "duplicates":
        removals = []
        for item in form.getlist("remove"):
            playlist_id, _, uri = item.partition("::")
            if playlist_id and uri:
                removals.append({"playlist_id": playlist_id, "uri": uri})
        duplicates_module.remove_from_playlists(sp, removals)
        for removal in removals:
            cache.remove_tracks(removal["playlist_id"], [removal["uri"]])
        return {"type": step_type, "label": label, "removed": len(removals)}

    if step_type == "playlist_filter":
        match_uris = {m["uri"] for m in result["matches"]}
        selected_uris = [uri for uri in form.getlist("track") if uri in match_uris]

        if result["destination_mode"] == "new":
            playlist_id = playlist_filter_module.create_playlist(sp, result["destination_name"])
            playlist_name = result["destination_name"]
            cache.register_new_playlist(playlist_id, playlist_name)
        else:
            playlist_id = result["destination_playlist_id"]
            playlist_name = result["destination_playlist_name"]

        existing_uris = {t["uri"] for t in cache.playlist(playlist_id)["tracks"]}
        track_details = _lookup_tracks(cache, step["playlist_ids"], selected_uris)
        added, skipped, local_skipped = playlist_filter_module.add_new_tracks_to_playlist(
            sp, playlist_id, selected_uris, existing_uris, track_details=track_details
        )
        # track_details may include local tracks that just got excluded from
        # the actual add above (the API can't add those) - only the ones
        # that really landed in the playlist belong in the in-memory cache.
        added_track_details = [t for t in track_details if not t.get("is_local")]
        cache.add_tracks(playlist_id, added_track_details)
        return {
            "type": step_type,
            "label": label,
            "added": added,
            "skipped": skipped,
            "local_skipped": local_skipped,
            "playlist_name": playlist_name,
        }

    if step_type == "playlist_cleanup":
        removal_uris = {r["uri"] for r in result["removals"]}
        selected_uris = [uri for uri in form.getlist("track") if uri in removal_uris]
        playlist_cleanup_module.remove_tracks(sp, result["playlist_id"], selected_uris)
        cache.remove_tracks(result["playlist_id"], selected_uris)
        return {
            "type": step_type,
            "label": label,
            "removed": len(selected_uris),
            "playlist_name": result["playlist_name"],
        }

    if step_type == "playlist_diff":
        missing_uris = {t["uri"] for t in result["missing"]}
        target_ids = {t["id"] for t in result["targets"]}
        additions = []
        for item in form.getlist("add"):
            uri, _, playlist_id = item.partition("::")
            if uri in missing_uris and playlist_id in target_ids:
                additions.append({"playlist_id": playlist_id, "uri": uri})

        def _add_tracks(playlist_id, uris):
            existing_uris = {t["uri"] for t in cache.playlist(playlist_id)["tracks"]}
            track_details = _lookup_tracks(cache, step["source_ids"], uris)
            added, skipped, local_skipped = playlist_filter_module.add_new_tracks_to_playlist(
                sp, playlist_id, uris, existing_uris, track_details=track_details
            )
            # As above: track_details may include local tracks excluded from
            # the actual add - only cache the ones that really landed.
            added_track_details = [t for t in track_details if not t.get("is_local")]
            cache.add_tracks(playlist_id, added_track_details)
            return added, skipped, local_skipped

        added_counts = playlist_diff_module.add_to_playlists(sp, additions, add_tracks=_add_tracks)

        targets_by_id = {t["id"]: t["name"] for t in result["targets"]}
        added_summary = [
            {
                "name": targets_by_id[pid],
                "added": counts["added"],
                "local_skipped": counts["local_skipped"],
            }
            for pid, counts in added_counts.items()
        ]
        return {"type": step_type, "label": label, "added_summary": added_summary}

    if step_type == "sync":
        to_add, to_remove = result["to_add"], result["to_remove"]
        sync_module.apply_diff(sp, to_add, to_remove)
        cache.update_liked_songs(to_add, to_remove)
        return {"type": step_type, "label": label, "added": len(to_add), "removed": len(to_remove)}

    raise ValueError(f"unknown step type {step_type!r}")


class CascadeRun:
    """Tracks one cascade end-to-end: the ordered steps, the shared
    playlist cache, and how far through the chain we've gotten."""

    def __init__(self, steps: list[dict]):
        self.steps = steps
        self.cache = PlaylistCache()
        self.index = 0
        self.results: list[Any] = [None] * len(steps)
        self.summaries: list[dict] = []

    @property
    def current_step(self) -> dict | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def is_done(self) -> bool:
        return self.index >= len(self.steps)

    def all_playlist_ids(self) -> set[str]:
        ids: set[str] = set()
        for step in self.steps:
            ids.update(needed_playlist_ids(step))
        return ids

    def prefetch(self, sp: Spotify, cancel_check: CancelCheck | None = None) -> None:
        # Playlists and Liked Songs are independent reads, so run them
        # concurrently rather than paying for Liked Songs' fetch time on
        # top of the playlists' (which are themselves already fanned out
        # across their own thread pool in ensure_playlists).
        tasks = [lambda: self.cache.ensure_playlists(sp, self.all_playlist_ids(), cancel_check)]
        if any(needs_liked_songs(step) for step in self.steps):
            tasks.append(lambda: self.cache.ensure_liked_songs(sp, cancel_check))

        if len(tasks) == 1:
            tasks[0]()
            return
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(task) for task in tasks]
            for future in futures:
                future.result()

    def scan_current(self) -> Any:
        result = scan_step(self.cache, self.current_step)
        self.results[self.index] = result
        return result

    def apply_current(self, sp: Spotify, form) -> dict:
        summary = apply_step(sp, self.cache, self.current_step, self.results[self.index], form)
        self.summaries.append(summary)
        self.index += 1
        return summary
