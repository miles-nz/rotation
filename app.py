from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from spotipy import Spotify

from core.background_job import BackgroundJob
from spotify.spotify_client import make_oauth, get_authenticated_client
from core.sync import apply_diff, get_target_diff
import playlists.cascade as cascade_module
import playlists.duplicates as duplicates_module
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter_module
import playlists.playlist_cleanup as playlist_cleanup_module
import playlists.playlist_diff as playlist_diff_module
import playlists.playlist_prepend as playlist_prepend_module

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("app")

# Above this many combined add/remove tracks, skip fetching display names
# up front (that's one extra API request per 50 tracks) and just show
# counts; names can be loaded on demand via /diff/names.
NAME_LOOKUP_THRESHOLD = 200

# Hardcoded per-tool defaults, set via env vars so the actual playlist
# IDs/names never land in this public repo. Only applied for
# SPOTIFY_OWNER_ID so anyone else deploying this app sees a blank picker.
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


DEFAULT_PREFERENCES = {
    "liked_songs_sync": _parse_json_env("DEFAULT_LIKED_SONGS_SYNC"),
    "duplicates": _parse_json_env("DEFAULT_DUPLICATES"),
    "playlist_filter": _parse_json_env("DEFAULT_PLAYLIST_FILTER"),
    "playlist_cleanup": _parse_json_env("DEFAULT_PLAYLIST_CLEANUP"),
    "playlist_diff": _parse_json_env("DEFAULT_PLAYLIST_DIFF"),
    "playlist_prepend": _parse_json_env("DEFAULT_PLAYLIST_PREPEND"),
    "cascade": _parse_json_env("DEFAULT_CASCADE"),
}


def _default_preferences(tool: str, sp):
    if sp is None:
        return None
    if SPOTIFY_OWNER_ID and sp.current_user().get("id") != SPOTIFY_OWNER_ID:
        return None
    return DEFAULT_PREFERENCES.get(tool)


# Maps a cascade step type to the DEFAULT_PREFERENCES key holding that same
# tool's own default config, so DEFAULT_CASCADE only has to name which steps
# to pre-populate and in what order - not repeat their settings.
_CASCADE_STEP_PREF_KEYS = {
    "duplicates": "duplicates",
    "playlist_filter": "playlist_filter",
    "playlist_cleanup": "playlist_cleanup",
    "playlist_diff": "playlist_diff",
    "sync": "liked_songs_sync",
}


def _default_cascade_steps(sp):
    """DEFAULT_CASCADE is just an ordered list of step types, e.g.
    ["playlist_diff", "duplicates"]. Each step's actual config comes from
    that tool's own DEFAULT_* env var, so there's one source of truth per
    playlist selection."""
    step_types = _default_preferences("cascade", sp)
    if not step_types:
        return None

    # Ownership was already checked above, so it's safe to read the other
    # tools' defaults straight from DEFAULT_PREFERENCES from here on.
    steps = []
    for step_type in step_types:
        pref_key = _CASCADE_STEP_PREF_KEYS.get(step_type)
        base = DEFAULT_PREFERENCES.get(pref_key) if pref_key else None
        if not base:
            continue
        if step_type == "duplicates":
            steps.append({"type": step_type, "playlists": base})
        else:
            steps.append({"type": step_type, **base})
    return steps or None


app = Flask(__name__)

_sync_job = BackgroundJob(["core.sync", "app"])
_dup_job = BackgroundJob(["playlists.duplicates", "app"])
_filter_job = BackgroundJob(["playlists.playlist_filter", "app"])
_cleanup_job = BackgroundJob(["playlists.playlist_cleanup", "app"])
_diff_job = BackgroundJob(["playlists.playlist_diff", "app"])
_prepend_job = BackgroundJob(["playlists.playlist_prepend", "app"])
_cascade_job = BackgroundJob(["playlists.playlist_cache", "app"])
_cascade_run: cascade_module.CascadeRun | None = None


def _credentials_configured() -> bool:
    return bool(
        os.environ.get("SPOTIFY_CLIENT_ID")
        and os.environ.get("SPOTIFY_CLIENT_SECRET")
        and os.environ.get("SPOTIFY_REDIRECT_URI")
    )


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


def _run_playlist_prepend_scan(source_id: str, destination_id: str):
    def target(cancel_check):
        sp = get_authenticated_client()
        return playlist_prepend_module.find_prependable(
            sp, source_id, destination_id, cancel_check=cancel_check
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


@app.route("/")
def home():
    if not _credentials_configured():
        return render_template("home.html", needs_credentials=True)

    sp = get_authenticated_client()
    return render_template(
        "home.html",
        needs_credentials=False,
        logged_in=sp is not None,
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
    oauth = make_oauth(open_browser=False)
    return redirect(oauth.get_authorize_url())


@app.route("/callback")
def callback():
    oauth = make_oauth(open_browser=False)
    code = request.args.get("code")
    oauth.get_access_token(code, as_dict=False)
    return redirect(url_for("home"))


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
    )


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

    duplicates_module.remove_from_playlists(sp, removals)
    _dup_job.result = None

    return render_template("duplicates_removed.html", removed_count=len(removals))


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

    if result["destination_mode"] == "new":
        playlist_id = playlist_filter_module.create_playlist(sp, result["destination_name"])
        playlist_name = result["destination_name"]
    else:
        playlist_id = result["destination_playlist_id"]
        playlist_name = result["destination_playlist_name"]

    added, skipped = playlist_filter_module.add_tracks_to_playlist(sp, playlist_id, selected_uris)
    _filter_job.result = None

    return render_template(
        "playlist_filter_done.html", added=added, skipped=skipped, playlist_name=playlist_name
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

    playlist_cleanup_module.remove_tracks(sp, result["playlist_id"], selected_uris)
    playlist_name = result["playlist_name"]
    _cleanup_job.result = None

    return render_template(
        "playlist_cleanup_done.html", removed=len(selected_uris), playlist_name=playlist_name
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
    )


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
    targets_by_id = {t["id"]: t["name"] for t in result["targets"]}
    added_summary = [
        {"name": targets_by_id[pid], "added": count}
        for pid, count in added_counts.items()
    ]
    _diff_job.result = None

    return render_template("playlist_diff_done.html", added_summary=added_summary)


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
        field_operators=playlist_filter_module.FIELD_OPERATORS,
    )


@app.route("/cascade/start", methods=["POST"])
def cascade_start():
    global _cascade_run

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

    return render_template("cascade_done.html", summaries=_cascade_run.summaries)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8888, debug=True, threaded=True)
