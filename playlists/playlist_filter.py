from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module

ADD_BATCH_SIZE = 100

# Strips trailing version qualifiers like "(Single Version)", "[Live]", or
# "- Remastered 2011" so title comparisons can tell "same song, different
# version" from "different song".
_BRACKETED_SUFFIX_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_DASH_SUFFIX_RE = re.compile(r"\s+-\s+[^-]+$")

logger = logging.getLogger(__name__)

FIELD_OPERATORS = {
    "added_date": ["within_last", "older_than"],
    "release_year": ["is", "before", "after", "between"],
    "popularity": ["at_least", "at_most"],
    "explicit": ["is"],
    "artist": ["contains"],
    "track_name": ["contains"],
}

_UNIT_DAYS = {"days": 1, "weeks": 7, "months": 30}


def _cutoff_datetime(value: str, unit: str) -> datetime:
    days = int(value) * _UNIT_DAYS[unit]
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_added_at(added_at: str | None) -> datetime | None:
    if not added_at:
        return None
    try:
        return datetime.fromisoformat(added_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def matches_criterion(
    track: dict, field: str, operator: str, value: str, value2: str | None
) -> bool:
    if field == "added_date":
        added_dt = _parse_added_at(track.get("added_at"))
        if added_dt is None:
            return False
        cutoff = _cutoff_datetime(value, value2)
        if operator == "within_last":
            return added_dt >= cutoff
        if operator == "older_than":
            return added_dt < cutoff
        return False
    elif field == "release_year":
        year = track["release_year"]
        if year is None:
            return False
        target = int(value)
        if operator == "is":
            return year == target
        if operator == "before":
            return year < target
        if operator == "after":
            return year > target
        if operator == "between":
            target2 = int(value2)
            lo, hi = sorted((target, target2))
            return lo <= year <= hi
    elif field == "popularity":
        popularity = track["popularity"]
        if popularity is None:
            return False
        target = int(value)
        if operator == "at_least":
            return popularity >= target
        if operator == "at_most":
            return popularity <= target
    elif field == "explicit":
        return track["explicit"] == (value == "yes")
    elif field == "artist":
        return value.lower() in track["artists"].lower()
    elif field == "track_name":
        return value.lower() in track["name"].lower()
    return False


def matches_all_criteria(track: dict, criteria: list[dict]) -> bool:
    return all(
        matches_criterion(track, c["field"], c["operator"], c["value"], c.get("value2"))
        for c in criteria
    )


def find_matches_from_tracks(
    source_playlists: list[dict],
    criteria: list[dict],
) -> list[dict]:
    """source_playlists: [{"id": ..., "name": ..., "tracks": [...]}, ...]
    with already-fetched tracks. criteria: [{"field", "operator", "value",
    "value2"}, ...] - a track must match every criterion.

    Returns matching tracks, deduped by uri across source playlists and
    ordered by source playlist: all matches from the first playlist (in
    that playlist's order), then any new matches from the second playlist,
    and so on: [{"uri", "name", "artists"}]
    """
    seen: dict[str, dict] = {}

    for playlist in source_playlists:
        for track in playlist["tracks"]:
            if track["uri"] in seen:
                continue
            if matches_all_criteria(track, criteria):
                seen[track["uri"]] = {
                    "uri": track["uri"],
                    "name": track["name"],
                    "artists": track["artists"],
                }

    matches = list(seen.values())
    logger.info(
        "found %d matching track(s) across %d source playlist(s)",
        len(matches),
        len(source_playlists),
    )
    return matches


def find_matches(
    sp: Spotify,
    source_playlist_ids: list[str],
    criteria: list[dict],
    cancel_check: CancelCheck | None = None,
) -> list[dict]:
    playlists = playlist_cache_module.get_playlists(sp, source_playlist_ids, cancel_check)
    fetched = [
        {"id": pid, "name": playlists[pid]["name"], "tracks": playlists[pid]["tracks"]}
        for pid in source_playlist_ids
    ]
    return find_matches_from_tracks(fetched, criteria)


def get_playlist_track_uris(
    sp: Spotify, playlist_id: str, cancel_check: CancelCheck | None = None
) -> set[str]:
    tracks = playlist_cache_module.get_playlist(sp, playlist_id, cancel_check)["tracks"]
    return {t["uri"] for t in tracks}


def exclude_existing(matches: list[dict], existing_uris: set[str]) -> list[dict]:
    return [m for m in matches if m["uri"] not in existing_uris]


def get_playlist_track_details(
    sp: Spotify, playlist_id: str, cancel_check: CancelCheck | None = None
) -> list[dict]:
    """Like get_playlist_track_uris but also returns name/artists, for the
    "check for similar versions" comparison."""
    tracks = playlist_cache_module.get_playlist(sp, playlist_id, cancel_check)["tracks"]
    return [{"uri": t["uri"], "name": t["name"], "artists": t["artists"]} for t in tracks]


def _normalize_title(title: str) -> str:
    cleaned = title.strip().lower()
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _BRACKETED_SUFFIX_RE.sub("", cleaned).strip()
    return _DASH_SUFFIX_RE.sub("", cleaned).strip()


def _primary_artist(artists: str) -> str:
    return artists.split(",")[0].strip().lower()


def find_similar_versions(
    matches: list[dict], destination_tracks: list[dict]
) -> dict[str, list[dict]]:
    """For each match, finds tracks already in the destination that share the
    same primary artist and normalized title (version qualifiers like
    "(Single Version)"/"- Remastered 2011" stripped) but a different uri -
    i.e. likely a different version of the same song. Only matches with at
    least one hit are included: {match_uri: [{"uri", "name", "artists"}, ...]}
    """
    by_key: dict[tuple[str, str], list[dict]] = {}
    for track in destination_tracks:
        key = (_primary_artist(track["artists"]), _normalize_title(track["name"]))
        by_key.setdefault(key, []).append(track)

    result: dict[str, list[dict]] = {}
    for match in matches:
        key = (_primary_artist(match["artists"]), _normalize_title(match["name"]))
        candidates = [t for t in by_key.get(key, []) if t["uri"] != match["uri"]]
        if candidates:
            result[match["uri"]] = candidates
    return result


def create_playlist(sp: Spotify, name: str) -> str:
    user_id = sp.current_user()["id"]
    playlist = sp.user_playlist_create(user_id, name, public=False)
    logger.info("created playlist '%s' (%s)", name, playlist["id"])
    return playlist["id"]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def add_new_tracks_to_playlist(
    sp: Spotify,
    playlist_id: str,
    uris: list[str],
    existing_uris,
    track_details: list[dict] | None = None,
) -> tuple[int, int]:
    """Adds uris not already in existing_uris. Returns (added, skipped).

    track_details, when the caller already has full track dicts for the
    uris being added (e.g. cascade, which keeps them in its own playlist
    cache), avoids an extra Spotify lookup when refreshing the on-disk
    cache afterwards."""
    to_add = [uri for uri in uris if uri not in existing_uris]
    skipped = len(uris) - len(to_add)

    current_tracks = (
        playlist_cache_module.get_playlist(sp, playlist_id)["tracks"] if to_add else None
    )

    batches = list(_chunks(to_add, ADD_BATCH_SIZE))
    for i, batch in enumerate(batches, start=1):
        logger.info(
            "adding %d/%d track(s) to playlist %s (batch %d/%d)",
            len(batch),
            len(to_add),
            playlist_id,
            i,
            len(batches),
        )
        sp.playlist_add_items(playlist_id, batch)

    if to_add:
        # The add itself already succeeded above; nothing from here on
        # should turn into an exception that makes it look like it failed.
        # Worst case some housekeeping step doesn't complete and the cache
        # stays stale until it self-heals on the next read (snapshot_id
        # won't match) or a later invalidate call.
        try:
            details_by_uri = (
                {t["uri"]: t for t in track_details}
                if track_details is not None
                else playlist_cache_module.track_details_for_uris(sp, to_add)
            )
            unresolved = set(to_add) - details_by_uri.keys()
            if unresolved:
                # Can't build a complete post-add track list, so don't cache
                # an undercount with false confidence - invalidate instead
                # and let the next read do a full, honest re-fetch.
                logger.warning(
                    "playlist %s: couldn't resolve metadata for %d added track(s), "
                    "invalidating cache instead of saving an incomplete one: %s",
                    playlist_id,
                    len(unresolved),
                    ", ".join(sorted(unresolved)),
                )
                playlist_cache_module.invalidate(playlist_id)
            else:
                current_uris = {t["uri"] for t in current_tracks}
                new_tracks = [
                    details_by_uri[uri] for uri in to_add if uri not in current_uris
                ]
                playlist_cache_module.refresh_after_mutation(
                    sp, playlist_id, current_tracks + new_tracks
                )
        except Exception:
            logger.warning(
                "playlist %s: %d track(s) added, but refreshing the cache afterwards "
                "failed - it'll self-correct on the next read",
                playlist_id,
                len(to_add),
                exc_info=True,
            )

    return len(to_add), skipped


def add_tracks_to_playlist(sp: Spotify, playlist_id: str, uris: list[str]) -> tuple[int, int]:
    """Adds uris not already present in the playlist. Returns (added, skipped)."""
    existing = get_playlist_track_uris(sp, playlist_id)
    return add_new_tracks_to_playlist(sp, playlist_id, uris, existing)
