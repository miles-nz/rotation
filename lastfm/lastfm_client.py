from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_ROOT = "https://ws.audioscrobbler.com/2.0/"


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
