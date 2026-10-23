from __future__ import annotations

from typing import Any, Callable

# The env var each tool's default used to live in (and still falls back
# to), keyed by the tool name used in /defaults/<tool> and settings.json.
TOOL_ENV_VARS = {
    "liked_songs_sync": "DEFAULT_LIKED_SONGS_SYNC",
    "duplicates": "DEFAULT_DUPLICATES",
    "playlist_filter": "DEFAULT_PLAYLIST_FILTER",
    "playlist_cleanup": "DEFAULT_PLAYLIST_CLEANUP",
    "playlist_diff": "DEFAULT_PLAYLIST_DIFF",
    "playlist_prepend": "DEFAULT_PLAYLIST_PREPEND",
    "playlist_search": "DEFAULT_PLAYLIST_SEARCH",
    "album_swap": "DEFAULT_ALBUM_SWAP",
    "cascade": "DEFAULT_CASCADE",
    "ranking_check": "DEFAULT_RANKING_CHECK",
}

# Step types a saved Cascade default may list (see _CASCADE_STEP_PREF_KEYS
# in app.py, which maps each to the tool default holding its settings).
CASCADE_STEP_TYPES = {"duplicates", "playlist_filter", "playlist_cleanup", "playlist_diff", "sync"}


# Each validator returns a cleaned copy holding only the keys that tool's
# page reads, or raises ValueError - so a save can't put arbitrary JSON in
# the settings file.


def _str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _opt_str(value: Any, what: str) -> str | None:
    if value is None or value == "":
        return None
    return _str(value, what)


def _playlist(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each playlist must be an object")
    return {"id": _str(value.get("id"), "playlist id"), "name": _str(value.get("name"), "playlist name")}


def _playlists(value: Any, what: str, min_count: int = 1) -> list[dict]:
    if not isinstance(value, list) or len(value) < min_count:
        raise ValueError(f"{what} needs at least {min_count} playlist(s)")
    return [_playlist(p) for p in value]


def _criterion(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each criterion must be an object")
    return {
        "field": _str(value.get("field"), "criterion field"),
        "operator": _str(value.get("operator"), "criterion operator"),
        "value": _opt_str(value.get("value"), "criterion value"),
        "value2": _opt_str(value.get("value2"), "criterion value2"),
    }


def _criteria(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("criteria needs at least one condition")
    return [_criterion(c) for c in value]


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("default must be an object")
    return value


def _liked_songs_sync(v: Any) -> dict:
    v = _object(v)
    return {k: _str(v.get(k), k) for k in ("id1", "name1", "id2", "name2")}


def _playlist_diff(v: Any) -> dict:
    v = _object(v)
    return {"sources": _playlists(v.get("sources"), "sources"), "targets": _playlists(v.get("targets"), "targets")}


def _playlist_cleanup(v: Any) -> dict:
    v = _object(v)
    return {
        "playlistId": _str(v.get("playlistId"), "playlistId"),
        "playlistName": _str(v.get("playlistName"), "playlistName"),
        **_criterion(v),
    }


def _playlist_filter(v: Any) -> dict:
    v = _object(v)
    dest_mode = _str(v.get("destMode"), "destMode")
    if dest_mode not in {"existing", "new"}:
        raise ValueError("destMode must be 'existing' or 'new'")
    return {
        "sources": _playlists(v.get("sources"), "sources"),
        "criteria": _criteria(v.get("criteria")),
        "destMode": dest_mode,
        "destPlaylistId": _opt_str(v.get("destPlaylistId"), "destPlaylistId"),
        "destPlaylistName": _opt_str(v.get("destPlaylistName"), "destPlaylistName"),
        "destNewName": _opt_str(v.get("destNewName"), "destNewName"),
    }


def _playlist_prepend(v: Any) -> dict:
    v = _object(v)
    return {k: _str(v.get(k), k) for k in ("sourceId", "sourceName", "destinationId", "destinationName")}


def _playlist_search(v: Any) -> dict:
    v = _object(v)
    excludes = v.get("excludes")
    return {
        "sources": _playlists(v.get("sources"), "sources"),
        "criteria": _criteria(v.get("criteria")),
        "excludes": _playlists(excludes, "excludes", min_count=0),
    }


def _cascade(v: Any) -> list[str]:
    if not isinstance(v, list) or not v:
        raise ValueError("cascade needs at least one step")
    for step in v:
        if step not in CASCADE_STEP_TYPES:
            raise ValueError(f"unknown cascade step {step!r}")
    if len(set(v)) != len(v):
        raise ValueError("each cascade step type can only appear once")
    return list(v)


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "liked_songs_sync": _liked_songs_sync,
    "duplicates": lambda v: _playlists(v, "duplicates", min_count=2),
    "playlist_filter": _playlist_filter,
    "playlist_cleanup": _playlist_cleanup,
    "playlist_diff": _playlist_diff,
    "playlist_prepend": _playlist_prepend,
    "playlist_search": _playlist_search,
    "album_swap": lambda v: _playlists(v, "album_swap"),
    "cascade": _cascade,
    "ranking_check": lambda v: _playlists(v, "ranking_check"),
}


def validate(tool: str, value: Any) -> Any:
    """Cleaned copy of value for storing as tool's default. None (clearing
    the default) is always allowed. Raises KeyError for an unknown tool and
    ValueError for a bad shape."""
    validator = _VALIDATORS[tool]
    return None if value is None else validator(value)
