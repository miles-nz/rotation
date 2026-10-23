from __future__ import annotations

import os

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.cache_handler import CacheFileHandler, CacheHandler
from spotipy.oauth2 import SpotifyOAuth

from core.paths import DATA_DIR

load_dotenv()

SCOPES = " ".join(
    [
        "user-library-read",
        "user-library-modify",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-public",
        "playlist-modify-private",
    ]
)

CACHE_PATH = str(DATA_DIR / ".cache")


def _client_id() -> str:
    return os.environ["SPOTIFY_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["SPOTIFY_CLIENT_SECRET"]


def _redirect_uri() -> str:
    return os.environ["SPOTIFY_REDIRECT_URI"]


def make_oauth(open_browser: bool = True, cache_handler: CacheHandler | None = None) -> SpotifyOAuth:
    """Build the OAuth manager. open_browser=True lets spotipy run its own
    one-off local server to catch the redirect (for standalone CLI scripts).
    Use open_browser=False inside the Flask app, which owns the redirect
    port itself and handles /callback manually. Pass a cache_handler (e.g.
    MemoryCacheHandler) to exchange a login code without touching the shared
    token file - the app's /callback does this so it can check whose
    account it is before saving anything.
    """
    return SpotifyOAuth(
        client_id=_client_id(),
        client_secret=_client_secret(),
        redirect_uri=_redirect_uri(),
        scope=SCOPES,
        cache_handler=cache_handler or CacheFileHandler(cache_path=CACHE_PATH),
        open_browser=open_browser,
    )


def save_token(token_info: dict) -> None:
    """Store token_info as the app's one shared token - the one every
    request and the background schedulers use via get_authenticated_client."""
    CacheFileHandler(cache_path=CACHE_PATH).save_token_to_cache(token_info)


def get_cli_spotify_client() -> Spotify:
    """For standalone scripts (e.g. list_playlists.py). Triggers the
    interactive browser login on first run, then reuses the cached token.
    """
    return Spotify(auth_manager=make_oauth(open_browser=True))


def get_authenticated_client() -> Spotify | None:
    """For use inside the Flask app. Returns a client only if a valid
    (possibly refreshed) cached token covering all of SCOPES is available;
    otherwise None, so the caller can redirect the user to log in. Using
    validate_token (rather than just checking expiry) matters because it
    also rejects a cached token that predates a scope being added here -
    forcing a fresh login instead of failing later with a 403 from Spotify.
    """
    oauth = make_oauth(open_browser=False)
    token_info = oauth.validate_token(oauth.cache_handler.get_cached_token())
    if not token_info:
        return None
    return Spotify(auth=token_info["access_token"])
