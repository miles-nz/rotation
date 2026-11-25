from __future__ import annotations

import logging
import threading
import time

from spotipy import Spotify, SpotifyException

from core.cancellation import CancelCheck, check_cancelled

logger = logging.getLogger(__name__)

# Spotify rate-limits over a rolling 30 second window. spotipy retries a 429
# only a few times and then raises without the Retry-After header, so a
# burst from several worker threads fails whole artists instead of just
# slowing down. These retries wait out the window instead.
_FIRST_WAIT = 5.0
_MAX_WAIT = 30.0
_MAX_ATTEMPTS = 6

_lock = threading.Lock()
_blocked_until = 0.0


def _wait_for_cooldown(cancel_check: CancelCheck | None) -> None:
    """Sleeps until no 429 cooldown is active. Shared by every thread, so
    one rate-limited call pauses the others too instead of each hammering
    Spotify on its own."""
    while True:
        with _lock:
            remaining = _blocked_until - time.monotonic()
        if remaining <= 0:
            return
        check_cancelled(cancel_check)
        time.sleep(min(remaining, 0.5))


def _start_cooldown(seconds: float) -> None:
    global _blocked_until
    with _lock:
        _blocked_until = max(_blocked_until, time.monotonic() + seconds)


def call_with_retry(fn, description: str, cancel_check: CancelCheck | None = None):
    """Calls a zero-arg Spotify API function, waiting and retrying when
    Spotify answers 429 (up to _MAX_ATTEMPTS calls, then the last error is
    raised). Any other error is raised straight away."""
    wait = _FIRST_WAIT
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _wait_for_cooldown(cancel_check)
        try:
            return fn()
        except SpotifyException as exc:
            if exc.http_status != 429 or attempt == _MAX_ATTEMPTS:
                raise
            logger.warning("%s: rate limited by Spotify, pausing %.0fs (attempt %d)", description, wait, attempt)
            _start_cooldown(wait)
            wait = min(wait * 2, _MAX_WAIT)


class _ThrottledSpotify:
    """Stands in for a Spotify client, sending each call through
    call_with_retry. Only methods are wrapped; anything else is passed on."""

    def __init__(self, sp: Spotify, cancel_check: CancelCheck | None):
        self._sp = sp
        self._cancel_check = cancel_check

    def __getattr__(self, name: str):
        attr = getattr(self._sp, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            return call_with_retry(lambda: attr(*args, **kwargs), f"spotify {name}", self._cancel_check)

        return call


def throttled(sp: Spotify, cancel_check: CancelCheck | None = None) -> Spotify:
    """sp, but a 429 makes its calls wait and retry rather than raise."""
    return _ThrottledSpotify(sp, cancel_check)  # type: ignore[return-value]
