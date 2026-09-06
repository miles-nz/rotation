from __future__ import annotations

import json
import logging
import time

import requests

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR
import lastfm.lastfm_client as lastfm_client_module

logger = logging.getLogger(__name__)

# Same directory as the resolve cache (lastfm/resolve_cache.py), just a
# different file - this has different lifecycle semantics (a sync
# watermark plus a full-history walk, not a pure memoized lookup), so it's
# a dedicated module rather than another resolve_cache entity type.
_PATH = DATA_DIR / ".lastfm_cache" / "scrobble_dates.json"

# One request per page, paced with this delay between requests, to stay
# comfortably under Last.fm's commonly-observed ~5 req/s per-IP limit
# (their public ToS reserves the right to set limits without publishing an
# exact number - see the plan/commit for the sources behind this figure).
_REQUEST_DELAY_SECONDS = 0.3
_PAGE_LIMIT = 200


def load() -> dict:
    if not _PATH.exists():
        return {"tracks": {}, "synced_through": None, "backfill_complete": False}
    try:
        return json.loads(_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tracks": {}, "synced_through": None, "backfill_complete": False}


def save(data: dict) -> None:
    _PATH.parent.mkdir(exist_ok=True)
    _PATH.write_text(json.dumps(data))


def _key(artist: str, name: str) -> str:
    return f"{artist.strip().lower()}||{name.strip().lower()}"


def lookup(data: dict, artist: str, name: str) -> dict:
    """Looks up a track's {"first", "last"} unix timestamps in an
    already-loaded index (see load()). Empty dict if never scrobbled or
    not yet indexed (i.e. sync() hasn't covered it - most likely nobody
    has ever called sync() at all) - callers should use .get("first") /
    .get("last"), which are then naturally None."""
    return data["tracks"].get(_key(artist, name), {})


def sync(cancel_check: CancelCheck | None = None) -> dict:
    """Backfills (first call) or incrementally updates (every call after)
    the scrobble-dates index by walking user.getRecentTracks. Returns the
    updated (and already-saved) index dict - same shape as load().

    Walks pages newest-to-oldest starting from page 1, passing
    from_ts=synced_through+1 on an incremental run so only scrobbles since
    last time are fetched at all. For every scrobble seen, each track's
    "first"/"last" are reduced via min()/max() across every page
    (including ones where the track already appeared) - not "skip if
    already present". This is deliberate: walking newest-to-oldest means
    the first time a track is *encountered* is its most recent play, not
    its first, so only an unconditional min() finds the true earliest;
    max() is the mirror of that same logic for the most recent play (and
    happens to equal "whichever occurrence was encountered first" in this
    newest-to-oldest walk, without needing that as a separate special
    case). Both reductions are correct for incremental runs too
    (everything fetched is guaranteed more recent than what's indexed, so
    min() leaves "first" untouched while max() naturally advances "last")
    - one code path handles backfill and incremental alike.

    Stops at an empty page (exhausted available history) or a
    cancellation. Saves after every page, so a cancelled or interrupted
    sync keeps whatever progress it made and resumes from there next time
    - synced_through only advances to cover a page once that page's
    tracks are already folded into the index.
    """
    data = load()
    tracks = data["tracks"]
    from_ts = (data["synced_through"] + 1) if data["synced_through"] else None
    newest_seen = data["synced_through"]

    page = 1
    while True:
        page_tracks, meta = _get_page_with_backoff(page, from_ts, cancel_check)
        if not page_tracks:
            break
        for t in page_tracks:
            key = _key(t["artist"], t["name"])
            entry = tracks.setdefault(key, {})
            entry["first"] = t["timestamp"] if "first" not in entry else min(entry["first"], t["timestamp"])
            entry["last"] = t["timestamp"] if "last" not in entry else max(entry["last"], t["timestamp"])
            if newest_seen is None or t["timestamp"] > newest_seen:
                newest_seen = t["timestamp"]

        data["synced_through"] = newest_seen
        if page >= meta["total_pages"]:
            data["backfill_complete"] = True
        save(data)

        logger.info(
            "scrobble history: page %d/%d, %d unique track(s) indexed",
            page,
            meta["total_pages"],
            len(tracks),
            extra={
                "progress": {
                    "id": "history_sync",
                    "name": "Building listening history index",
                    "current": page,
                    "total": meta["total_pages"],
                }
            },
        )
        check_cancelled(cancel_check)

        if page >= meta["total_pages"]:
            break
        page += 1
        time.sleep(_REQUEST_DELAY_SECONDS)

    return data


def _get_page_with_backoff(page: int, from_ts: int | None, cancel_check: CancelCheck | None):
    while True:
        try:
            return lastfm_client_module.get_recent_tracks(
                limit=_PAGE_LIMIT, page=page, from_ts=from_ts
            )
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                logger.warning("scrobble history: rate limited, backing off")
                check_cancelled(cancel_check)
                time.sleep(5)
                continue
            raise
