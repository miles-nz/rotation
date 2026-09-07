from __future__ import annotations

import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.lastfm_client as lastfm_client_module
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history

logger = logging.getLogger(__name__)

# Per-entity-type field/operator specs - kept independent of
# playlists/playlist_filter.py's FIELD_OPERATORS since these three shapes
# (playcount, genres, per-type name fields) don't overlap with the
# Spotify-playlist-track shape that dict is built around.
FIELD_OPERATORS = {
    "track": {
        "release_year": ["is", "before", "after", "between"],
        "first_scrobbled": ["before", "after"],
        "last_scrobbled": ["before", "after"],
        "popularity": ["at_least", "at_most"],
        "playcount": ["at_least", "at_most"],
        "explicit": ["is"],
        "genre": ["contains"],
        "artist": ["contains"],
        "track_name": ["contains"],
    },
    "album": {
        "release_year": ["is", "before", "after", "between"],
        "popularity": ["at_least", "at_most"],
        "playcount": ["at_least", "at_most"],
        "genre": ["contains"],
        "artist": ["contains"],
        "album_name": ["contains"],
    },
    "artist": {
        "popularity": ["at_least", "at_most"],
        "followers": ["at_least", "at_most"],
        "playcount": ["at_least", "at_most"],
        "genre": ["contains"],
        "artist_name": ["contains"],
    },
}

# A pool overlaps the latency of several Spotify search/lookup calls instead
# of paying for them one at a time - same rationale and size as
# playlist_cache.MAX_CONCURRENT_FETCHES.
MAX_CONCURRENT_RESOLVES = 5

# Criteria (release year, genre, ...) apply to Last.fm's top list, which is
# ranked by playcount, not filtered - so "top 200 tracks before 1990" can't
# just take the top 200 and filter afterward (that might leave 1 result out
# of 200). Instead, keep paging further into scrobble history until `count`
# matches are found, up to this many multiples of `count` items scanned, so
# a very restrictive filter gives up instead of scanning someone's entire
# multi-year history looking for matches that don't exist.
MAX_SCAN_MULTIPLIER = 20


def _parse_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(release_date[:4])
    except ValueError:
        return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _normalize_name(s: str) -> str:
    # Strips accents (so "Beyonce" ~ "Beyoncé") and treats all punctuation
    # as word-separators rather than literal characters, so formatting-only
    # differences between how Last.fm and Spotify write the same title
    # don't look like a mismatch - e.g. Last.fm's "Track (X Remix)" vs
    # Spotify's "Track - X Remix" both become "track x remix", and
    # "Can't" vs "Cant" both become "cant". A loose sanity check, not a
    # precise match - just enough to tell "basically the same name" from
    # "unrelated".
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", stripped.lower()))


def _names_plausibly_match(query: str, found: str) -> bool:
    """Sanity check that a Spotify search's top hit is actually related to
    what was searched for. Spotify's search doesn't strictly enforce field
    filters (artist:"..."/track:"...") - when the exact combination doesn't
    exist, it can fall back to a popular but unrelated result instead of
    returning nothing (seen in practice: a track whose Last.fm artist
    credit is a short/single-character name matched an unrelated hit song
    by title alone, since Spotify apparently couldn't use that short a
    token to narrow the artist match). Rejecting anything that doesn't
    share a name in some form turns that silent wrong-match into a normal
    "not found", which already has a correct fallback (shown as
    unresolved, Last.fm tags used for genre) - the cost is that a
    legitimately-matching track whose Spotify/Last.fm names differ more
    than this loose check allows for (e.g. a Spotify-side stage-name
    change) will also show as unresolved instead of being wrongly guessed.
    """
    query = _normalize_name(query)
    found = _normalize_name(found)
    if not query or not found:
        return False
    return query in found or found in query


def matches_criterion(item: dict, field: str, operator: str, value: str, value2: str | None) -> bool:
    if field == "release_year":
        year = item.get("release_year")
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
        return False
    if field in ("first_scrobbled", "last_scrobbled"):
        ts = item.get(field)
        if ts is None:
            return False
        target = int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        if operator == "before":
            return ts < target
        if operator == "after":
            return ts > target
        return False
    if field in ("popularity", "playcount", "followers"):
        item_value = item.get(field)
        if item_value is None:
            return False
        target = int(value)
        if operator == "at_least":
            return item_value >= target
        if operator == "at_most":
            return item_value <= target
        return False
    if field == "explicit":
        return item.get("explicit") == (value == "yes")
    if field == "genre":
        return any(value.lower() in g.lower() for g in item.get("genres") or [])
    if field == "artist":
        return value.lower() in (item.get("artists") or "").lower()
    if field in ("artist_name", "track_name", "album_name"):
        return value.lower() in (item.get("name") or "").lower()
    return False


def matches_all_criteria(item: dict, criteria: list[dict]) -> bool:
    return all(
        matches_criterion(item, c["field"], c["operator"], c["value"], c.get("value2"))
        for c in criteria
    )


def _report_progress(current: int, total: int) -> None:
    logger.info(
        "resolving %d/%d on Spotify",
        current,
        total,
        extra={
            "progress": {
                "id": "resolve",
                "name": "Resolving on Spotify",
                "current": current,
                "total": total,
            }
        },
    )


def _resolve_generic(
    sp: Spotify,
    top_list: list[dict],
    cache: dict,
    key_fn,
    search_fn,
    cancel_check: CancelCheck | None,
    enrich_fn=None,
) -> list[tuple[dict, dict]]:
    """Resolves each item in top_list to Spotify metadata, using cache for
    anything already looked up (including confirmed no-matches). Mutates
    cache in place with any newly-resolved entries. enrich_fn, if given, is
    called once with {key: result} for this run's newly-found items, to
    fill in fields that need a second batched Spotify call (e.g. album
    popularity) - it mutates those dicts in place, which also updates cache
    since the same dict objects are stored there.

    Returns [(top_item, resolved), ...] in top_list's order. resolved is
    always a dict with a "found" bool - {"found": False} (plus whatever a
    caller later adds to it, e.g. a Last.fm tag fallback - the same dict
    object lives in cache, so mutating it here persists that too) when
    nothing matched on Spotify, never None.
    """
    total = len(top_list)
    resolved_map: dict[str, dict] = {}
    to_resolve: list[tuple[str, dict]] = []
    for item in top_list:
        key = key_fn(item)
        cached = cache.get(key)
        if cached is not None:
            resolved_map[key] = cached
        else:
            to_resolve.append((key, item))

    current = total - len(to_resolve)
    _report_progress(current, total)

    if to_resolve:
        newly_found: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_RESOLVES, len(to_resolve))) as pool:
            future_to_key = {pool.submit(search_fn, sp, item): key for key, item in to_resolve}
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    result = future.result()
                except Exception:
                    logger.warning("Spotify lookup failed for %r", key, exc_info=True)
                    result = None
                normalized = result if result else {"found": False}
                cache[key] = normalized
                resolved_map[key] = normalized
                if result:
                    newly_found[key] = normalized
                current += 1
                _report_progress(current, total)
                check_cancelled(cancel_check)

        if newly_found and enrich_fn:
            enrich_fn(sp, newly_found)

    return [(item, resolved_map[key_fn(item)]) for item in top_list]


def _fill_fallback_tags(resolved_pairs: list[tuple[dict, dict]], tags_fn, cancel_check: CancelCheck | None) -> None:
    """For items that didn't match on Spotify, fetches Last.fm's top tags as
    a rough genre stand-in (skipped if already cached from an earlier run).
    Mutates each unresolved dict in place - since it's the same object
    stored in the resolve cache, this persists the tags there too, so a
    repeat run doesn't re-fetch them."""
    for top_item, resolved in resolved_pairs:
        if resolved.get("found") or "tags" in resolved:
            continue
        try:
            resolved["tags"] = tags_fn(top_item)
        except Exception:
            logger.warning("Last.fm tag lookup failed for %r", top_item.get("name"), exc_info=True)
            resolved["tags"] = []
        check_cancelled(cancel_check)


def _search_track(sp: Spotify, item: dict) -> dict | None:
    resp = sp.search(q=f'track:"{item["name"]}" artist:"{item["artist"]}"', type="track", limit=1)
    items = (resp.get("tracks") or {}).get("items") or []
    if not items:
        return None
    t = items[0]
    artists = t.get("artists") or []
    primary_artist = artists[0]["name"] if artists else ""
    if not _names_plausibly_match(item["artist"], primary_artist) or not _names_plausibly_match(
        item["name"], t["name"]
    ):
        return None
    album = t.get("album") or {}
    images = album.get("images") or []
    return {
        "found": True,
        "uri": t["uri"],
        "name": t["name"],
        "artists": ", ".join(a["name"] for a in artists),
        "artist_id": artists[0]["id"] if artists else None,
        "album": album.get("name") or "",
        "release_year": _parse_year(album.get("release_date")),
        "popularity": t.get("popularity"),
        "explicit": bool(t.get("explicit")),
        "image_url": images[0]["url"] if images else None,
    }


def _search_album(sp: Spotify, item: dict) -> dict | None:
    resp = sp.search(q=f'album:"{item["name"]}" artist:"{item["artist"]}"', type="album", limit=1)
    items = (resp.get("albums") or {}).get("items") or []
    if not items:
        return None
    a = items[0]
    artists = a.get("artists") or []
    primary_artist = artists[0]["name"] if artists else ""
    if not _names_plausibly_match(item["artist"], primary_artist) or not _names_plausibly_match(
        item["name"], a["name"]
    ):
        return None
    images = a.get("images") or []
    return {
        "found": True,
        "spotify_id": a["id"],
        "uri": a["uri"],
        "name": a["name"],
        "artists": ", ".join(ar["name"] for ar in artists),
        "artist_id": artists[0]["id"] if artists else None,
        "release_year": _parse_year(a.get("release_date")),
        "popularity": None,  # album search results don't include this - filled in by _enrich_album_popularity
        "image_url": images[0]["url"] if images else None,
    }


def _enrich_album_popularity(sp: Spotify, newly_resolved: dict[str, dict]) -> None:
    ids = [v["spotify_id"] for v in newly_resolved.values()]
    popularity_by_id: dict[str, int | None] = {}
    for chunk in _chunks(ids, 20):
        for a in sp.albums(chunk)["albums"]:
            if a:
                popularity_by_id[a["id"]] = a.get("popularity")
    for v in newly_resolved.values():
        v["popularity"] = popularity_by_id.get(v["spotify_id"])


def _search_artist(sp: Spotify, item: dict) -> dict | None:
    resp = sp.search(q=f'artist:"{item["name"]}"', type="artist", limit=1)
    items = (resp.get("artists") or {}).get("items") or []
    if not items:
        return None
    a = items[0]
    if not _names_plausibly_match(item["name"], a["name"]):
        return None
    images = a.get("images") or []
    return {
        "found": True,
        "uri": a["uri"],
        "name": a["name"],
        "genres": a.get("genres") or [],
        "followers": (a.get("followers") or {}).get("total") or 0,
        "popularity": a.get("popularity"),
        "image_url": images[0]["url"] if images else None,
    }


def _fetch_genres(sp: Spotify, artist_ids: list[str], cancel_check: CancelCheck | None) -> dict[str, list[str]]:
    """Batch genre lookup for Track/Album mode, keyed by Spotify artist id
    (not name - the id already came from the resolved track/album's own
    artist, so this can't collide with a same-named different artist)."""
    cache = resolve_cache.load("artist_genres")
    ids = [aid for aid in dict.fromkeys(artist_ids) if aid]
    missing = [aid for aid in ids if aid not in cache]
    if missing:
        for chunk in _chunks(missing, 50):
            for a in sp.artists(chunk)["artists"]:
                if a:
                    cache[a["id"]] = {"genres": a.get("genres") or []}
            check_cancelled(cancel_check)
        resolve_cache.save("artist_genres", cache)
    return {aid: cache.get(aid, {}).get("genres", []) for aid in ids}


def _report_scan_progress(scanned: int, scan_cap: int, matches: int, target: int) -> None:
    logger.info(
        "scanned %d/%d, %d/%d matches found so far",
        scanned,
        scan_cap,
        matches,
        target,
        extra={
            "progress": {
                "id": "matches",
                "name": "Matches found",
                "current": min(matches, target),
                "total": target,
            }
        },
    )


def _paginated_scan(
    sp: Spotify,
    period: str,
    count: int,
    criteria: list[dict],
    cancel_check: CancelCheck | None,
    fetch_page,
    build_page_entries,
) -> dict:
    """Criteria apply to metadata Last.fm's top list doesn't have (release
    year, genre, ...), so "top `count` matching X" can't just take the top
    `count` and filter afterward - a restrictive filter might leave far
    fewer than `count`. Instead, pages further into scrobble history (via
    fetch_page(period=, limit=, page=)) until `count` matches are found or
    MAX_SCAN_MULTIPLIER * count items have been scanned without enough
    matches turning up.

    build_page_entries(sp, page_items, cancel_check) resolves one page and
    returns (entries, unresolved_count_this_page) - entity-specific (each
    entity type has its own resolve/cache/dict-shape), while this function
    owns the shared paging/stopping/progress logic.
    """
    matches: list[dict] = []
    unresolved_count = 0
    scanned = 0
    scan_cap = count * MAX_SCAN_MULTIPLIER
    page = 1
    while len(matches) < count and scanned < scan_cap:
        top = lastfm_client_module.call_with_retry(
            lambda: fetch_page(period=period, limit=count, page=page),
            description=f"most played scan: page {page}",
            cancel_check=cancel_check,
        )
        if not top:
            break
        entries, page_unresolved = build_page_entries(sp, top, cancel_check)
        unresolved_count += page_unresolved
        matches.extend(e for e in entries if matches_all_criteria(e, criteria))
        scanned += len(top)
        _report_scan_progress(scanned, scan_cap, len(matches), count)
        check_cancelled(cancel_check)
        page += 1

    return {
        "results": matches[:count],
        "unresolved_count": unresolved_count,
        "scanned_count": scanned,
        "truncated": len(matches) < count and scanned >= scan_cap,
    }


def _find_most_played_tracks(
    sp: Spotify, period: str, count: int, criteria: list[dict], cancel_check: CancelCheck | None
) -> dict:
    cache = resolve_cache.load("track")

    # The scrobble-history index (for the first_scrobbled/last_scrobbled
    # criteria) is only built/updated when actually needed - a scan
    # without either criterion never touches it, so nobody pays for a
    # history sync unless they're filtering on it. sync() is a one-time
    # ~708-page walk the first time it's ever called, then a cheap
    # incremental top-up on every call after (see lastfm/scrobble_history.py).
    needs_history = any(c["field"] in ("first_scrobbled", "last_scrobbled") for c in criteria)
    history = scrobble_history.sync(cancel_check) if needs_history else scrobble_history.load()

    def build_page_entries(sp: Spotify, top: list[dict], cancel_check: CancelCheck | None):
        resolved_pairs = _resolve_generic(
            sp,
            top,
            cache,
            key_fn=lambda item: resolve_cache.track_key(item["artist"], item["name"]),
            search_fn=_search_track,
            cancel_check=cancel_check,
        )
        _fill_fallback_tags(
            resolved_pairs,
            tags_fn=lambda item: lastfm_client_module.get_track_tags(item["artist"], item["name"]),
            cancel_check=cancel_check,
        )
        artist_ids = [r["artist_id"] for _, r in resolved_pairs if r.get("found") and r.get("artist_id")]
        genres_by_id = _fetch_genres(sp, artist_ids, cancel_check)

        entries = []
        unresolved = 0
        for top_item, resolved in resolved_pairs:
            history_entry = scrobble_history.lookup(history, top_item["artist"], top_item["name"])
            if resolved.get("found"):
                entries.append(
                    {
                        "uri": resolved["uri"],
                        "name": resolved["name"],
                        "artists": resolved["artists"],
                        "album": resolved["album"],
                        "release_year": resolved["release_year"],
                        "popularity": resolved["popularity"],
                        "explicit": resolved["explicit"],
                        "genres": genres_by_id.get(resolved.get("artist_id"), []),
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": resolved.get("image_url"),
                        "first_scrobbled": history_entry.get("first"),
                        "last_scrobbled": history_entry.get("last"),
                        "resolved": True,
                    }
                )
            else:
                unresolved += 1
                entries.append(
                    {
                        "uri": None,
                        "name": top_item["name"],
                        "artists": top_item["artist"],
                        "album": None,
                        "release_year": None,
                        "popularity": None,
                        "explicit": None,
                        "genres": resolved.get("tags", []),
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": None,
                        "first_scrobbled": history_entry.get("first"),
                        "last_scrobbled": history_entry.get("last"),
                        "resolved": False,
                    }
                )
        return entries, unresolved

    try:
        return _paginated_scan(
            sp,
            period,
            count,
            criteria,
            cancel_check,
            fetch_page=lastfm_client_module.get_top_tracks,
            build_page_entries=build_page_entries,
        )
    finally:
        resolve_cache.save("track", cache)


def _find_most_played_albums(
    sp: Spotify, period: str, count: int, criteria: list[dict], cancel_check: CancelCheck | None
) -> dict:
    cache = resolve_cache.load("album")

    def build_page_entries(sp: Spotify, top: list[dict], cancel_check: CancelCheck | None):
        resolved_pairs = _resolve_generic(
            sp,
            top,
            cache,
            key_fn=lambda item: resolve_cache.album_key(item["artist"], item["name"]),
            search_fn=_search_album,
            cancel_check=cancel_check,
            enrich_fn=_enrich_album_popularity,
        )
        _fill_fallback_tags(
            resolved_pairs,
            tags_fn=lambda item: lastfm_client_module.get_album_tags(item["artist"], item["name"]),
            cancel_check=cancel_check,
        )
        artist_ids = [r["artist_id"] for _, r in resolved_pairs if r.get("found") and r.get("artist_id")]
        genres_by_id = _fetch_genres(sp, artist_ids, cancel_check)

        entries = []
        unresolved = 0
        for top_item, resolved in resolved_pairs:
            if resolved.get("found"):
                entries.append(
                    {
                        "uri": resolved["uri"],
                        "name": resolved["name"],
                        "artists": resolved["artists"],
                        "release_year": resolved["release_year"],
                        "popularity": resolved["popularity"],
                        "genres": genres_by_id.get(resolved.get("artist_id"), []),
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": resolved.get("image_url"),
                        "resolved": True,
                    }
                )
            else:
                unresolved += 1
                entries.append(
                    {
                        "uri": None,
                        "name": top_item["name"],
                        "artists": top_item["artist"],
                        "release_year": None,
                        "popularity": None,
                        "genres": resolved.get("tags", []),
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": None,
                        "resolved": False,
                    }
                )
        return entries, unresolved

    try:
        return _paginated_scan(
            sp,
            period,
            count,
            criteria,
            cancel_check,
            fetch_page=lastfm_client_module.get_top_albums,
            build_page_entries=build_page_entries,
        )
    finally:
        resolve_cache.save("album", cache)


def _find_most_played_artists(
    sp: Spotify, period: str, count: int, criteria: list[dict], cancel_check: CancelCheck | None
) -> dict:
    cache = resolve_cache.load("artist")

    def build_page_entries(sp: Spotify, top: list[dict], cancel_check: CancelCheck | None):
        resolved_pairs = _resolve_generic(
            sp,
            top,
            cache,
            key_fn=lambda item: resolve_cache.artist_key(item["name"]),
            search_fn=_search_artist,
            cancel_check=cancel_check,
        )
        _fill_fallback_tags(
            resolved_pairs,
            tags_fn=lambda item: lastfm_client_module.get_artist_tags(item["name"]),
            cancel_check=cancel_check,
        )

        entries = []
        unresolved = 0
        for top_item, resolved in resolved_pairs:
            if resolved.get("found"):
                entries.append(
                    {
                        "uri": resolved["uri"],
                        "name": resolved["name"],
                        "genres": resolved["genres"],
                        "followers": resolved["followers"],
                        "popularity": resolved["popularity"],
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": resolved.get("image_url"),
                        "resolved": True,
                    }
                )
            else:
                unresolved += 1
                entries.append(
                    {
                        "uri": None,
                        "name": top_item["name"],
                        "genres": resolved.get("tags", []),
                        "followers": None,
                        "popularity": None,
                        "playcount": top_item["playcount"],
                        "rank": top_item["rank"],
                        "image_url": None,
                        "resolved": False,
                    }
                )
        return entries, unresolved

    try:
        return _paginated_scan(
            sp,
            period,
            count,
            criteria,
            cancel_check,
            fetch_page=lastfm_client_module.get_top_artists,
            build_page_entries=build_page_entries,
        )
    finally:
        resolve_cache.save("artist", cache)


def find_most_played(
    sp: Spotify,
    entity_type: str,
    period: str,
    count: int,
    criteria: list[dict],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """Returns {"results": [...], "unresolved_count": int, "scanned_count":
    int, "truncated": bool}, results kept in Last.fm rank order (most-played
    first). When criteria are given, pages further into scrobble history
    past `count` items to try to find `count` matches (see
    MAX_SCAN_MULTIPLIER) - scanned_count says how far it had to look, and
    truncated is True if it gave up short of `count` matches. See
    FIELD_OPERATORS for the filterable fields and result-dict shape per
    entity_type."""
    if entity_type == "track":
        return _find_most_played_tracks(sp, period, count, criteria, cancel_check)
    if entity_type == "album":
        return _find_most_played_albums(sp, period, count, criteria, cancel_check)
    if entity_type == "artist":
        return _find_most_played_artists(sp, period, count, criteria, cancel_check)
    raise ValueError(f"unknown entity_type: {entity_type!r}")
