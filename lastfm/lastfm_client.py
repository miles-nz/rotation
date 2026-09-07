from __future__ import annotations

import logging
import os
import time

import requests
from dotenv import load_dotenv

from core.cancellation import CancelCheck, check_cancelled

load_dotenv()

logger = logging.getLogger(__name__)

API_ROOT = "https://ws.audioscrobbler.com/2.0/"

# Last.fm's own server occasionally 500s transiently (seen in practice) -
# retried with escalating backoff, unlike 429 (which retries indefinitely
# since it's expected to self-resolve quickly), a persistent 5xx outage
# shouldn't hang a caller forever.
_MAX_SERVER_ERROR_RETRIES = 5


def call_with_retry(fn, description: str, cancel_check: CancelCheck | None = None):
    """Calls a zero-arg Last.fm API function, retrying on transient HTTP
    failures instead of letting them abort whatever multi-page operation is
    in progress (a paginated scan/sync loses all its progress otherwise).
    description is used only for logging (e.g. "page 4") to identify which
    call is being retried.
    """
    server_error_attempts = 0
    while True:
        try:
            return fn()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                logger.warning("%s: rate limited, backing off", description)
                check_cancelled(cancel_check)
                time.sleep(5)
                continue
            if status is not None and 500 <= status < 600 and server_error_attempts < _MAX_SERVER_ERROR_RETRIES:
                server_error_attempts += 1
                wait = min(5 * server_error_attempts, 30)
                logger.warning(
                    "%s: Last.fm returned %d, retrying (%d/%d) in %ds",
                    description,
                    status,
                    server_error_attempts,
                    _MAX_SERVER_ERROR_RETRIES,
                    wait,
                )
                check_cancelled(cancel_check)
                time.sleep(wait)
                continue
            raise


def _api_key() -> str:
    return os.environ["LASTFM_API_KEY"]


def _username() -> str:
    return os.environ["LASTFM_USERNAME"]


def _get(method: str, **params) -> dict:
    """Call a read-only Last.fm API method and return the parsed JSON body.
    Raises if the HTTP request fails or Last.fm returns an API error payload
    (e.g. bad key, unknown user) - Last.fm reports those with a 200 status.
    """
    response = requests.get(
        API_ROOT,
        params={"method": method, "api_key": _api_key(), "format": "json", **params},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"Last.fm API error {payload['error']}: {payload.get('message')}")
    return payload


def get_user_info(username: str | None = None) -> dict:
    """Basic profile info (play count, registration date, ...) for a user.
    Cheap connectivity check for a newly configured API key/username.
    """
    return _get("user.getinfo", user=username or _username())["user"]


def get_track_playcount(artist: str, track: str, username: str | None = None) -> int:
    """How many times the user has scrobbled this track, per track.getInfo.
    Returns 0 if never scrobbled or Last.fm can't match the track.
    """
    info = _get(
        "track.getinfo",
        artist=artist,
        track=track,
        username=username or _username(),
    )
    return int(info.get("track", {}).get("userplaycount", 0))


PERIODS = ["overall", "7day", "1month", "3month", "6month", "12month"]


def _as_list(value) -> list:
    # Last.fm's list endpoints return a bare object instead of a one-item
    # array when there's exactly one result, so a caller asking for a small
    # limit can't just index into the parsed JSON without checking this.
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def get_top_tracks(
    username: str | None = None, period: str = "overall", limit: int = 50, page: int = 1
) -> list[dict]:
    """The user's most-scrobbled tracks. Returns
    [{"rank", "artist", "name", "playcount"}, ...], most-played first.
    page lets a caller page past `limit` into deeper scrobble history
    (Last.fm returns an empty list once there's nothing left).
    """
    payload = _get(
        "user.gettoptracks", user=username or _username(), period=period, limit=limit, page=page
    )
    return [
        {
            "rank": int(t["@attr"]["rank"]),
            "artist": t["artist"]["name"],
            "name": t["name"],
            "playcount": int(t["playcount"]),
        }
        for t in _as_list(payload.get("toptracks", {}).get("track"))
    ]


def get_top_artists(
    username: str | None = None, period: str = "overall", limit: int = 50, page: int = 1
) -> list[dict]:
    """The user's most-scrobbled artists. Returns
    [{"rank", "name", "playcount"}, ...], most-played first.
    """
    payload = _get(
        "user.gettopartists", user=username or _username(), period=period, limit=limit, page=page
    )
    return [
        {
            "rank": int(a["@attr"]["rank"]),
            "name": a["name"],
            "playcount": int(a["playcount"]),
        }
        for a in _as_list(payload.get("topartists", {}).get("artist"))
    ]


def get_top_albums(
    username: str | None = None, period: str = "overall", limit: int = 50, page: int = 1
) -> list[dict]:
    """The user's most-scrobbled albums. Returns
    [{"rank", "artist", "name", "playcount"}, ...], most-played first.
    """
    payload = _get(
        "user.gettopalbums", user=username or _username(), period=period, limit=limit, page=page
    )
    return [
        {
            "rank": int(a["@attr"]["rank"]),
            "artist": a["artist"]["name"],
            "name": a["name"],
            "playcount": int(a["playcount"]),
        }
        for a in _as_list(payload.get("topalbums", {}).get("album"))
    ]


def _top_tag_names(payload: dict, limit: int) -> list[str]:
    tags = _as_list(payload.get("toptags", {}).get("tag"))
    return [t["name"] for t in tags][:limit]


def get_recent_tracks(
    username: str | None = None, limit: int = 200, page: int = 1, from_ts: int | None = None
) -> tuple[list[dict], dict]:
    """One page of scrobble history, newest first. Returns
    ([{"artist", "name", "timestamp"}, ...], {"page", "total_pages"}).
    Skips the in-progress "now playing" entry, if present - it has no
    "date" yet since it hasn't finished being scrobbled. from_ts, if
    given, only returns scrobbles after that unix timestamp (for
    incremental syncs - avoids re-walking already-seen history).
    """
    params = {"user": username or _username(), "limit": limit, "page": page}
    if from_ts is not None:
        params["from"] = from_ts
    payload = _get("user.getrecenttracks", **params)
    recenttracks = payload.get("recenttracks", {})
    attr = recenttracks.get("@attr", {})
    tracks = [
        {
            "artist": t["artist"]["#text"],
            "name": t["name"],
            "timestamp": int(t["date"]["uts"]),
        }
        for t in _as_list(recenttracks.get("track"))
        if t.get("date")
    ]
    return tracks, {
        "page": int(attr.get("page", page)),
        "total_pages": int(attr.get("totalPages", 0)),
    }


def get_track_tags(artist: str, track: str, limit: int = 5) -> list[str]:
    """User-submitted tags for a track, most-applied first - Last.fm's
    closest thing to a genre for a track that couldn't be matched on
    Spotify. Public data, no username needed."""
    return _top_tag_names(_get("track.gettoptags", artist=artist, track=track), limit)


def get_album_tags(artist: str, album: str, limit: int = 5) -> list[str]:
    """Same as get_track_tags, for an album."""
    return _top_tag_names(_get("album.gettoptags", artist=artist, album=album), limit)


def get_artist_tags(artist: str, limit: int = 5) -> list[str]:
    """Same as get_track_tags, for an artist."""
    return _top_tag_names(_get("artist.gettoptags", artist=artist), limit)
