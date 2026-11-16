from __future__ import annotations

import logging
import time

import requests

from core.cancellation import CancelCheck, check_cancelled

logger = logging.getLogger(__name__)

# ListenBrainz Labs' similar-artists dataset: open, non-commercial, built
# from which artists people listen to in the same sessions. No API key or
# account needed.
SIMILAR_ARTISTS_URL = "https://labs.api.listenbrainz.org/similar-artists/json"

# The endpoint requires an algorithm name - this is the one ListenBrainz's
# own artist pages use.
SIMILAR_ARTISTS_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
)

# ListenBrainz asks API clients to identify themselves. Deliberately generic.
_HEADERS = {"User-Agent": "Rotation/1.0 (personal playlist tool)"}

_MAX_ATTEMPTS = 4


def get_similar_artists(mbid: str, cancel_check: CancelCheck | None = None) -> list[dict]:
    """Artists ListenBrainz considers similar to the artist with MusicBrainz
    ID mbid, most similar first. Returns [{"name", "mbid", "score"}, ...];
    score is unbounded (higher is more similar), so compare it only within
    one artist's list. An MBID ListenBrainz doesn't know returns [].

    Retries rate limits and server errors a few times, then raises - the
    caller decides whether to carry on without ListenBrainz."""
    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.get(
                SIMILAR_ARTISTS_URL,
                params={"artist_mbids": mbid, "algorithm": SIMILAR_ARTISTS_ALGORITHM},
                headers=_HEADERS,
                timeout=30,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.exceptions.HTTPError(response=response)
            response.raise_for_status()
            break
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt >= _MAX_ATTEMPTS:
                raise
            wait = 5 * attempt
            logger.warning("ListenBrainz similar artists: %s, retrying in %ds", status or type(exc).__name__, wait)
            check_cancelled(cancel_check)
            time.sleep(wait)

    payload = response.json() or []
    return [
        {"name": a["name"], "mbid": a.get("artist_mbid"), "score": float(a.get("score") or 0)}
        for a in payload
        if a.get("name")
    ]
