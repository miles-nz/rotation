from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from spotipy import Spotify
from spotipy.cache_handler import MemoryCacheHandler

from core.background_job import BackgroundJob
import core.cascade_scheduler as cascade_scheduler
import core.snapshot_scheduler as snapshot_scheduler
from core.paths import DATA_DIR
import core.settings_store as settings_store
import core.tool_defaults as tool_defaults
from spotify.spotify_client import make_oauth, get_authenticated_client, save_token
from core.sync import apply_diff, get_target_diff
import playlists.cascade as cascade_module
import playlists.diff_snoozes as diff_snoozes_module
import playlists.duplicates as duplicates_module
import playlists.not_duplicates as not_duplicates_module
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter_module
import playlists.playlist_cleanup as playlist_cleanup_module
import playlists.playlist_diff as playlist_diff_module
import playlists.playlist_prepend as playlist_prepend_module
import playlists.playlist_search as playlist_search_module
import playlists.playlist_swap as playlist_swap_module
import playlists.ranking_check as ranking_check_module
import playlists.ranking_dismissals as ranking_dismissals_module
import playlists.removal_history as removal_history_module
import playlists.snapshots as snapshots_module
import lastfm.forgotten_favourites as forgotten_favourites_module
import lastfm.lastfm_client as lastfm_client_module
import lastfm.most_played as most_played_module
import visuals.artist_river as artist_river_module
import visuals.listening_shape as listening_shape_module

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("app")

# Above this many combined add/remove tracks, skip fetching display names
# up front (that's one extra API request per 50 tracks) and just show
# counts; names can be loaded on demand via /diff/names.
NAME_LOOKUP_THRESHOLD = 200

# The one Spotify account allowed to use the app (see
# _require_owner_session). Per-tool defaults are only ever shown to it.
SPOTIFY_OWNER_ID = os.environ.get("SPOTIFY_OWNER_ID")


def _parse_json_env(name: str):
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in env var %s", name)
        return None


def _stored_default(tool: str):
    """tool's default playlist selection: whatever was saved from its page
    (settings.json on the volume), else its old DEFAULT_* env var."""
    env_name = tool_defaults.TOOL_ENV_VARS[tool]
    return settings_store.get(f"default:{tool}", lambda: _parse_json_env(env_name))


def _copy_env_settings_into_store() -> None:
    """One-time copy of the settings that used to live only in env vars
    (each tool's DEFAULT_* plus the scheduler times) into settings.json, so
    the env vars can be deleted afterwards. Only fills keys that have never
    been set in the app, so it never overwrites anything saved there - and
    once a key is copied, later changes to its env var are ignored. An env
    value that isn't in a shape the app can use is left uncopied (it still
    works as a fallback) and logged."""
    values = {}
    for tool, env_name in tool_defaults.TOOL_ENV_VARS.items():
        raw = _parse_json_env(env_name)
        if raw is None:
            continue
        try:
            values[f"default:{tool}"] = tool_defaults.validate(tool, raw)
        except ValueError as e:
            logger.warning("Not copying %s into settings: %s", env_name, e)
    values.update(cascade_scheduler.env_settings())
    values.update(snapshot_scheduler.env_settings())
    added = settings_store.seed_missing(values)
    if added:
        logger.info("Copied env var settings into settings.json: %s", ", ".join(added))


_copy_env_settings_into_store()


def _default_preferences(tool: str, sp):
    # No ownership check needed here: _require_owner_session only lets the
    # owner's browser in, and only the owner's token is ever saved.
    if sp is None:
        return None
    return _stored_default(tool)


# Maps a cascade step type to the tool whose default holds that step's
# settings, so the Cascade default only has to name which steps to
# pre-populate and in what order - not repeat their settings.
_CASCADE_STEP_PREF_KEYS = {
    "duplicates": "duplicates",
    "playlist_filter": "playlist_filter",
    "playlist_cleanup": "playlist_cleanup",
    "playlist_diff": "playlist_diff",
    "sync": "liked_songs_sync",
}


def _default_cascade_steps(sp):
    """The Cascade default is just an ordered list of step types, e.g.
    ["playlist_diff", "duplicates"]. Each step's actual config comes from
    that tool's own default, so there's one source of truth per playlist
    selection. A step whose tool has no default is skipped."""
    step_types = _default_preferences("cascade", sp)
    if not step_types:
        return None

    steps = []
    for step_type in step_types:
        pref_key = _CASCADE_STEP_PREF_KEYS.get(step_type)
        base = _stored_default(pref_key) if pref_key else None
        if not base:
            continue
        if step_type == "duplicates":
            steps.append({"type": step_type, "playlists": base})
        else:
            steps.append({"type": step_type, **base})
    return steps or None


app = Flask(__name__)


def _load_secret_key() -> str:
    """Signs the session cookie that proves a browser logged in as the
    owner. SECRET_KEY wins if set; otherwise one is generated once and kept
    on the volume, so logins survive redeploys without another env var.
    O_EXCL makes the first writer win if two processes race to create it."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    path = DATA_DIR / ".flask_secret_key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


app.secret_key = _load_secret_key()
app.config.update(
    # Lax keeps the cookie off cross-site POSTs, so another site can't
    # submit forms here as you, while still sending it on Spotify's
    # redirect back to /callback.
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SPOTIFY_REDIRECT_URI", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


@app.template_filter("duration")
def _format_duration(duration_ms) -> str:
    if duration_ms is None:
        return ""
    total_seconds = int(duration_ms) // 1000
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"

_sync_job = BackgroundJob(["core.sync", "app"])
_dup_job = BackgroundJob(["playlists.duplicates", "app"])
_filter_job = BackgroundJob(["playlists.playlist_filter", "app"])
_cleanup_job = BackgroundJob(["playlists.playlist_cleanup", "app"])
_diff_job = BackgroundJob(["playlists.playlist_diff", "app"])
_search_job = BackgroundJob(["playlists.playlist_search", "app"])
_prepend_job = BackgroundJob(["playlists.playlist_prepend", "app"])
_cascade_job = BackgroundJob(["playlists.playlist_cache", "app"])
_most_played_job = BackgroundJob(["lastfm.most_played", "lastfm.scrobble_history", "app"])
_swap_job = BackgroundJob(["playlists.playlist_swap", "app"])
_forgotten_job = BackgroundJob(
    [
        "lastfm.forgotten_favourites",
        "lastfm.most_played",
        "lastfm.scrobble_history",
        "playlists.playlist_cache",
        "app",
    ]
)
_ranking_job = BackgroundJob(
    [
        "playlists.ranking_check",
        "playlists.playlist_cache",
        "lastfm.scrobble_history",
        "app",
    ]
)
_visuals_job = BackgroundJob(
    [
        "lastfm.scrobble_history",
        "lastfm.most_played",
        "visuals.album_colours",
        "visuals.artist_river",
        "visuals.listening_shape",
        "app",
    ]
)
_snapshot_job = BackgroundJob(["playlists.snapshots", "playlists.playlist_cache", "core.sync", "app"])
_cascade_run: cascade_module.CascadeRun | None = None

# Gunicorn imports this file as the "app" module (not "__main__"), so it
# always starts the scheduler here. Locally, `python app.py` runs this file
# as "__main__", and app.run(debug=True) below spawns Werkzeug's reloader,
# which re-executes the whole script in a child process with
# WERKZEUG_RUN_MAIN=true - only start the thread there, not in the parent
# watcher process, to avoid a duplicate scheduler.
if __name__ != "__main__" or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    cascade_scheduler.start_scheduler(_default_cascade_steps)
    snapshot_scheduler.start_scheduler()


def _credentials_configured() -> bool:
    return bool(
        os.environ.get("SPOTIFY_CLIENT_ID")
        and os.environ.get("SPOTIFY_CLIENT_SECRET")
        and os.environ.get("SPOTIFY_REDIRECT_URI")
    )


# Everything else requires a browser that logged in as SPOTIFY_OWNER_ID.
# The Spotify token itself is shared server-side (the schedulers need it
# with no browser around), so this session check is what stops anyone who
# finds the URL from acting as the owner.
_PUBLIC_ENDPOINTS = {"login", "callback", "logout", "static"}


@app.before_request
def _require_owner_session():
    if request.endpoint in _PUBLIC_ENDPOINTS or not _credentials_configured():
        return None
    if SPOTIFY_OWNER_ID and session.get("user_id") == SPOTIFY_OWNER_ID:
        return None
    if request.method != "GET" or request.path.startswith("/api/"):
        return jsonify({"error": "not_authenticated"}), 401
    if request.endpoint != "home":
        return redirect(url_for("home"))
    return render_template("home.html", needs_credentials=False, logged_in=False)


def _lastfm_configured() -> bool:
    return bool(os.environ.get("LASTFM_API_KEY") and os.environ.get("LASTFM_USERNAME"))


def _track_labels(sp: Spotify, uris: set[str]) -> list[str]:
    ids = [uri.split(":")[-1] for uri in uris]
    labels = []
    for i in range(0, len(ids), 50):
        batch = sp.tracks(ids[i : i + 50])["tracks"]
        for track in batch:
            if not track:
                continue
            artists = ", ".join(a["name"] for a in track["artists"])
            labels.append(f"{artists} – {track['name']}")
        logger.info("looked up %d/%d track names", min(i + 50, len(ids)), len(ids))
    return sorted(labels)


def _run_sync_diff(playlist_id_1: str, playlist_id_2: str):
    def target(cancel_check):
        sp = get_authenticated_client()
        return get_target_diff(sp, playlist_id_1, playlist_id_2, cancel_check=cancel_check)

    return target


def _run_duplicate_scan(playlist_ids: list[str]):
    def target(cancel_check):
        sp = get_authenticated_client()
        return duplicates_module.find_duplicates(sp, playlist_ids, cancel_check=cancel_check)

    return target


def _run_playlist_filter_scan(
    playlist_ids: list[str],
    criteria: list[dict],
    destination: dict,
):
    def target(cancel_check):
        sp = get_authenticated_client()
        matches = playlist_filter_module.find_matches(
            sp, playlist_ids, criteria, cancel_check=cancel_check
        )

        already_in_destination = 0
        similar_versions: dict = {}
        if destination["destination_mode"] == "existing":
            logger.info(
                "fetching destination playlist '%s'", destination["destination_playlist_name"]
            )
            destination_tracks = playlist_filter_module.get_playlist_track_details(
                sp, destination["destination_playlist_id"], cancel_check=cancel_check
            )
            existing_uris = {t["uri"] for t in destination_tracks}
            before_count = len(matches)
            matches = playlist_filter_module.exclude_existing(matches, existing_uris)
            already_in_destination = before_count - len(matches)
            similar_versions = playlist_filter_module.find_similar_versions(
                matches, destination_tracks
            )

        return {
            "matches": matches,
            "already_in_destination": already_in_destination,
            "similar_versions": similar_versions,
            **destination,
        }

    return target


def _run_playlist_cleanup_scan(
    playlist_id: str, field: str, operator: str, value: str, value2: str | None
):
    def target(cancel_check):
        sp = get_authenticated_client()
        playlist = playlist_cache_module.get_playlist(sp, playlist_id, cancel_check)
        playlist_filter_module.attach_genres(
            sp, playlist["tracks"], [{"field": field}], cancel_check
        )
        removals = playlist_cleanup_module.find_removals_from_tracks(
            playlist["name"], playlist["tracks"], field, operator, value, value2
        )
        return {
            "playlist_id": playlist_id,
            "playlist_name": playlist["name"],
            "removals": removals,
        }

    return target


def _run_playlist_diff_scan(source_ids: list[str], target_ids: list[str]):
    def target(cancel_check):
        sp = get_authenticated_client()
        return playlist_diff_module.find_missing(
            sp, source_ids, target_ids, cancel_check=cancel_check
        )

    return target


def _run_playlist_search_scan(
    include_playlist_ids: list[str],
    criteria: list[dict],
    exclude_playlist_ids: list[str],
):
    def target(cancel_check):
        sp = get_authenticated_client()
        results = playlist_search_module.search(
            sp,
            include_playlist_ids,
            criteria,
            exclude_playlist_ids=exclude_playlist_ids,
            cancel_check=cancel_check,
        )
        return {"results": results}

    return target


def _run_most_played_scan(entity_type: str, period: str, count: int, criteria: list[dict]):
    def target(cancel_check):
        sp = get_authenticated_client()
        result = most_played_module.find_most_played(
            sp, entity_type, period, count, criteria, cancel_check=cancel_check
        )
        return {"entity_type": entity_type, "count": count, **result}

    return target


def _run_forgotten_favourites_scan(min_plays: int, months: int):
    def target(cancel_check):
        sp = get_authenticated_client()
        result = forgotten_favourites_module.find_forgotten_favourites(
            sp, min_plays, months, cancel_check=cancel_check
        )
        return {"min_plays": min_plays, "months": months, **result}

    return target


def _run_ranking_check_scan(tier_ids: list[str], year_id: str, period: str, only_added: bool):
    def target(cancel_check):
        sp = get_authenticated_client()
        result = ranking_check_module.find_inconsistencies(
            sp,
            [{"id": tier_id} for tier_id in tier_ids],
            year_id,
            period,
            only_added,
            ranking_dismissals_module.active(),
            cancel_check=cancel_check,
        )
        ranking_dismissals_module.restore(result["stale_dismissals"])
        return result

    return target


def _run_listening_shape(from_ts: int, to_ts: int, settings: dict):
    def target(cancel_check):
        sp = get_authenticated_client()
        result = listening_shape_module.build(sp, from_ts, to_ts, cancel_check=cancel_check)
        return {"view": "shape", "from_ts": from_ts, "to_ts": to_ts, "settings": settings, **result}

    return target


def _run_colour_month(from_ts: int, to_ts: int, settings: dict):
    def target(cancel_check):
        sp = get_authenticated_client()
        # The same plays-with-album-colours Listening Shape draws - the
        # month's tiles are worked out in the browser.
        result = listening_shape_module.build(sp, from_ts, to_ts, cancel_check=cancel_check)
        return {"view": "month", "from_ts": from_ts, "to_ts": to_ts, "settings": settings, **result}

    return target


def _run_artist_river(from_ts: int, to_ts: int, settings: dict):
    def target(cancel_check):
        sp = get_authenticated_client()
        result = artist_river_module.build(sp, from_ts, to_ts, cancel_check=cancel_check)
        return {"view": "river", "from_ts": from_ts, "to_ts": to_ts, "settings": settings, **result}

    return target


def _run_playlist_prepend_scan(source_id: str, destination_id: str):
    def target(cancel_check):
        sp = get_authenticated_client()
        return playlist_prepend_module.find_prependable(
            sp, source_id, destination_id, cancel_check=cancel_check
        )

    return target


def _run_album_swap_scan(album_id: str, playlist_ids: list[str]):
    def target(cancel_check):
        sp = get_authenticated_client()
        return playlist_swap_module.find_swaps(
            sp, album_id, playlist_ids, cancel_check=cancel_check
        )

    return target


def _run_cascade_prefetch(run: cascade_module.CascadeRun):
    def target(cancel_check):
        sp = get_authenticated_client()
        run.prefetch(sp, cancel_check)

    return target


def _cascade_step_view(sp: Spotify, run: cascade_module.CascadeRun):
    """Returns (template_name, context) for the current step's result,
    reusing each function's own result template with a cascade banner and
    an apply action pointed at /cascade/step/apply."""
    step = run.current_step
    result = run.scan_current()
    step_type = step["type"]
    banner = {
        "cascade_step": run.index + 1,
        "cascade_total": len(run.steps),
        "cascade_label": cascade_module.STEP_LABELS[step_type],
        "form_action": url_for("cascade_step_apply"),
        "cancel_url": url_for("cascade_picker"),
    }

    if step_type == "duplicates":
        return "duplicates_result.html", {"duplicates": result, **banner}

    if step_type == "playlist_filter":
        destination_label = (
            result["destination_name"]
            if result["destination_mode"] == "new"
            else result["destination_playlist_name"]
        )
        return "playlist_filter_result.html", {
            "matches": result["matches"],
            "destination_label": destination_label,
            "destination_mode": result["destination_mode"],
            "already_in_destination": result["already_in_destination"],
            "similar_versions": result.get("similar_versions", {}),
            **banner,
        }

    if step_type == "playlist_cleanup":
        return "playlist_cleanup_result.html", {
            "playlist_name": result["playlist_name"],
            "removals": result["removals"],
            **banner,
        }

    if step_type == "playlist_diff":
        return "playlist_diff_result.html", {
            "missing": result["missing"],
            "targets": result["targets"],
            **banner,
        }

    if step_type == "sync":
        to_add, to_remove = result["to_add"], result["to_remove"]
        if len(to_add) + len(to_remove) > NAME_LOOKUP_THRESHOLD:
            return "diff.html", {
                "add_count": len(to_add),
                "remove_count": len(to_remove),
                "names_loaded": False,
                **banner,
            }
        return "diff.html", {
            "add_count": len(to_add),
            "remove_count": len(to_remove),
            "add_labels": _track_labels(sp, to_add),
            "remove_labels": _track_labels(sp, to_remove),
            "names_loaded": True,
            **banner,
        }

    raise ValueError(f"unknown step type {step_type!r}")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/")
def home():
    if not _credentials_configured():
        return render_template("home.html", needs_credentials=True)

    sp = get_authenticated_client()
    pending_review = cascade_scheduler.load_pending_review() if sp is not None else None
    return render_template(
        "home.html",
        needs_credentials=False,
        logged_in=sp is not None,
        pending_review=pending_review,
    )


@app.route("/api/playlists")
def api_playlists():
    sp = get_authenticated_client()
    if sp is None:
        return jsonify({"error": "not_authenticated"}), 401

    playlists = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for playlist in results["items"]:
            images = playlist.get("images") or []
            playlists.append(
                {
                    "id": playlist["id"],
                    "name": playlist["name"],
                    "image_url": images[0]["url"] if images else None,
                }
            )
        results = sp.next(results) if results.get("next") else None

    return jsonify(playlists)


@app.route("/search")
def search_page():
    if not _credentials_configured():
        return render_template("search.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "search.html",
        needs_credentials=False,
        logged_in=sp is not None,
    )


@app.route("/api/search")
def api_search():
    sp = get_authenticated_client()
    if sp is None:
        return jsonify({"error": "not_authenticated"}), 401

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    results = sp.search(q=query, type="track", limit=10)
    tracks = []
    for track in results["tracks"]["items"]:
        images = track["album"].get("images") or []
        tracks.append(
            {
                "uri": track["uri"],
                "name": track["name"],
                "artists": ", ".join(a["name"] for a in track["artists"]),
                "image_url": images[0]["url"] if images else None,
            }
        )

    return jsonify(tracks)


@app.route("/all-playlists")
def all_playlists_page():
    if not _credentials_configured():
        return render_template("all_playlists.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "all_playlists.html",
        needs_credentials=False,
        logged_in=sp is not None,
    )


@app.route("/login")
def login():
    # Random state, checked in /callback, so a login can only be completed
    # by the browser that started it.
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    return redirect(make_oauth(open_browser=False).get_authorize_url(state=state))


@app.route("/callback")
def callback():
    expected_state = session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(request.args.get("state", ""), expected_state):
        return render_template(
            "login_error.html",
            message="That login link expired or came from a different browser. "
            "Make sure you open the app at the same address as SPOTIFY_REDIRECT_URI.",
        ), 400
    code = request.args.get("code")
    if not code:
        return redirect(url_for("home"))

    # Exchange the code in memory first - only the owner's token is ever
    # written to the shared token file the rest of the app uses.
    cache = MemoryCacheHandler()
    access_token = make_oauth(open_browser=False, cache_handler=cache).get_access_token(
        code, as_dict=False, check_cache=False
    )
    user_id = Spotify(auth=access_token).current_user()["id"]

    if not SPOTIFY_OWNER_ID:
        return render_template(
            "login_error.html",
            message="This app only lets its owner in, but SPOTIFY_OWNER_ID isn't set.",
            user_id=user_id,
        ), 403
    if user_id != SPOTIFY_OWNER_ID:
        logger.warning("Rejected Spotify login from non-owner account %s", user_id)
        return render_template("login_error.html", message="This app is private."), 403

    save_token(cache.get_cached_token())
    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    # Only forgets this browser - the shared token stays so the daily
    # scans and snapshots keep running.
    session.clear()
    return redirect(url_for("home"))


# --- Saved defaults and settings ---------------------------------------

# A saved default is a few hundred bytes; anything far bigger didn't come
# from this app's pages.
_MAX_DEFAULT_BODY = 64 * 1024


@app.route("/defaults/<tool>", methods=["POST"])
def save_default(tool):
    """Saves (or, with a null body, clears) tool's default selection.
    POST + JSON only: the session cookie isn't sent on cross-site POSTs, and
    a cross-site JSON request would need a CORS preflight this app never
    approves - so only the app's own pages can call this."""
    if tool not in tool_defaults.TOOL_ENV_VARS:
        return jsonify({"error": "unknown_tool"}), 404
    if not request.is_json:
        return jsonify({"error": "expected_json"}), 415
    if request.content_length is None or request.content_length > _MAX_DEFAULT_BODY:
        return jsonify({"error": "too_large"}), 413
    try:
        value = tool_defaults.validate(tool, request.get_json())
    except ValueError as e:
        return jsonify({"error": "invalid", "detail": str(e)}), 400
    settings_store.put(f"default:{tool}", value)
    return jsonify({"default": value})


def _parse_hour(raw) -> int:
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Pick an hour for every time.")
    if not 0 <= hour <= 23:
        raise ValueError("Hours must be between 0 and 23.")
    return hour


def _parse_settings_form(form) -> dict:
    days = cascade_scheduler.schedule_settings()["days"]
    if form.get("cascade_schedule_mode") == "days":
        schedule = {d: _parse_hour(form.get(f"day_hour_{d}")) for d in days if form.get(f"day_{d}") == "on"}
        if not schedule:
            raise ValueError("Pick at least one day, or choose every day.")
    else:
        schedule = {}
    return {
        "cascade_auto_enabled": form.get("cascade_auto_enabled") == "on",
        "cascade_auto_hour": _parse_hour(form.get("cascade_auto_hour")),
        "cascade_auto_schedule": schedule,
        "snapshot_hour": _parse_hour(form.get("snapshot_hour")),
    }


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    error = None
    if request.method == "POST":
        try:
            new_settings = _parse_settings_form(request.form)
        except ValueError as e:
            error = str(e)
        else:
            for key, value in new_settings.items():
                settings_store.put(key, value)
            return redirect(url_for("settings_page", saved=1))
    return render_template(
        "settings.html",
        cascade=cascade_scheduler.schedule_settings(),
        snapshot_hour=snapshot_scheduler.configured_hour(),
        has_cascade_default=bool(_stored_default("cascade")),
        error=error,
        saved=request.args.get("saved") == "1" and error is None,
    )


# --- Liked Songs Sync -------------------------------------------------


@app.route("/liked-songs-sync")
def liked_songs_sync():
    if not _credentials_configured():
        return render_template("liked_songs_sync.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "liked_songs_sync.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("liked_songs_sync", sp),
    )


@app.route("/liked-songs-sync/diff")
def diff():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    playlist_id_1 = request.args.get("playlist_id_1")
    playlist_id_2 = request.args.get("playlist_id_2")
    if not playlist_id_1 or not playlist_id_2:
        return redirect(url_for("liked_songs_sync"))

    _sync_job.start(_run_sync_diff(playlist_id_1, playlist_id_2))

    return render_template(
        "progress.html",
        status_url=url_for("diff_status"),
        cancel_url=url_for("diff_cancel"),
        result_url=url_for("diff_result"),
        back_url=url_for("liked_songs_sync"),
        heading="Refreshing…",
        description="Reading your playlists and Liked Songs. This can take a few minutes for large libraries.",
    )


@app.route("/liked-songs-sync/diff/status")
def diff_status():
    return jsonify(_sync_job.status())


@app.route("/liked-songs-sync/diff/cancel", methods=["POST"])
def diff_cancel():
    _sync_job.cancel()
    return jsonify({"ok": True})


@app.route("/liked-songs-sync/diff/result")
def diff_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _sync_job.result is None:
        return redirect(url_for("diff"))
    to_add, to_remove = _sync_job.result

    if len(to_add) + len(to_remove) > NAME_LOOKUP_THRESHOLD:
        return render_template(
            "diff.html",
            add_count=len(to_add),
            remove_count=len(to_remove),
            names_loaded=False,
        )

    return render_template(
        "diff.html",
        add_count=len(to_add),
        remove_count=len(to_remove),
        add_labels=_track_labels(sp, to_add),
        remove_labels=_track_labels(sp, to_remove),
        names_loaded=True,
    )


@app.route("/liked-songs-sync/diff/names")
def diff_names():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _sync_job.result is None:
        return redirect(url_for("diff"))
    to_add, to_remove = _sync_job.result

    return render_template(
        "diff.html",
        add_count=len(to_add),
        remove_count=len(to_remove),
        add_labels=_track_labels(sp, to_add),
        remove_labels=_track_labels(sp, to_remove),
        names_loaded=True,
    )


@app.route("/liked-songs-sync/apply", methods=["POST"])
def apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    to_add, to_remove = _sync_job.result or (set(), set())
    apply_diff(sp, to_add, to_remove)
    _sync_job.result = None

    return render_template("result.html", added=len(to_add), removed=len(to_remove))


# --- Duplicate Finder ---------------------------------------------------


@app.route("/duplicates")
def duplicates_picker():
    if not _credentials_configured():
        return render_template("duplicates.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "duplicates.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("duplicates", sp),
        not_duplicates=not_duplicates_module.list_active() if sp is not None else [],
    )


@app.route("/duplicates/unmark", methods=["POST"])
def duplicates_unmark():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    # key as a form field rather than in the path - it's made of Spotify
    # uris, which contain colons.
    key = request.form.get("key")
    if key:
        not_duplicates_module.unmark(key)
    return redirect(url_for("duplicates_picker"))


@app.route("/duplicates/scan")
def duplicates_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    playlist_ids = request.args.getlist("playlist_id")
    if len(playlist_ids) < 2:
        return redirect(url_for("duplicates_picker"))

    _dup_job.start(_run_duplicate_scan(playlist_ids))

    return render_template(
        "progress.html",
        status_url=url_for("duplicates_scan_status"),
        cancel_url=url_for("duplicates_scan_cancel"),
        result_url=url_for("duplicates_scan_result"),
        back_url=url_for("duplicates_picker"),
        heading="Scanning for duplicates…",
        description="Reading your playlists to find tracks that appear in more than one. This can take a few minutes for large libraries.",
    )


@app.route("/duplicates/scan/status")
def duplicates_scan_status():
    return jsonify(_dup_job.status())


@app.route("/duplicates/scan/cancel", methods=["POST"])
def duplicates_scan_cancel():
    _dup_job.cancel()
    return jsonify({"ok": True})


@app.route("/duplicates/scan/result")
def duplicates_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _dup_job.result is None:
        return redirect(url_for("duplicates_picker"))

    return render_template("duplicates_result.html", duplicates=_dup_job.result)


@app.route("/duplicates/remove", methods=["POST"])
def duplicates_remove():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    removals = []
    for item in request.form.getlist("remove"):
        playlist_id, _, uri = item.partition("::")
        if playlist_id and uri:
            removals.append({"playlist_id": playlist_id, "uri": uri})

    history_id = duplicates_module.remove_from_playlists(sp, removals)
    marked = not_duplicates_module.mark(
        duplicates_module.not_duplicates_from_form(_dup_job.result or [], request.form)
    )
    _dup_job.result = None

    return render_template(
        "duplicates_removed.html",
        removed_count=len(removals),
        history_id=history_id,
        marked_count=marked,
    )


# --- Playlist Filter -----------------------------------------------------


@app.route("/playlist-filter")
def playlist_filter_picker():
    if not _credentials_configured():
        return render_template("playlist_filter.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "playlist_filter.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("playlist_filter", sp),
        field_operators=playlist_filter_module.FIELD_OPERATORS,
    )


@app.route("/playlist-filter/scan")
def playlist_filter_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    playlist_ids = request.args.getlist("playlist_id")
    fields = request.args.getlist("field")
    operators = request.args.getlist("operator")
    values = request.args.getlist("value")
    value2s = request.args.getlist("value2")
    destination_mode = request.args.get("destination_mode")
    destination_playlist_id = request.args.get("destination_playlist_id")
    destination_playlist_name = request.args.get("destination_playlist_name")
    destination_name = request.args.get("destination_name")

    if (
        not playlist_ids
        or not fields
        or len(fields) != len(operators)
        or len(fields) != len(values)
        or len(fields) != len(value2s)
        or any(not v for v in values)
    ):
        return redirect(url_for("playlist_filter_picker"))
    if destination_mode == "existing" and not destination_playlist_id:
        return redirect(url_for("playlist_filter_picker"))
    if destination_mode == "new" and not destination_name:
        return redirect(url_for("playlist_filter_picker"))

    criteria = [
        {"field": f, "operator": o, "value": v, "value2": v2 or None}
        for f, o, v, v2 in zip(fields, operators, values, value2s)
    ]

    destination = {
        "destination_mode": destination_mode,
        "destination_playlist_id": destination_playlist_id,
        "destination_playlist_name": destination_playlist_name,
        "destination_name": destination_name,
    }
    _filter_job.start(
        _run_playlist_filter_scan(playlist_ids, criteria, destination)
    )

    return render_template(
        "progress.html",
        status_url=url_for("playlist_filter_scan_status"),
        cancel_url=url_for("playlist_filter_scan_cancel"),
        result_url=url_for("playlist_filter_scan_result"),
        back_url=url_for("playlist_filter_picker"),
        heading="Scanning playlists…",
        description="Reading your playlists to find matching tracks. This can take a few minutes for large libraries.",
    )


@app.route("/playlist-filter/scan/status")
def playlist_filter_scan_status():
    return jsonify(_filter_job.status())


@app.route("/playlist-filter/scan/cancel", methods=["POST"])
def playlist_filter_scan_cancel():
    _filter_job.cancel()
    return jsonify({"ok": True})


@app.route("/playlist-filter/scan/result")
def playlist_filter_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _filter_job.result
    if result is None:
        return redirect(url_for("playlist_filter_picker"))

    destination_label = (
        result["destination_name"]
        if result["destination_mode"] == "new"
        else result["destination_playlist_name"]
    )

    return render_template(
        "playlist_filter_result.html",
        matches=result["matches"],
        destination_label=destination_label,
        destination_mode=result["destination_mode"],
        already_in_destination=result["already_in_destination"],
        similar_versions=result.get("similar_versions", {}),
    )


@app.route("/playlist-filter/apply", methods=["POST"])
def playlist_filter_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _filter_job.result
    if result is None:
        return redirect(url_for("playlist_filter_picker"))

    match_uris = {m["uri"] for m in result["matches"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in match_uris]

    if not selected_uris:
        # "Continue without adding" - don't create an empty new playlist.
        added, skipped, local_skipped = 0, 0, 0
        playlist_name = (
            result["destination_name"]
            if result["destination_mode"] == "new"
            else result["destination_playlist_name"]
        )
    else:
        if result["destination_mode"] == "new":
            playlist_id = playlist_filter_module.create_playlist(sp, result["destination_name"])
            playlist_name = result["destination_name"]
        else:
            playlist_id = result["destination_playlist_id"]
            playlist_name = result["destination_playlist_name"]

        added, skipped, local_skipped = playlist_filter_module.add_tracks_to_playlist(
            sp, playlist_id, selected_uris
        )
    _filter_job.result = None

    return render_template(
        "playlist_filter_done.html",
        added=added,
        skipped=skipped,
        local_skipped=local_skipped,
        playlist_name=playlist_name,
    )


# --- Playlist Cleanup ---------------------------------------------------


@app.route("/playlist-cleanup")
def playlist_cleanup_picker():
    if not _credentials_configured():
        return render_template("playlist_cleanup.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "playlist_cleanup.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("playlist_cleanup", sp),
        field_operators=playlist_cleanup_module.FIELD_OPERATORS,
    )


@app.route("/playlist-cleanup/scan")
def playlist_cleanup_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    playlist_id = request.args.get("playlist_id")
    field = request.args.get("field")
    operator = request.args.get("operator")
    value = request.args.get("value")
    value2 = request.args.get("value2")

    if not playlist_id or not field or not operator or not value:
        return redirect(url_for("playlist_cleanup_picker"))

    _cleanup_job.start(_run_playlist_cleanup_scan(playlist_id, field, operator, value, value2))

    return render_template(
        "progress.html",
        status_url=url_for("playlist_cleanup_scan_status"),
        cancel_url=url_for("playlist_cleanup_scan_cancel"),
        result_url=url_for("playlist_cleanup_scan_result"),
        back_url=url_for("playlist_cleanup_picker"),
        heading="Scanning playlist…",
        description="Reading the playlist to find tracks to remove. This can take a few minutes for large playlists.",
    )


@app.route("/playlist-cleanup/scan/status")
def playlist_cleanup_scan_status():
    return jsonify(_cleanup_job.status())


@app.route("/playlist-cleanup/scan/cancel", methods=["POST"])
def playlist_cleanup_scan_cancel():
    _cleanup_job.cancel()
    return jsonify({"ok": True})


@app.route("/playlist-cleanup/scan/result")
def playlist_cleanup_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _cleanup_job.result
    if result is None:
        return redirect(url_for("playlist_cleanup_picker"))

    return render_template(
        "playlist_cleanup_result.html",
        playlist_name=result["playlist_name"],
        removals=result["removals"],
    )


@app.route("/playlist-cleanup/remove", methods=["POST"])
def playlist_cleanup_remove():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _cleanup_job.result
    if result is None:
        return redirect(url_for("playlist_cleanup_picker"))

    removal_uris = {r["uri"] for r in result["removals"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in removal_uris]

    history_id = playlist_cleanup_module.remove_tracks(sp, result["playlist_id"], selected_uris)
    playlist_name = result["playlist_name"]
    _cleanup_job.result = None

    return render_template(
        "playlist_cleanup_done.html",
        removed=len(selected_uris),
        playlist_name=playlist_name,
        history_id=history_id,
    )


# --- Removal History -------------------------------------------------------
# Every removal Duplicate Finder / Playlist Cleanup (standalone or in a
# Cascade) makes is logged, so tracks can be put back later - usually a
# missing song is only noticed days afterwards in Spotify itself.


@app.route("/removal-history")
def removal_history():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    return render_template(
        "removal_history.html",
        entries=removal_history_module.load_history(),
        max_entries=removal_history_module.MAX_ENTRIES,
    )


@app.route("/removal-history/<entry_id>/restore", methods=["POST"])
def removal_history_restore(entry_id: str):
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    # "all" is the done screen's Undo shortcut; otherwise only the ticked
    # tracks from the history page.
    keys = None if request.form.get("all") else set(request.form.getlist("track"))
    if keys is not None and not keys:
        return redirect(url_for("removal_history"))

    summary = removal_history_module.restore(sp, entry_id, keys)
    return render_template("removal_history_restored.html", summary=summary)


# --- Snapshots -------------------------------------------------------------
# Daily copies of every playlist you own plus Liked Songs (see
# core/snapshot_scheduler.py), to see what's changed since and put tracks -
# or a whole playlist - back. One background job runs the read-only
# compare/"Snapshot now" work and the restore/rollback writes; its result is
# tagged with "kind" so each result page only picks up its own.


def _run_take_snapshots():
    def target(cancel_check):
        sp = get_authenticated_client()
        return {"kind": "run", **snapshots_module.take_snapshots(sp, cancel_check)}

    return target


def _run_snapshot_compare(playlist_id: str, snapshot_id: str):
    def target(cancel_check):
        sp = get_authenticated_client()
        return {"kind": "compare", **snapshots_module.compare(sp, playlist_id, snapshot_id, cancel_check)}

    return target


def _run_snapshot_restore(playlist_id: str, snapshot_id: str, indices: set[int]):
    def target(cancel_check):
        sp = get_authenticated_client()
        return {"kind": "restore", **snapshots_module.restore(sp, playlist_id, snapshot_id, indices)}

    return target


def _run_snapshot_rollback(playlist_id: str, snapshot_id: str, signature: str):
    def target(cancel_check):
        sp = get_authenticated_client()
        return {"kind": "rollback", **snapshots_module.rollback(sp, playlist_id, snapshot_id, signature)}

    return target


def _snapshot_job_result(kind: str, playlist_id: str | None = None, snapshot_id: str | None = None):
    result = _snapshot_job.result
    if not result or result.get("kind") != kind:
        return None
    if playlist_id is not None and result.get("playlist_id") != playlist_id:
        return None
    if snapshot_id is not None and result["snapshot"]["id"] != snapshot_id:
        return None
    return result


def _snapshot_progress(heading: str, description: str, result_url: str):
    return render_template(
        "progress.html",
        status_url=url_for("snapshots_job_status"),
        cancel_url=url_for("snapshots_job_cancel"),
        result_url=result_url,
        back_url=url_for("snapshots_page"),
        heading=heading,
        description=description,
    )


@app.route("/snapshots")
def snapshots_page():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    return render_template(
        "snapshots.html",
        playlists=snapshots_module.list_playlists(),
        last_run=snapshot_scheduler.read_last_run(),
        hour=snapshot_scheduler.configured_hour(),
        retention_days=snapshots_module.RETENTION_DAYS,
    )


@app.route("/snapshots/job/status")
def snapshots_job_status():
    return jsonify(_snapshot_job.status())


@app.route("/snapshots/job/cancel", methods=["POST"])
def snapshots_job_cancel():
    _snapshot_job.cancel()
    return jsonify({"ok": True})


@app.route("/snapshots/run", methods=["POST"])
def snapshots_run():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    _snapshot_job.start(_run_take_snapshots())
    return _snapshot_progress(
        "Taking snapshots…",
        "Checking every playlist you own plus Liked Songs, and saving a snapshot of any that changed.",
        url_for("snapshots_run_result"),
    )


@app.route("/snapshots/run/result")
def snapshots_run_result():
    result = _snapshot_job_result("run")
    if result is None:
        return redirect(url_for("snapshots_page"))
    return render_template("snapshots_run_done.html", result=result)


@app.route("/snapshots/<playlist_id>/<snapshot_id>")
def snapshot_compare(playlist_id: str, snapshot_id: str):
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if snapshots_module.load_snapshot(playlist_id, snapshot_id) is None:
        return redirect(url_for("snapshots_page"))

    _snapshot_job.start(_run_snapshot_compare(playlist_id, snapshot_id))
    return _snapshot_progress(
        "Comparing with now…",
        "Reading the playlist as it is now to see what's changed since the snapshot.",
        url_for("snapshot_compare_result", playlist_id=playlist_id, snapshot_id=snapshot_id),
    )


@app.route("/snapshots/<playlist_id>/<snapshot_id>/compare")
def snapshot_compare_result(playlist_id: str, snapshot_id: str):
    result = _snapshot_job_result("compare", playlist_id, snapshot_id)
    if result is None:
        return redirect(url_for("snapshot_compare", playlist_id=playlist_id, snapshot_id=snapshot_id))

    return render_template(
        "snapshot_compare.html",
        result=result,
        liked=playlist_id == snapshots_module.LIKED_SONGS_ID,
        same_order=[t["uri"] for t in result["snapshot"]["tracks"]]
        == [t["uri"] for t in result["current_tracks"]],
    )


@app.route("/snapshots/<playlist_id>/<snapshot_id>/restore", methods=["POST"])
def snapshot_restore(playlist_id: str, snapshot_id: str):
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    indices = {int(i) for i in request.form.getlist("track") if i.isdigit()}
    if not indices or snapshots_module.load_snapshot(playlist_id, snapshot_id) is None:
        return redirect(url_for("snapshot_compare_result", playlist_id=playlist_id, snapshot_id=snapshot_id))

    _snapshot_job.start(_run_snapshot_restore(playlist_id, snapshot_id, indices))
    return _snapshot_progress(
        "Restoring tracks…",
        "Checking which of the selected tracks are still missing and adding them back.",
        url_for("snapshot_applied"),
    )


@app.route("/snapshots/<playlist_id>/<snapshot_id>/rollback")
def snapshot_rollback_confirm(playlist_id: str, snapshot_id: str):
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    # The plan is worked out from the comparison just shown, so there's no
    # second read of the playlist here; applying it re-reads and checks
    # nothing's changed in between.
    result = _snapshot_job_result("compare", playlist_id, snapshot_id)
    if result is None:
        return redirect(url_for("snapshot_compare", playlist_id=playlist_id, snapshot_id=snapshot_id))

    plan = snapshots_module.plan_rollback(
        playlist_id, result["snapshot"]["tracks"], result["current_tracks"]
    )
    return render_template(
        "snapshot_rollback.html",
        result=result,
        plan=plan,
        liked=playlist_id == snapshots_module.LIKED_SONGS_ID,
    )


@app.route("/snapshots/<playlist_id>/<snapshot_id>/rollback", methods=["POST"])
def snapshot_rollback(playlist_id: str, snapshot_id: str):
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    signature = request.form.get("signature", "")
    if not signature or snapshots_module.load_snapshot(playlist_id, snapshot_id) is None:
        return redirect(url_for("snapshots_page"))

    _snapshot_job.start(_run_snapshot_rollback(playlist_id, snapshot_id, signature))
    return _snapshot_progress(
        "Rolling back…",
        "Saving a snapshot of the playlist as it is now, then rolling it back. Don't close this page.",
        url_for("snapshot_applied"),
    )


@app.route("/snapshots/applied")
def snapshot_applied():
    result = _snapshot_job_result("restore") or _snapshot_job_result("rollback")
    if result is None:
        return redirect(url_for("snapshots_page"))
    return render_template(
        "snapshot_applied.html",
        result=result,
        liked=result["playlist_id"] == snapshots_module.LIKED_SONGS_ID,
    )


# --- Playlist Diff -------------------------------------------------------


@app.route("/playlist-diff")
def playlist_diff_picker():
    if not _credentials_configured():
        return render_template("playlist_diff.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "playlist_diff.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("playlist_diff", sp),
        snoozes=diff_snoozes_module.list_active() if sp is not None else [],
    )


@app.route("/playlist-diff/unsnooze", methods=["POST"])
def playlist_diff_unsnooze():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    # uri as a form field rather than in the path - Spotify uris contain colons.
    uri = request.form.get("uri")
    if uri:
        diff_snoozes_module.unsnooze(uri)
    return redirect(url_for("playlist_diff_picker"))


@app.route("/playlist-diff/scan")
def playlist_diff_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    source_ids = request.args.getlist("source_id")
    target_ids = request.args.getlist("target_id")
    if not source_ids or not target_ids:
        return redirect(url_for("playlist_diff_picker"))

    _diff_job.start(_run_playlist_diff_scan(source_ids, target_ids))

    return render_template(
        "progress.html",
        status_url=url_for("playlist_diff_scan_status"),
        cancel_url=url_for("playlist_diff_scan_cancel"),
        result_url=url_for("playlist_diff_scan_result"),
        back_url=url_for("playlist_diff_picker"),
        heading="Scanning playlists…",
        description="Reading your playlists to find missing tracks. This can take a few minutes for large libraries.",
    )


@app.route("/playlist-diff/scan/status")
def playlist_diff_scan_status():
    return jsonify(_diff_job.status())


@app.route("/playlist-diff/scan/cancel", methods=["POST"])
def playlist_diff_scan_cancel():
    _diff_job.cancel()
    return jsonify({"ok": True})


@app.route("/playlist-diff/scan/result")
def playlist_diff_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _diff_job.result
    if result is None:
        return redirect(url_for("playlist_diff_picker"))

    return render_template(
        "playlist_diff_result.html",
        missing=result["missing"],
        targets=result["targets"],
    )


@app.route("/playlist-diff/add", methods=["POST"])
def playlist_diff_add():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _diff_job.result
    if result is None:
        return redirect(url_for("playlist_diff_picker"))

    missing_uris = {t["uri"] for t in result["missing"]}
    target_ids = {t["id"] for t in result["targets"]}

    additions = []
    for item in request.form.getlist("add"):
        uri, _, playlist_id = item.partition("::")
        if uri in missing_uris and playlist_id in target_ids:
            additions.append({"playlist_id": playlist_id, "uri": uri})

    added_counts = playlist_diff_module.add_to_playlists(sp, additions)
    snoozed = diff_snoozes_module.snooze(playlist_diff_module.snoozes_from_form(result, request.form))
    targets_by_id = {t["id"]: t["name"] for t in result["targets"]}
    added_summary = [
        {"name": targets_by_id[pid], "added": counts["added"], "local_skipped": counts["local_skipped"]}
        for pid, counts in added_counts.items()
    ]
    _diff_job.result = None

    return render_template(
        "playlist_diff_done.html",
        added_summary=added_summary,
        snoozed=snoozed,
        snooze_days=diff_snoozes_module.SNOOZE_DAYS,
    )


# --- Playlist Search ------------------------------------------------------
# Standalone search: matches criteria across one or more playlists,
# optionally excluding tracks already in other playlists (even a different
# version of the same song). Intentionally not a Cascade step and doesn't
# share picker/apply state with Playlist Filter, though it reuses its
# generic create/add-tracks helpers same as Playlist Diff does.


@app.route("/playlist-search")
def playlist_search_picker():
    if not _credentials_configured():
        return render_template("playlist_search.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "playlist_search.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("playlist_search", sp),
        field_operators=playlist_search_module.FIELD_OPERATORS,
    )


@app.route("/playlist-search/scan")
def playlist_search_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    playlist_ids = request.args.getlist("playlist_id")
    fields = request.args.getlist("field")
    operators = request.args.getlist("operator")
    values = request.args.getlist("value")
    value2s = request.args.getlist("value2")
    exclude_playlist_ids = request.args.getlist("exclude_playlist_id")

    if (
        not playlist_ids
        or not fields
        or len(fields) != len(operators)
        or len(fields) != len(values)
        or len(fields) != len(value2s)
        or any(not v for v in values)
    ):
        return redirect(url_for("playlist_search_picker"))

    criteria = [
        {"field": f, "operator": o, "value": v, "value2": v2 or None}
        for f, o, v, v2 in zip(fields, operators, values, value2s)
    ]

    _search_job.start(
        _run_playlist_search_scan(playlist_ids, criteria, exclude_playlist_ids)
    )

    return render_template(
        "progress.html",
        status_url=url_for("playlist_search_scan_status"),
        cancel_url=url_for("playlist_search_scan_cancel"),
        result_url=url_for("playlist_search_scan_result"),
        back_url=url_for("playlist_search_picker"),
        heading="Searching playlists…",
        description="Reading your playlists to find matching tracks. This can take a few minutes for large libraries.",
    )


@app.route("/playlist-search/scan/status")
def playlist_search_scan_status():
    return jsonify(_search_job.status())


@app.route("/playlist-search/scan/cancel", methods=["POST"])
def playlist_search_scan_cancel():
    _search_job.cancel()
    return jsonify({"ok": True})


@app.route("/playlist-search/scan/result")
def playlist_search_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _search_job.result
    if result is None:
        return redirect(url_for("playlist_search_picker"))

    return render_template("playlist_search_result.html", results=result["results"])


@app.route("/playlist-search/apply", methods=["POST"])
def playlist_search_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _search_job.result
    if result is None:
        return redirect(url_for("playlist_search_picker"))

    result_uris = {t["uri"] for t in result["results"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in result_uris]
    if not selected_uris:
        return redirect(url_for("playlist_search_scan_result"))

    destination_mode = request.form.get("destination_mode")
    if destination_mode == "new":
        destination_name = request.form.get("destination_name", "").strip()
        if not destination_name:
            return redirect(url_for("playlist_search_scan_result"))
        playlist_id = playlist_filter_module.create_playlist(sp, destination_name)
        playlist_name = destination_name
    else:
        playlist_id = request.form.get("destination_playlist_id")
        playlist_name = request.form.get("destination_playlist_name")
        if not playlist_id:
            return redirect(url_for("playlist_search_scan_result"))

    added, skipped, local_skipped = playlist_filter_module.add_tracks_to_playlist(
        sp, playlist_id, selected_uris
    )
    _search_job.result = None

    return render_template(
        "playlist_search_done.html",
        added=added,
        skipped=skipped,
        local_skipped=local_skipped,
        playlist_name=playlist_name,
    )


# --- Album Swap -------------------------------------------------------------
# Once a single already sitting on some playlists gets a full album release,
# find every playlist track that's still the single version and swap it for
# the matching album version, in the same position it occupied.


@app.route("/album-swap")
def album_swap_picker():
    if not _credentials_configured():
        return render_template("album_swap.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "album_swap.html",
        needs_credentials=False,
        needs_lastfm=not _lastfm_configured(),
        logged_in=sp is not None,
        default_prefs=_default_preferences("album_swap", sp),
    )


@app.route("/api/search-albums")
def api_search_albums():
    sp = get_authenticated_client()
    if sp is None:
        return jsonify({"error": "not_authenticated"}), 401

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    results = sp.search(q=query, type="album", limit=10)
    albums = []
    for album in results["albums"]["items"]:
        # Spotify's type=album search also returns singles/EPs - only full
        # albums are relevant here, since the whole point is finding the
        # album a single graduated into.
        if album.get("album_type") != "album":
            continue
        images = album.get("images") or []
        albums.append(
            {
                "id": album["id"],
                "name": album["name"],
                "artists": ", ".join(a["name"] for a in album.get("artists") or []),
                "image_url": images[0]["url"] if images else None,
            }
        )

    return jsonify(albums)


@app.route("/api/recent-albums")
def api_recent_albums():
    if not _lastfm_configured():
        return jsonify([])

    sp = get_authenticated_client()
    if sp is None:
        return jsonify({"error": "not_authenticated"}), 401

    return jsonify(most_played_module.find_recent_released_albums(sp))


@app.route("/api/album-tracks")
def api_album_tracks():
    sp = get_authenticated_client()
    if sp is None:
        return jsonify({"error": "not_authenticated"}), 401

    album_id = request.args.get("album_id", "").strip()
    if not album_id:
        return jsonify({"error": "missing_album_id"}), 400

    return jsonify(playlist_swap_module.get_album_tracks(sp, album_id))


@app.route("/album-swap/scan")
def album_swap_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    album_id = request.args.get("album_id", "").strip()
    playlist_ids = request.args.getlist("playlist_id")
    if not album_id or not playlist_ids:
        return redirect(url_for("album_swap_picker"))

    _swap_job.start(_run_album_swap_scan(album_id, playlist_ids))

    return render_template(
        "progress.html",
        status_url=url_for("album_swap_scan_status"),
        cancel_url=url_for("album_swap_scan_cancel"),
        result_url=url_for("album_swap_scan_result"),
        back_url=url_for("album_swap_picker"),
        heading="Scanning playlists…",
        description="Reading your playlists to find single versions to swap for the album version. This can take a few minutes for large libraries.",
    )


@app.route("/album-swap/scan/status")
def album_swap_scan_status():
    return jsonify(_swap_job.status())


@app.route("/album-swap/scan/cancel", methods=["POST"])
def album_swap_scan_cancel():
    _swap_job.cancel()
    return jsonify({"ok": True})


@app.route("/album-swap/scan/result")
def album_swap_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _swap_job.result
    if result is None:
        return redirect(url_for("album_swap_picker"))

    return render_template(
        "album_swap_result.html",
        album=result["album"],
        swaps=result["swaps"],
    )


@app.route("/album-swap/apply", methods=["POST"])
def album_swap_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _swap_job.result
    if result is None:
        return redirect(url_for("album_swap_picker"))

    swaps_by_id = {s["id"]: s for s in result["swaps"]}
    selected = [
        swaps_by_id[sid] for sid in request.form.getlist("swap_id") if sid in swaps_by_id
    ]
    if not selected:
        return redirect(url_for("album_swap_scan_result"))

    summary = playlist_swap_module.apply_swaps(sp, selected)
    _swap_job.result = None

    return render_template("album_swap_done.html", summary=list(summary.values()))


# --- Most Played -----------------------------------------------------------
# Your most-scrobbled tracks, artists, or albums from Last.fm, enriched with
# Spotify metadata (release date, genre, popularity) for filtering. Artists
# and Albums mode are read-only browsing; only Tracks mode can add results
# to a playlist.


@app.route("/most-played")
def most_played_picker():
    if not _credentials_configured():
        return render_template("most_played.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "most_played.html",
        needs_credentials=False,
        needs_lastfm=not _lastfm_configured(),
        logged_in=sp is not None,
        field_operators=most_played_module.FIELD_OPERATORS,
        periods=lastfm_client_module.PERIODS,
    )


@app.route("/most-played/scan")
def most_played_scan():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    entity_type = request.args.get("entity_type")
    period = request.args.get("period", "overall")
    count = request.args.get("count", type=int)
    fields = request.args.getlist("field")
    operators = request.args.getlist("operator")
    values = request.args.getlist("value")
    value2s = request.args.getlist("value2")

    if (
        entity_type not in most_played_module.FIELD_OPERATORS
        or period not in lastfm_client_module.PERIODS
        or not count
        or len(fields) != len(operators)
        or len(fields) != len(values)
        or len(fields) != len(value2s)
        or any(not v for v in values)
    ):
        return redirect(url_for("most_played_picker"))

    count = max(1, min(count, 200))
    criteria = [
        {"field": f, "operator": o, "value": v, "value2": v2 or None}
        for f, o, v, v2 in zip(fields, operators, values, value2s)
    ]

    _most_played_job.start(_run_most_played_scan(entity_type, period, count, criteria))

    return render_template(
        "progress.html",
        status_url=url_for("most_played_scan_status"),
        cancel_url=url_for("most_played_scan_cancel"),
        result_url=url_for("most_played_scan_result"),
        back_url=url_for("most_played_picker"),
        heading="Looking up your most played…",
        description="Fetching your top scrobbles from Last.fm, then resolving each one on Spotify for filtering. If you've added conditions, it keeps paging further into your scrobble history until enough results match (or gives up after a while). Filtering by First Played or Last Played also builds your full listening history index the first time you use either, which can take several minutes on its own - both only happen once, since everything's cached for next time.",
    )


@app.route("/most-played/scan/status")
def most_played_scan_status():
    return jsonify(_most_played_job.status())


@app.route("/most-played/scan/cancel", methods=["POST"])
def most_played_scan_cancel():
    _most_played_job.cancel()
    return jsonify({"ok": True})


@app.route("/most-played/scan/result")
def most_played_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _most_played_job.result
    if result is None:
        return redirect(url_for("most_played_picker"))

    return render_template(
        "most_played_result.html",
        entity_type=result["entity_type"],
        results=result["results"],
        unresolved_count=result["unresolved_count"],
        count=result["count"],
        scanned_count=result["scanned_count"],
        truncated=result["truncated"],
    )


@app.route("/most-played/apply", methods=["POST"])
def most_played_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _most_played_job.result
    if result is None or result["entity_type"] != "track":
        return redirect(url_for("most_played_picker"))

    result_uris = {t["uri"] for t in result["results"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in result_uris]
    if not selected_uris:
        return redirect(url_for("most_played_scan_result"))

    destination_mode = request.form.get("destination_mode")
    if destination_mode == "new":
        destination_name = request.form.get("destination_name", "").strip()
        if not destination_name:
            return redirect(url_for("most_played_scan_result"))
        playlist_id = playlist_filter_module.create_playlist(sp, destination_name)
        playlist_name = destination_name
    else:
        playlist_id = request.form.get("destination_playlist_id")
        playlist_name = request.form.get("destination_playlist_name")
        if not playlist_id:
            return redirect(url_for("most_played_scan_result"))

    added, skipped, local_skipped = playlist_filter_module.add_tracks_to_playlist(
        sp, playlist_id, selected_uris
    )
    _most_played_job.result = None

    return render_template(
        "most_played_done.html",
        added=added,
        skipped=skipped,
        local_skipped=local_skipped,
        playlist_name=playlist_name,
    )


# --- Forgotten Favourites --------------------------------------------------
# Tracks with a lot of all-time scrobbles that haven't been played in a
# while, so they can be rediscovered. Separate from Most Played so its form
# is just the two questions that matter here; manual only, not a Cascade
# step.


@app.route("/forgotten-favourites")
def forgotten_favourites_picker():
    if not _credentials_configured():
        return render_template("forgotten_favourites.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "forgotten_favourites.html",
        needs_credentials=False,
        needs_lastfm=not _lastfm_configured(),
        logged_in=sp is not None,
    )


@app.route("/forgotten-favourites/scan")
def forgotten_favourites_scan():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    min_plays = request.args.get("min_plays", type=int)
    months = request.args.get("months", type=int)
    if not min_plays or not months or min_plays < 1 or months < 1:
        return redirect(url_for("forgotten_favourites_picker"))

    _forgotten_job.start(_run_forgotten_favourites_scan(min_plays, months))

    return render_template(
        "progress.html",
        status_url=url_for("forgotten_favourites_scan_status"),
        cancel_url=url_for("forgotten_favourites_scan_cancel"),
        result_url=url_for("forgotten_favourites_scan_result"),
        back_url=url_for("forgotten_favourites_picker"),
        heading="Looking for forgotten favourites…",
        description="Updating your Last.fm listening history, finding tracks you've played a lot but not recently, resolving each one on Spotify, then checking your playlists and Liked Songs for ones you already have. The first run builds your full listening history index, which can take several minutes - after that it's cached and only tops up what's new.",
    )


@app.route("/forgotten-favourites/scan/status")
def forgotten_favourites_scan_status():
    return jsonify(_forgotten_job.status())


@app.route("/forgotten-favourites/scan/cancel", methods=["POST"])
def forgotten_favourites_scan_cancel():
    _forgotten_job.cancel()
    return jsonify({"ok": True})


@app.route("/forgotten-favourites/scan/result")
def forgotten_favourites_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _forgotten_job.result
    if result is None:
        return redirect(url_for("forgotten_favourites_picker"))

    return render_template(
        "forgotten_favourites_result.html",
        results=result["results"],
        unmatched=result["unmatched"],
        no_history_count=result["no_history_count"],
        truncated=result["truncated"],
        max_results=forgotten_favourites_module.MAX_RESULTS,
        min_plays=result["min_plays"],
        months=result["months"],
    )


@app.route("/forgotten-favourites/apply", methods=["POST"])
def forgotten_favourites_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _forgotten_job.result
    if result is None:
        return redirect(url_for("forgotten_favourites_picker"))

    result_uris = {t["uri"] for t in result["results"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in result_uris]
    if not selected_uris:
        return redirect(url_for("forgotten_favourites_scan_result"))

    destination_mode = request.form.get("destination_mode")
    if destination_mode == "new":
        destination_name = request.form.get("destination_name", "").strip()
        if not destination_name:
            return redirect(url_for("forgotten_favourites_scan_result"))
        playlist_id = playlist_filter_module.create_playlist(sp, destination_name)
        playlist_name = destination_name
    else:
        playlist_id = request.form.get("destination_playlist_id")
        playlist_name = request.form.get("destination_playlist_name")
        if not playlist_id:
            return redirect(url_for("forgotten_favourites_scan_result"))

    added, skipped, local_skipped = playlist_filter_module.add_tracks_to_playlist(
        sp, playlist_id, selected_uris
    )
    _forgotten_job.result = None

    return render_template(
        "forgotten_favourites_done.html",
        added=added,
        skipped=skipped,
        local_skipped=local_skipped,
        playlist_name=playlist_name,
    )


# --- Ranking Check -------------------------------------------------------
# Finds songs whose ranking playlist (best to worst, e.g. "one" to "four")
# doesn't match the yearly playlist's order or how much they're played.
# A move between ranking playlists is a button per song, and the page comes
# back to the results afterwards so several can be worked through in one
# go; reordering the yearly playlist is left to the user. Manual only, not
# a Cascade step.


@app.route("/ranking-check")
def ranking_check_picker():
    if not _credentials_configured():
        return render_template("ranking_check.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "ranking_check.html",
        needs_credentials=False,
        needs_lastfm=not _lastfm_configured(),
        logged_in=sp is not None,
        default_prefs=_default_preferences("ranking_check", sp),
        periods=ranking_check_module.PERIODS,
        default_period=ranking_check_module.DEFAULT_PERIOD,
        dismissals=ranking_dismissals_module.list_active() if sp is not None else [],
        dismiss_months=ranking_dismissals_module.DISMISS_MONTHS,
    )


@app.route("/ranking-check/scan")
def ranking_check_scan():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    tier_ids = request.args.getlist("tier_id")
    year_id = request.args.get("year_id")
    period = request.args.get("period", ranking_check_module.DEFAULT_PERIOD)
    if len(tier_ids) < 2 or not all(tier_ids) or not year_id:
        return redirect(url_for("ranking_check_picker"))

    _ranking_job.start(
        _run_ranking_check_scan(tier_ids, year_id, period, request.args.get("only_added") == "1")
    )

    return render_template(
        "progress.html",
        status_url=url_for("ranking_check_scan_status"),
        cancel_url=url_for("ranking_check_scan_cancel"),
        result_url=url_for("ranking_check_scan_result"),
        back_url=url_for("ranking_check_picker"),
        heading="Checking your rankings…",
        description="Updating your Last.fm listening history, reading your ranking playlists and yearly playlist, then comparing them with each other and with how much you've played each song. The first run builds your full listening history index, which can take several minutes - after that it's cached and only tops up what's new.",
    )


@app.route("/ranking-check/scan/status")
def ranking_check_scan_status():
    return jsonify(_ranking_job.status())


@app.route("/ranking-check/scan/cancel", methods=["POST"])
def ranking_check_scan_cancel():
    _ranking_job.cancel()
    return jsonify({"ok": True})


@app.route("/ranking-check/scan/result")
def ranking_check_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _ranking_job.result
    if result is None:
        return redirect(url_for("ranking_check_picker"))

    return render_template(
        "ranking_check_result.html",
        max_results=ranking_check_module.MAX_RESULTS,
        unranked_days=ranking_check_module.UNRANKED_TAIL_DAYS,
        demotion_min_days=ranking_check_module.DEMOTION_MIN_DAYS,
        **result,
    )


def _ranking_check_entry(key: str | None):
    """The result entry for key from the last scan, or None."""
    result = _ranking_job.result
    if result is None or not key:
        return None
    return next((r for r in result["results"] if r["key"] == key), None)


def _ranking_check_back(entry: dict | None):
    url = url_for("ranking_check_scan_result")
    if entry is not None:
        url += f"#song-{_ranking_job.result['results'].index(entry) + 1}"
    return redirect(url)


@app.route("/ranking-check/move", methods=["POST"])
def ranking_check_move():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    entry = _ranking_check_entry(request.form.get("key"))
    tier = request.form.get("tier", type=int)
    move = next((m for m in entry["moves"] if m["tier"] == tier), None) if entry else None
    if entry is None or entry["status"] is not None or move is None:
        return _ranking_check_back(entry)

    tiers = _ranking_job.result["tiers"]
    history_id = ranking_check_module.move_song(sp, entry, tiers, tier)
    verb = move["label"].split(" ", 1)[0]
    done = {"Add": "Added to", "Keep": "Now only in", "Move": "Moved to"}[verb]
    entry["status"] = {"kind": "moved", "text": f"{done} {tiers[tier]['name']}", "history_id": history_id}
    return _ranking_check_back(entry)


@app.route("/ranking-check/dismiss", methods=["POST"])
def ranking_check_dismiss():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    entry = _ranking_check_entry(request.form.get("key"))
    if entry is None or entry["status"] is not None:
        return _ranking_check_back(entry)

    until = ranking_dismissals_module.dismiss(entry)
    entry["status"] = {"kind": "dismissed", "until": until}
    return _ranking_check_back(entry)


@app.route("/ranking-check/restore", methods=["POST"])
def ranking_check_restore():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    key = request.form.get("key")
    if key:
        ranking_dismissals_module.restore([key])

    # Undo from the results page goes back there; a restore from the
    # dismissed list on the picker stays on the picker.
    entry = _ranking_check_entry(key)
    if request.form.get("from") == "result" and entry is not None:
        if entry["status"] and entry["status"]["kind"] == "dismissed":
            entry["status"] = None
        return _ranking_check_back(entry)
    return redirect(url_for("ranking_check_picker"))


# --- Listening Visuals ----------------------------------------------------
# Read-only, artistic views of Last.fm listening history, drawn from the
# local scrobble store. One page with a switcher between visualisations.
# The chosen visualisation and range are remembered in the browser, and
# days/times are worked out there too (in its local time zone - the server
# runs in UTC), so the bare page just hands off to the browser, which
# redirects to the last-used visualisation's prepare route with UTC
# timestamps. Manual only, not a Cascade step.

LISTENING_VISUALS = [
    {
        "key": "shape",
        "label": "Listening Shape",
        "prepare_endpoint": "listening_shape_prepare",
        "default_range": "30",
    },
    {
        "key": "river",
        "label": "Artist river",
        "prepare_endpoint": "artist_river_prepare",
        "default_range": "12m",
    },
    {
        "key": "month",
        "label": "Colour of your month",
        "prepare_endpoint": "colour_month_prepare",
        "default_range": "month",
    },
]


def _listening_visuals_template(**context):
    visuals = [
        {
            "key": v["key"],
            "label": v["label"],
            "prepare_url": url_for(v["prepare_endpoint"]),
            "default_range": v["default_range"],
        }
        for v in LISTENING_VISUALS
    ]
    return render_template("listening_visuals.html", visuals=visuals, **context)


@app.route("/listening-visuals")
def listening_visuals():
    if not _credentials_configured():
        return _listening_visuals_template(needs_credentials=True, view=None)

    sp = get_authenticated_client()
    return _listening_visuals_template(
        needs_credentials=False,
        needs_lastfm=not _lastfm_configured(),
        logged_in=sp is not None,
        view=None,
    )


def _visual_settings(default_range: str) -> dict:
    """The range a prepare route was asked for, as the browser described
    it - handed back to the page so its controls show what's displayed."""
    return {
        "range": request.args.get("range", default_range),
        "start": request.args.get("start", ""),
        "end": request.args.get("end", ""),
        "month": request.args.get("month", ""),
    }


@app.route("/listening-visuals/shape/prepare")
def listening_shape_prepare():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("listening_visuals"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    from_ts = request.args.get("from_ts", type=int)
    to_ts = request.args.get("to_ts", type=int)
    # A day of slack for daylight saving changes, since the browser picks
    # the range in local days.
    max_span = (listening_shape_module.MAX_DAYS + 1) * 24 * 60 * 60
    if from_ts is None or to_ts is None or not 0 < to_ts - from_ts <= max_span:
        return redirect(url_for("listening_visuals"))
    _visuals_job.start(_run_listening_shape(from_ts, to_ts, _visual_settings("30")))

    return render_template(
        "progress.html",
        status_url=url_for("listening_visuals_status"),
        cancel_url=url_for("listening_visuals_cancel"),
        result_url=url_for("listening_shape"),
        back_url=url_for("home"),
        heading="Drawing your listening shape…",
        description="Updating your Last.fm listening history, then working out the colour of each album's art. The first time, this downloads your full listening history, which takes about 20 minutes - after that it only fetches what's new.",
    )


@app.route("/listening-visuals/river/prepare")
def artist_river_prepare():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("listening_visuals"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    # No maximum span - the river can show all time (from_ts 0), which is
    # bucketed by month in the browser past two years.
    from_ts = request.args.get("from_ts", type=int)
    to_ts = request.args.get("to_ts", type=int)
    if from_ts is None or to_ts is None or from_ts < 0 or to_ts <= from_ts:
        return redirect(url_for("listening_visuals"))

    _visuals_job.start(_run_artist_river(from_ts, to_ts, _visual_settings("12m")))

    return render_template(
        "progress.html",
        status_url=url_for("listening_visuals_status"),
        cancel_url=url_for("listening_visuals_cancel"),
        result_url=url_for("artist_river"),
        back_url=url_for("home"),
        heading="Drawing your artist river…",
        description="Updating your Last.fm listening history. The first time, this downloads your full listening history, which takes about 20 minutes - after that it only fetches what's new.",
    )


@app.route("/listening-visuals/river")
def artist_river():
    result = _visuals_job.result
    if not _credentials_configured() or result is None or result.get("view") != "river":
        return redirect(url_for("listening_visuals"))

    return _listening_visuals_template(
        needs_credentials=False,
        needs_lastfm=False,
        logged_in=True,
        view="river",
        river=result,
    )


@app.route("/listening-visuals/month/prepare")
def colour_month_prepare():
    if not _credentials_configured() or not _lastfm_configured():
        return redirect(url_for("listening_visuals"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    from_ts = request.args.get("from_ts", type=int)
    to_ts = request.args.get("to_ts", type=int)
    # One calendar month, plus a day of slack for daylight saving changes.
    if from_ts is None or to_ts is None or not 0 < to_ts - from_ts <= 32 * 24 * 60 * 60:
        return redirect(url_for("listening_visuals"))

    _visuals_job.start(_run_colour_month(from_ts, to_ts, _visual_settings("month")))

    return render_template(
        "progress.html",
        status_url=url_for("listening_visuals_status"),
        cancel_url=url_for("listening_visuals_cancel"),
        result_url=url_for("colour_month"),
        back_url=url_for("home"),
        heading="Mixing the colour of your month…",
        description="Updating your Last.fm listening history, then working out the colour of each album's art. The first time, this downloads your full listening history, which takes about 20 minutes - after that it only fetches what's new.",
    )


@app.route("/listening-visuals/month")
def colour_month():
    result = _visuals_job.result
    if not _credentials_configured() or result is None or result.get("view") != "month":
        return redirect(url_for("listening_visuals"))

    return _listening_visuals_template(
        needs_credentials=False,
        needs_lastfm=False,
        logged_in=True,
        view="month",
        month=result,
    )


@app.route("/listening-visuals/status")
def listening_visuals_status():
    return jsonify(_visuals_job.status())


@app.route("/listening-visuals/cancel", methods=["POST"])
def listening_visuals_cancel():
    _visuals_job.cancel()
    return jsonify({"ok": True})


@app.route("/listening-visuals/shape")
def listening_shape():
    result = _visuals_job.result
    if not _credentials_configured() or result is None or result.get("view") != "shape":
        return redirect(url_for("listening_visuals"))

    return _listening_visuals_template(
        needs_credentials=False,
        needs_lastfm=False,
        logged_in=True,
        view="shape",
        shape=result,
        max_days=listening_shape_module.MAX_DAYS,
    )


# --- Playlist Prepend -----------------------------------------------------


@app.route("/playlist-prepend")
def playlist_prepend_picker():
    if not _credentials_configured():
        return render_template("playlist_prepend.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "playlist_prepend.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_prefs=_default_preferences("playlist_prepend", sp),
    )


@app.route("/playlist-prepend/scan")
def playlist_prepend_scan():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    source_id = request.args.get("source_id")
    destination_id = request.args.get("destination_id")
    if not source_id or not destination_id or source_id == destination_id:
        return redirect(url_for("playlist_prepend_picker"))

    _prepend_job.start(_run_playlist_prepend_scan(source_id, destination_id))

    return render_template(
        "progress.html",
        status_url=url_for("playlist_prepend_scan_status"),
        cancel_url=url_for("playlist_prepend_scan_cancel"),
        result_url=url_for("playlist_prepend_scan_result"),
        back_url=url_for("playlist_prepend_picker"),
        heading="Scanning playlists…",
        description="Reading both playlists to see which tracks would be new. This can take a few minutes for large libraries.",
    )


@app.route("/playlist-prepend/scan/status")
def playlist_prepend_scan_status():
    return jsonify(_prepend_job.status())


@app.route("/playlist-prepend/scan/cancel", methods=["POST"])
def playlist_prepend_scan_cancel():
    _prepend_job.cancel()
    return jsonify({"ok": True})


@app.route("/playlist-prepend/scan/result")
def playlist_prepend_scan_result():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _prepend_job.result
    if result is None:
        return redirect(url_for("playlist_prepend_picker"))

    return render_template(
        "playlist_prepend_result.html",
        to_add=result["to_add"],
        duplicates=result["duplicates"],
        local_skipped=result["local_skipped"],
        source_name=result["source_name"],
        destination_name=result["destination_name"],
    )


@app.route("/playlist-prepend/add", methods=["POST"])
def playlist_prepend_add():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    result = _prepend_job.result
    if result is None:
        return redirect(url_for("playlist_prepend_picker"))

    to_add_by_uri = {t["uri"]: t for t in result["to_add"]}
    selected_uris = [uri for uri in request.form.getlist("track") if uri in to_add_by_uri]
    track_details = [to_add_by_uri[uri] for uri in selected_uris]

    added = playlist_prepend_module.prepend_tracks_to_playlist(
        sp, result["destination_id"], selected_uris, track_details
    )
    destination_name = result["destination_name"]
    _prepend_job.result = None

    return render_template(
        "playlist_prepend_done.html", added=added, destination_name=destination_name
    )


# --- Cascade ---------------------------------------------------------


@app.route("/cascade")
def cascade_picker():
    if not _credentials_configured():
        return render_template("cascade.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "cascade.html",
        needs_credentials=False,
        logged_in=sp is not None,
        default_cascade=_default_cascade_steps(sp),
        default_cascade_types=_default_preferences("cascade", sp),
        field_operators=playlist_filter_module.FIELD_OPERATORS,
    )


def _start_cascade_run(steps: list[dict]):
    global _cascade_run
    _cascade_run = cascade_module.CascadeRun(steps)
    _cascade_job.start(_run_cascade_prefetch(_cascade_run))

    return render_template(
        "progress.html",
        status_url=url_for("cascade_scan_status"),
        cancel_url=url_for("cascade_scan_cancel"),
        result_url=url_for("cascade_step"),
        back_url=url_for("cascade_picker"),
        heading="Fetching playlists…",
        description="Reading each playlist your cascade needs once, no matter how many steps use it. This can take a few minutes for large libraries.",
    )


@app.route("/cascade/start", methods=["POST"])
def cascade_start():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    try:
        steps = json.loads(request.form.get("steps", "[]"))
    except (TypeError, ValueError):
        steps = []
    if not steps:
        return redirect(url_for("cascade_picker"))

    return _start_cascade_run(steps)


@app.route("/cascade/start-default", methods=["POST"])
def cascade_start_default():
    """Runs the saved default Cascade as-is, from the page's read-only view.
    Converted to run-ready steps here, the same way the daily auto-scan
    does, so the page doesn't have to build each step's form first."""
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    steps = cascade_scheduler.canonicalize_default_cascade(_default_cascade_steps(sp) or [])
    if not steps:
        return redirect(url_for("cascade_picker"))

    return _start_cascade_run(steps)


@app.route("/cascade/resume-auto")
def cascade_resume_auto():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    pending = cascade_scheduler.load_pending_review()
    if not pending or not pending.get("steps"):
        return redirect(url_for("cascade_picker"))

    return _start_cascade_run(pending["steps"])


@app.route("/cascade/auto/run-now", methods=["POST"])
def cascade_auto_run_now():
    """Debug helper: fires the daily auto-scan immediately instead of
    waiting for the scheduled hour. Same scan-only code path as the real
    scheduled job - lets the feature be verified without waiting a day."""
    if not _credentials_configured():
        return redirect(url_for("home"))

    cascade_scheduler.run_daily_scan(_default_cascade_steps)
    return redirect(url_for("home"))


@app.route("/cascade/scan/status")
def cascade_scan_status():
    return jsonify(_cascade_job.status())


@app.route("/cascade/scan/cancel", methods=["POST"])
def cascade_scan_cancel():
    _cascade_job.cancel()
    return jsonify({"ok": True})


@app.route("/cascade/step")
def cascade_step():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _cascade_run is None:
        return redirect(url_for("cascade_picker"))
    if _cascade_run.is_done:
        return redirect(url_for("cascade_done"))

    template_name, context = _cascade_step_view(sp, _cascade_run)
    return render_template(template_name, **context)


@app.route("/cascade/step/apply", methods=["POST"])
def cascade_step_apply():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _cascade_run is None:
        return redirect(url_for("cascade_picker"))

    _cascade_run.apply_current(sp, request.form)

    if _cascade_run.is_done:
        return redirect(url_for("cascade_done"))
    return redirect(url_for("cascade_step"))


@app.route("/cascade/done")
def cascade_done():
    if not _credentials_configured():
        return redirect(url_for("home"))

    sp = get_authenticated_client()
    if sp is None:
        return redirect(url_for("login"))

    if _cascade_run is None:
        return redirect(url_for("cascade_picker"))

    cascade_scheduler.clear_pending_review()
    return render_template("cascade_done.html", summaries=_cascade_run.summaries)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8888, debug=True, threaded=True)
