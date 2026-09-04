from __future__ import annotations

import json
from pathlib import Path

from core.paths import DATA_DIR

# Persistent lookup of Last.fm name -> resolved Spotify metadata, so a
# repeat "most played" scan doesn't re-search Spotify for names it already
# resolved (or already confirmed have no match). Unlike playlist_cache,
# there's no staleness/snapshot concept here - a track/artist/album's
# identity doesn't change, so entries are kept indefinitely.
CACHE_DIR = DATA_DIR / ".lastfm_cache"

_ENTITY_FILES = {
    "track": "tracks.json",
    "album": "albums.json",
    "artist": "artists.json",
    "artist_genres": "artist_genres.json",
}


def _cache_path(entity_type: str) -> Path:
    return CACHE_DIR / _ENTITY_FILES[entity_type]


def load(entity_type: str) -> dict[str, dict]:
    path = _cache_path(entity_type)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save(entity_type: str, data: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(entity_type).write_text(json.dumps(data))


def track_key(artist: str, name: str) -> str:
    return f"{artist.strip().lower()}||{name.strip().lower()}"


def album_key(artist: str, name: str) -> str:
    return f"{artist.strip().lower()}||{name.strip().lower()}"


def artist_key(name: str) -> str:
    return name.strip().lower()
