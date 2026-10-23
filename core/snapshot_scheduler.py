from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time

from core.paths import DATA_DIR
import core.settings_store as settings_store
from spotify.spotify_client import get_authenticated_client
import playlists.snapshots as snapshots_module

logger = logging.getLogger(__name__)

# Same "last ran" marker approach as the Cascade auto-scan, so a redeploy
# after the day's run doesn't snapshot a second time. Independent of
# CASCADE_AUTO_ENABLED - snapshots always run.
LAST_RUN_PATH = DATA_DIR / "snapshots_last_run.json"
CHECK_INTERVAL_SECONDS = 300
DEFAULT_HOUR = 4

_scheduler_lock = threading.Lock()
_scheduler_started = False
_warned_logged_out_on: str | None = None


def configured_hour() -> int:
    try:
        stored = settings_store.get("snapshot_hour")
        hour = int(stored if stored is not None else os.environ.get("SNAPSHOT_HOUR", str(DEFAULT_HOUR)))
    except ValueError:
        hour = DEFAULT_HOUR
    return max(0, min(23, hour))


def env_settings() -> dict:
    """SNAPSHOT_HOUR in settings.json's shape, if it's set and valid - for
    the one-time copy into it at startup."""
    try:
        return {"snapshot_hour": max(0, min(23, int(os.environ["SNAPSHOT_HOUR"])))}
    except (KeyError, ValueError):
        return {}


def read_last_run() -> dict | None:
    """{"date", "at"} for the last completed daily run, or None."""
    if not LAST_RUN_PATH.exists():
        return None
    try:
        return json.loads(LAST_RUN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _mark_ran(now: datetime.datetime) -> None:
    LAST_RUN_PATH.write_text(
        json.dumps({"date": now.date().isoformat(), "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    )


def _maybe_run() -> None:
    global _warned_logged_out_on
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    if now.hour < configured_hour():
        return
    if (read_last_run() or {}).get("date") == today:
        return

    sp = get_authenticated_client()
    if sp is None:
        # Not marked as ran, so it picks up as soon as you log in again -
        # but only say so once a day rather than every check.
        if _warned_logged_out_on != today:
            logger.warning("snapshots: no authenticated client, will retry once logged in")
            _warned_logged_out_on = today
        return

    try:
        snapshots_module.take_snapshots(sp)
    except Exception:
        logger.exception("snapshots: daily run failed")
    _mark_ran(now)


def _scheduler_loop() -> None:
    logger.info("snapshots: scheduler thread started (hour=%d UTC, every day)", configured_hour())
    while True:
        try:
            _maybe_run()
        except Exception:
            logger.exception("snapshots: scheduler iteration failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_scheduler() -> None:
    """Starts the daemon scheduler thread once. Safe to call more than
    once - only the first call actually starts the thread."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()
