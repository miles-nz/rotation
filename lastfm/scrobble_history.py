from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR
import lastfm.lastfm_client as lastfm_client_module

logger = logging.getLogger(__name__)

# Every scrobble, in SQLite next to the resolve cache (lastfm/resolve_cache.py).
# Unlike that cache this isn't a memoized lookup that can be rebuilt on
# demand - the full history walk takes ~20 minutes - so the schema is
# versioned and migrated in place (see _MIGRATIONS) rather than ever being
# thrown away and re-fetched. Last.fm's API terms cap stored Last.fm data
# at 100 MB across everything in .lastfm_cache/; this file is ~12 MB for
# ~140k scrobbles.
_CACHE_DIR = DATA_DIR / ".lastfm_cache"
_PATH = _CACHE_DIR / "scrobbles.sqlite3"

# JSON indexes this store replaced: scrobble_dates.json (first/last play per
# track, everything else thrown away) and first_scrobbled.json (its
# predecessor, already unused). Neither can be migrated, since neither has
# every scrobble, so they're just deleted once the store exists.
_LEGACY_FILES = ("scrobble_dates.json", "first_scrobbled.json")

# One request per page, paced with this delay between requests, to stay
# comfortably under Last.fm's commonly-observed ~5 req/s per-IP limit
# (their public ToS reserves the right to set limits without publishing an
# exact number - see the plan/commit for the sources behind this figure).
_REQUEST_DELAY_SECONDS = 0.3
_PAGE_LIMIT = 200

# A top-up re-fetches this far back past the last sync, because scrobbles
# can arrive late with their original timestamp (e.g. plays made offline
# and submitted once back online). About a page of overlap at most, and
# already-stored scrobbles are ignored on insert.
_TOP_UP_OVERLAP_SECONDS = 24 * 60 * 60

# Numbered schema migrations: _MIGRATIONS[n] upgrades a database at
# user_version n to n + 1. Only ever append - never edit one that has
# shipped, since a database that already ran it won't run it again. Stick
# to SQLite 3.28 features (local dev's version): no RETURNING, STRICT
# tables or DROP COLUMN.
#
# Rows are identified by names exactly as Last.fm returned them; norm_key is
# derived (initially the lowercase artist||name key) and can be recalculated
# from the stored names if the matching rule changes. scrobbles' (timestamp,
# track_id) primary key makes re-fetching a scrobble harmless and serves
# timestamp range queries; WITHOUT ROWID stores the table as that one
# index instead of a table plus a copy of it. There's no per-track index
# yet - nothing needs one (load() scans everything) and a migration can add
# it when something does.
_MIGRATIONS: list[list[str]] = [
    [
        """CREATE TABLE artists (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            mbid TEXT
        )""",
        """CREATE TABLE albums (
            id INTEGER PRIMARY KEY,
            artist_id INTEGER NOT NULL REFERENCES artists (id),
            name TEXT NOT NULL,
            mbid TEXT,
            image_url TEXT,
            UNIQUE (artist_id, name)
        )""",
        """CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            artist_id INTEGER NOT NULL REFERENCES artists (id),
            name TEXT NOT NULL,
            mbid TEXT,
            norm_key TEXT NOT NULL,
            UNIQUE (artist_id, name)
        )""",
        "CREATE INDEX tracks_norm_key ON tracks (norm_key)",
        """CREATE TABLE scrobbles (
            timestamp INTEGER NOT NULL,
            track_id INTEGER NOT NULL REFERENCES tracks (id),
            album_id INTEGER REFERENCES albums (id),
            PRIMARY KEY (timestamp, track_id)
        ) WITHOUT ROWID""",
        "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value)",
    ],
]

# Background jobs run in threads of a single process, so a thread lock is
# enough to keep two tools from walking the history at the same time.
_sync_lock = threading.Lock()
_migrate_lock = threading.Lock()
_migrated = False


def _key(artist: str, name: str) -> str:
    return f"{artist.strip().lower()}||{name.strip().lower()}"


def _connect() -> sqlite3.Connection:
    """A new connection for the calling thread (sqlite3 connections can't
    be shared across threads), with the schema brought up to date.
    Autocommit mode - writes go through _transaction()."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_PATH), timeout=30, isolation_level=None)
    # WAL lets pages read while a background sync is writing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_migrated(conn)
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _ensure_migrated(conn: sqlite3.Connection) -> None:
    """Applies any pending migrations, once per process. Each migration and
    its version bump commit together, and the version is re-read inside
    the write lock, so a crash or a second process can't apply one twice."""
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        while True:
            with _transaction(conn):
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version >= len(_MIGRATIONS):
                    break
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version + 1}")
            logger.info("scrobble history: migrated store to schema version %d", version + 1)
        for name in _LEGACY_FILES:
            path = _CACHE_DIR / name
            if path.exists():
                path.unlink()
                logger.info("scrobble history: removed superseded %s", name)
        _migrated = True


def _get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", (key, value))


def _upsert_id(conn: sqlite3.Connection, insert_sql: str, select_sql: str, params: tuple, key: tuple) -> int:
    conn.execute(insert_sql, params)
    return conn.execute(select_sql, key).fetchone()[0]


def _insert_scrobbles(conn: sqlite3.Connection, scrobbles: list[dict]) -> None:
    """Stores a page of get_recent_tracks results. Must run inside a
    transaction. Artist/track/album rows are created on first sight; a
    MusicBrainz ID or image URL missing from earlier scrobbles is filled in
    when a later one has it, but never overwritten."""
    for s in scrobbles:
        artist_id = _upsert_id(
            conn,
            "INSERT INTO artists (name, mbid) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET mbid = COALESCE(artists.mbid, excluded.mbid)",
            "SELECT id FROM artists WHERE name = ?",
            (s["artist"], s["artist_mbid"]),
            (s["artist"],),
        )
        track_id = _upsert_id(
            conn,
            "INSERT INTO tracks (artist_id, name, mbid, norm_key) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (artist_id, name) DO UPDATE SET mbid = COALESCE(tracks.mbid, excluded.mbid)",
            "SELECT id FROM tracks WHERE artist_id = ? AND name = ?",
            (artist_id, s["name"], s["mbid"], _key(s["artist"], s["name"])),
            (artist_id, s["name"]),
        )
        album_id = None
        if s["album"]:
            album_id = _upsert_id(
                conn,
                "INSERT INTO albums (artist_id, name, mbid, image_url) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (artist_id, name) DO UPDATE SET "
                "mbid = COALESCE(albums.mbid, excluded.mbid), "
                "image_url = COALESCE(albums.image_url, excluded.image_url)",
                "SELECT id FROM albums WHERE artist_id = ? AND name = ?",
                (artist_id, s["album"], s["album_mbid"], s["image_url"]),
                (artist_id, s["album"]),
            )
        conn.execute(
            "INSERT OR IGNORE INTO scrobbles (timestamp, track_id, album_id) VALUES (?, ?, ?)",
            (s["timestamp"], track_id, album_id),
        )


def load() -> dict:
    """Every track's {"first", "last"} unix timestamps, keyed by normalised
    artist||name, from what's already stored - no Last.fm requests. Empty
    if nothing has been synced yet. Pass the result to lookup()."""
    if not _PATH.exists():
        return {}
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT t.norm_key, MIN(s.timestamp), MAX(s.timestamp) "
            "FROM scrobbles s JOIN tracks t ON t.id = s.track_id "
            "GROUP BY t.norm_key"
        ).fetchall()
    return {key: {"first": first, "last": last} for key, first, last in rows}


def scrobbles_between(from_ts: int, to_ts: int) -> list[dict]:
    """Every stored scrobble in [from_ts, to_ts), oldest first, as
    [{"timestamp", "artist", "name", "album", "image_url"}, ...]. "album"
    is "" and "image_url" None when the scrobble has no album; "image_url"
    is also None when Last.fm has no art for the album. Reads what's
    already stored - call sync() first for an up-to-date range."""
    if not _PATH.exists():
        return []
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT s.timestamp, ar.name, t.name, al.name, al.image_url "
            "FROM scrobbles s "
            "JOIN tracks t ON t.id = s.track_id "
            "JOIN artists ar ON ar.id = t.artist_id "
            "LEFT JOIN albums al ON al.id = s.album_id "
            "WHERE s.timestamp >= ? AND s.timestamp < ? "
            "ORDER BY s.timestamp",
            (from_ts, to_ts),
        ).fetchall()
    return [
        {"timestamp": ts, "artist": artist, "name": name, "album": album or "", "image_url": image_url}
        for ts, artist, name, album, image_url in rows
    ]


def lookup(data: dict, artist: str, name: str) -> dict:
    """Looks up a track's {"first", "last"} unix timestamps in an
    already-loaded index (see load()). Empty dict if never scrobbled or
    not yet stored (i.e. sync() hasn't covered it - most likely nobody
    has ever called sync() at all) - callers should use .get("first") /
    .get("last"), which are then naturally None."""
    return data.get(_key(artist, name), {})


def sync(cancel_check: CancelCheck | None = None) -> dict:
    """Brings the store up to date with Last.fm, then returns load().

    The first call (or any call before it has fully finished) backfills
    the full history, newest first, with a fixed "to" bound set when the
    backfill started - so scrobbles arriving during the ~20-minute walk
    can't shift page numbers, and an interrupted backfill (crash, cancel,
    or an error surviving retries) resumes from the next unstored page.
    Each page and the resume point commit together.

    Once the backfill is complete, each call only tops up: scrobbles since
    the last sync, plus a day of overlap for late-submitted ones.

    Only one sync runs at a time; a caller arriving while another is
    running waits for it (still honouring cancellation), then does its own
    top-up, which by then is usually a single request."""
    _acquire_sync_lock(cancel_check)
    try:
        with closing(_connect()) as conn:
            if not _get_state(conn, "backfill_complete"):
                _backfill(conn, cancel_check)
            _top_up(conn, cancel_check)
    finally:
        _sync_lock.release()
    return load()


def resync_range(from_ts: int, to_ts: int, cancel_check: CancelCheck | None = None) -> int:
    """Replaces the stored scrobbles in [from_ts, to_ts) with what Last.fm
    has for that range now, to pick up scrobbles edited or deleted on
    Last.fm without re-walking the rest of the history. Fetches the whole
    range before changing anything, so a cancellation or error leaves the
    store untouched. Returns the number of scrobbles now stored for the
    range."""
    _acquire_sync_lock(cancel_check)
    try:
        fetched: list[dict] = []
        _walk(
            from_ts,
            to_ts,
            start_page=1,
            on_page=lambda page, scrobbles: fetched.extend(scrobbles),
            cancel_check=cancel_check,
        )
        with closing(_connect()) as conn:
            with _transaction(conn):
                conn.execute(
                    "DELETE FROM scrobbles WHERE timestamp >= ? AND timestamp < ?", (from_ts, to_ts)
                )
                _insert_scrobbles(conn, fetched)
            return conn.execute(
                "SELECT COUNT(*) FROM scrobbles WHERE timestamp >= ? AND timestamp < ?", (from_ts, to_ts)
            ).fetchone()[0]
    finally:
        _sync_lock.release()


def _acquire_sync_lock(cancel_check: CancelCheck | None) -> None:
    if _sync_lock.acquire(blocking=False):
        return
    logger.info("scrobble history: waiting for another sync to finish")
    while not _sync_lock.acquire(timeout=1):
        check_cancelled(cancel_check)


def _backfill(conn: sqlite3.Connection, cancel_check: CancelCheck | None) -> None:
    to_ts = _get_state(conn, "backfill_to")
    if to_ts is None:
        # +1 because "to" is exclusive, so a scrobble from this second is
        # included. Also where the first top-up after the backfill starts.
        to_ts = int(time.time()) + 1
        with _transaction(conn):
            _set_state(conn, "backfill_to", to_ts)
            _set_state(conn, "synced_through", to_ts)
            _set_state(conn, "resume_page", 1)

    def store_page(page: int, scrobbles: list[dict]) -> None:
        with _transaction(conn):
            _insert_scrobbles(conn, scrobbles)
            _set_state(conn, "resume_page", page + 1)

    _walk(None, to_ts, _get_state(conn, "resume_page", 1), store_page, cancel_check)
    with _transaction(conn):
        _set_state(conn, "backfill_complete", 1)


def _top_up(conn: sqlite3.Connection, cancel_check: CancelCheck | None) -> None:
    from_ts = _get_state(conn, "synced_through") - _TOP_UP_OVERLAP_SECONDS
    to_ts = int(time.time()) + 1

    def store_page(page: int, scrobbles: list[dict]) -> None:
        with _transaction(conn):
            _insert_scrobbles(conn, scrobbles)

    _walk(from_ts, to_ts, 1, store_page, cancel_check)
    # Only advanced once every page is stored - an interrupted top-up just
    # redoes the same range next time.
    with _transaction(conn):
        _set_state(conn, "synced_through", to_ts)


def _walk(from_ts, to_ts, start_page: int, on_page, cancel_check: CancelCheck | None) -> None:
    """Fetches every page of [from_ts, to_ts) from start_page on, calling
    on_page(page, scrobbles) for each before moving to the next. Stops at
    the last page, an empty page, or a cancellation."""
    page = start_page
    while True:
        scrobbles, meta = _get_page_with_backoff(page, from_ts, to_ts, cancel_check)
        if not scrobbles:
            break
        on_page(page, scrobbles)

        # Two separate log calls, deliberately: BackgroundJob's handler
        # treats a "progress" record as a bar update OR a scrolling log
        # line, never both (see core/background_job.py) - without this
        # second, untagged call, the log pane shows nothing but "Starting…"
        # for the whole multi-minute walk, even though the bar is moving.
        logger.info("scrobble history: page %d/%d", page, meta["total_pages"])
        logger.info(
            "scrobble history progress: page %d/%d",
            page,
            meta["total_pages"],
            extra={
                "progress": {
                    "id": "history_sync",
                    "name": "Building listening history index",
                    "current": page,
                    "total": meta["total_pages"],
                }
            },
        )
        check_cancelled(cancel_check)

        if page >= meta["total_pages"]:
            break
        page += 1
        time.sleep(_REQUEST_DELAY_SECONDS)


def _get_page_with_backoff(page: int, from_ts, to_ts, cancel_check: CancelCheck | None):
    return lastfm_client_module.call_with_retry(
        lambda: lastfm_client_module.get_recent_tracks(
            limit=_PAGE_LIMIT, page=page, from_ts=from_ts, to_ts=to_ts
        ),
        description=f"scrobble history: page {page}",
        cancel_check=cancel_check,
    )
