from __future__ import annotations

import datetime
import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

import certifi
from spotipy import Spotify

from core.paths import DATA_DIR
from spotify.spotify_client import get_authenticated_client
import playlists.cascade as cascade_module

logger = logging.getLogger(__name__)

PENDING_REVIEW_PATH = DATA_DIR / "cascade_pending_review.json"
LAST_RUN_PATH = DATA_DIR / "cascade_last_run.json"
CHECK_INTERVAL_SECONDS = 300
DEFAULT_AUTO_HOUR = 8

# datetime.weekday(): Monday=0 ... Sunday=6 - keep this tuple in that order
# so _DAY_KEYS.index(key) lines up with it directly.
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_scheduler_lock = threading.Lock()
_scheduler_started = False


def _auto_enabled() -> bool:
    return os.environ.get("CASCADE_AUTO_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _configured_hour() -> int:
    try:
        hour = int(os.environ.get("CASCADE_AUTO_HOUR", str(DEFAULT_AUTO_HOUR)))
    except ValueError:
        hour = DEFAULT_AUTO_HOUR
    return max(0, min(23, hour))


def _configured_schedule() -> dict[int, int]:
    """Parses CASCADE_AUTO_SCHEDULE, e.g. {"mon": 9, "wed": 14, "fri": 18},
    into {weekday_int: hour_utc}. Days not listed don't run at all. Returns
    {} if unset/invalid, meaning "fall back to CASCADE_AUTO_HOUR every day"
    (see _target_hour_for)."""
    raw = os.environ.get("CASCADE_AUTO_SCHEDULE", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("cascade auto-scan: invalid JSON in CASCADE_AUTO_SCHEDULE, ignoring")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("cascade auto-scan: CASCADE_AUTO_SCHEDULE must be a JSON object, ignoring")
        return {}

    schedule: dict[int, int] = {}
    for day_key, hour in parsed.items():
        normalized = str(day_key).strip().lower()[:3]
        if normalized not in _DAY_KEYS:
            logger.warning("cascade auto-scan: unknown day %r in CASCADE_AUTO_SCHEDULE, ignoring", day_key)
            continue
        try:
            schedule[_DAY_KEYS.index(normalized)] = max(0, min(23, int(hour)))
        except (TypeError, ValueError):
            logger.warning("cascade auto-scan: invalid hour %r for day %r in CASCADE_AUTO_SCHEDULE, ignoring", hour, day_key)
    return schedule


def _target_hour_for(weekday: int) -> int | None:
    """Returns the UTC hour the scan should run at for this weekday
    (Monday=0..Sunday=6), or None if today isn't a scheduled day at all.
    CASCADE_AUTO_SCHEDULE takes priority when set; otherwise every day runs
    at CASCADE_AUTO_HOUR, matching the original single-hour behavior."""
    schedule = _configured_schedule()
    if schedule:
        return schedule.get(weekday)
    return _configured_hour()


def _webhook_url() -> str:
    return os.environ.get("CASCADE_WEBHOOK_URL", "").strip()


def _base_url() -> str | None:
    """Derives the app's own base URL (http://127.0.0.1:8888 locally, or
    the Railway domain in production) from SPOTIFY_REDIRECT_URI, which is
    already required to be correct for Spotify OAuth to work at all - so
    there's no separate env var to keep in sync."""
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()
    if not redirect_uri:
        return None
    parts = urlsplit(redirect_uri)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _read_last_run_date() -> str | None:
    if not LAST_RUN_PATH.exists():
        return None
    try:
        return json.loads(LAST_RUN_PATH.read_text()).get("date")
    except (json.JSONDecodeError, OSError):
        return None


def _mark_ran_today() -> None:
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    LAST_RUN_PATH.write_text(json.dumps({"date": today}))


def load_pending_review() -> dict | None:
    if not PENDING_REVIEW_PATH.exists():
        return None
    try:
        return json.loads(PENDING_REVIEW_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_pending_review() -> None:
    PENDING_REVIEW_PATH.unlink(missing_ok=True)


def _write_pending_review(steps: list[dict], summaries: list[dict]) -> None:
    PENDING_REVIEW_PATH.write_text(json.dumps({
        "steps": steps,
        "summaries": summaries,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }))


# Converts _default_cascade_steps()'s UI-prefill shape (camelCase field
# names matching each tool's own DEFAULT_* env var, used by the JS builder
# in templates/cascade.html to pre-fill a step card) into the real `steps`
# shape CascadeRun/scan_step expect - mirroring each step type's
# cascadeGetConfig() in templates/cascade.html exactly.

def _canonicalize_step(ui_step: dict) -> dict | None:
    step_type = ui_step["type"]
    if step_type == "duplicates":
        playlists = ui_step.get("playlists") or []
        if len(playlists) < 2:
            return None
        return {"type": "duplicates", "playlist_ids": [p["id"] for p in playlists]}
    if step_type == "playlist_filter":
        sources = ui_step.get("sources") or []
        if not sources:
            return None
        dest_mode = "new" if ui_step.get("destMode") == "new" else "existing"
        if dest_mode == "existing" and not ui_step.get("destPlaylistId"):
            return None
        if dest_mode == "new" and not ui_step.get("destNewName"):
            return None
        return {
            "type": "playlist_filter",
            "playlist_ids": [s["id"] for s in sources],
            "criteria": ui_step.get("criteria") or [],
            "destination_mode": dest_mode,
            "destination_playlist_id": ui_step.get("destPlaylistId"),
            "destination_playlist_name": ui_step.get("destPlaylistName"),
            "destination_name": ui_step.get("destNewName"),
        }
    if step_type == "playlist_cleanup":
        if not ui_step.get("playlistId"):
            return None
        return {
            "type": "playlist_cleanup",
            "playlist_id": ui_step["playlistId"],
            "field": ui_step.get("field"),
            "operator": ui_step.get("operator"),
            "value": ui_step.get("value"),
            "value2": ui_step.get("value2"),
        }
    if step_type == "playlist_diff":
        sources, targets = ui_step.get("sources") or [], ui_step.get("targets") or []
        if not sources or not targets:
            return None
        return {
            "type": "playlist_diff",
            "source_ids": [s["id"] for s in sources],
            "target_ids": [t["id"] for t in targets],
        }
    if step_type == "sync":
        if not ui_step.get("id1") or not ui_step.get("id2"):
            return None
        return {"type": "sync", "playlist_id_1": ui_step["id1"], "playlist_id_2": ui_step["id2"]}
    logger.warning("cascade auto-scan: unknown step type %r in default cascade, skipping", step_type)
    return None


def canonicalize_default_cascade(ui_steps: list[dict]) -> list[dict]:
    return [s for s in (_canonicalize_step(step) for step in ui_steps) if s is not None]


def _summarize_step(step: dict, result) -> dict:
    """Turns one scan_step() result into a JSON-safe, human-readable
    summary. count is the number of reviewable items (0 = nothing here)."""
    step_type = step["type"]
    label = cascade_module.STEP_LABELS[step_type]

    if step_type == "duplicates":
        count = len(result)
        detail = f"{count} duplicate track(s)"
    elif step_type == "playlist_filter":
        count = len(result["matches"])
        dest = (
            result["destination_name"]
            if result["destination_mode"] == "new"
            else result["destination_playlist_name"]
        )
        detail = f'{count} matching track(s) for "{dest}"'
    elif step_type == "playlist_cleanup":
        count = len(result["removals"])
        detail = f'{count} track(s) to remove from "{result["playlist_name"]}"'
    elif step_type == "playlist_diff":
        count = len(result["missing"])
        detail = f"{count} missing track(s)"
    elif step_type == "sync":
        count = len(result["to_add"]) + len(result["to_remove"])
        detail = f"{len(result['to_add'])} to add, {len(result['to_remove'])} to remove"
    else:
        raise ValueError(f"unknown step type {step_type!r}")

    return {"type": step_type, "label": label, "count": count, "detail": detail}


_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _post_webhook(url: str, text: str) -> None:
    payload = {"content": text} if "discord.com" in url else {"text": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        # Discord/Slack sit behind bot-detection (Cloudflare) that 403s
        # requests carrying urllib's default "Python-urllib/x.y" User-Agent.
        headers={"Content-Type": "application/json", "User-Agent": "Rotation-CascadeScheduler/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as resp:
            resp.read()
    except Exception:
        logger.exception("cascade auto-scan: failed to post webhook notification")


def _notify_pending_review(summaries: list[dict]) -> None:
    url = _webhook_url()
    if not url:
        return
    lines = [f"- {s['label']}: {s['detail']}" for s in summaries if s["count"] > 0]
    text = "Rotation: daily cascade scan found items to review.\n" + "\n".join(lines)
    base_url = _base_url()
    if base_url:
        text += f"\n\n{base_url}/"
    _post_webhook(url, text)


def _notify_reauth_needed() -> None:
    url = _webhook_url()
    if not url:
        return
    text = (
        "Rotation: daily cascade scan couldn't run - your Spotify login "
        "has expired. Open the app and log in again."
    )
    base_url = _base_url()
    if base_url:
        text += f"\n\n{base_url}/login"
    _post_webhook(url, text)


def run_daily_scan(default_steps_fn: Callable[[Spotify], list[dict] | None]) -> None:
    """default_steps_fn is app._default_cascade_steps, injected here to
    avoid a circular import. Scan-only - never calls apply_step/apply_current,
    so this can never change a real playlist on its own."""
    sp = get_authenticated_client()
    if sp is None:
        logger.warning("cascade auto-scan: no authenticated client, skipping")
        _notify_reauth_needed()
        _mark_ran_today()
        return

    ui_steps = default_steps_fn(sp)
    steps = canonicalize_default_cascade(ui_steps) if ui_steps else []
    if not steps:
        logger.info("cascade auto-scan: no default cascade configured, skipping")
        _mark_ran_today()
        return

    run = cascade_module.CascadeRun(steps)
    try:
        run.prefetch(sp)
        summaries = [_summarize_step(step, cascade_module.scan_step(run.cache, step)) for step in steps]
    except Exception:
        logger.exception("cascade auto-scan: scan failed")
        _mark_ran_today()
        return

    _mark_ran_today()

    if not any(s["count"] > 0 for s in summaries):
        logger.info("cascade auto-scan: nothing to review")
        return

    _write_pending_review(steps, summaries)
    _notify_pending_review(summaries)
    logger.info("cascade auto-scan: found items to review")


def _maybe_run(default_steps_fn) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    target_hour = _target_hour_for(now.weekday())
    if target_hour is None:
        return  # not a scheduled day
    if now.hour < target_hour:
        return
    if _read_last_run_date() == now.date().isoformat():
        return
    run_daily_scan(default_steps_fn)


def _scheduler_loop(default_steps_fn) -> None:
    schedule = _configured_schedule()
    if schedule:
        described = ", ".join(f"{_DAY_KEYS[d]}={h}:00" for d, h in sorted(schedule.items()))
        logger.info("cascade auto-scan: scheduler thread started (schedule: %s UTC)", described)
    else:
        logger.info("cascade auto-scan: scheduler thread started (hour=%d UTC, every day)", _configured_hour())
    while True:
        try:
            _maybe_run(default_steps_fn)
        except Exception:
            logger.exception("cascade auto-scan: scheduler iteration failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_scheduler(default_steps_fn: Callable[[Spotify], list[dict] | None]) -> None:
    """Starts the daemon scheduler thread once, only if CASCADE_AUTO_ENABLED
    and SPOTIFY_OWNER_ID are set. Safe to call more than once - only the
    first call actually starts the thread."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        if not _auto_enabled():
            logger.info("cascade auto-scan: disabled (CASCADE_AUTO_ENABLED not set)")
            return
        if not os.environ.get("SPOTIFY_OWNER_ID"):
            logger.warning("cascade auto-scan: SPOTIFY_OWNER_ID not set, refusing to start")
            return
        _scheduler_started = True
    threading.Thread(target=_scheduler_loop, args=(default_steps_fn,), daemon=True).start()
