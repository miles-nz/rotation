from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from core.paths import DATA_DIR
import lastfm.forgotten_favourites as forgotten_favourites

DISMISS_MONTHS = 6

logger = logging.getLogger(__name__)

# Songs Ranking Check shouldn't flag for a while ("this is fine"), keyed by
# the song's key (see ranking_check). Each lasts DISMISS_MONTHS - long enough
# not to nag, short enough that a song whose plays keep drifting gets a
# second look - and remembers which ranking playlists the song was in when
# dismissed: it only stays hidden while that's still true, so moving the
# song brings its suggestions back sooner. On disk so it survives redeploys.
DISMISSALS_PATH = DATA_DIR / "ranking_dismissals.json"

_lock = threading.Lock()


def _format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> dict[str, dict]:
    if not DISMISSALS_PATH.exists():
        return {}
    try:
        return json.loads(DISMISSALS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("ranking dismissals file unreadable, starting fresh", exc_info=True)
        return {}


def _save(dismissals: dict[str, dict]) -> None:
    DISMISSALS_PATH.write_text(json.dumps(dismissals))


def _load() -> dict[str, dict]:
    """Unexpired dismissals. Expired ones are written out of the file as
    soon as they're noticed, so it doesn't keep growing. Call with _lock
    held."""
    dismissals = _read()
    now = _format(datetime.now(timezone.utc))
    active = {k: d for k, d in dismissals.items() if d.get("until", "") > now}
    if len(active) != len(dismissals):
        logger.info("pruning %d expired ranking dismissal(s)", len(dismissals) - len(active))
        _save(active)
    return active


def dismiss(result: dict) -> str:
    """result: one of ranking_check's results. Hides it for DISMISS_MONTHS
    (restarting the clock if it was already dismissed) and returns when the
    dismissal ends, as an ISO timestamp."""
    now = datetime.now(timezone.utc)
    until = _format(forgotten_favourites.months_ago(now, -DISMISS_MONTHS))
    with _lock:
        dismissals = _load()
        dismissals[result["key"]] = {
            "key": result["key"],
            "name": result["name"],
            "artists": result["artists"],
            "tier_ids": result["tier_ids"],
            "tier_names": [t["name"] for t in result["tiers"]],
            "dismissed_at": _format(now),
            "until": until,
        }
        _save(dismissals)
    return until


def restore(keys) -> None:
    """Un-dismisses keys - a restore from the page, or dismissals a scan
    found no longer apply because the song has moved."""
    keys = set(keys)
    if not keys:
        return
    with _lock:
        dismissals = _load()
        remaining = {k: d for k, d in dismissals.items() if k not in keys}
        if len(remaining) != len(dismissals):
            _save(remaining)


def active() -> dict[str, dict]:
    with _lock:
        return _load()


def list_active() -> list[dict]:
    """Unexpired dismissals, soonest to expire first."""
    return sorted(active().values(), key=lambda d: d["until"])
