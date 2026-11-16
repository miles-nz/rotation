from __future__ import annotations

import calendar
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.find_more_hidden as find_more_hidden
import lastfm.forgotten_favourites as forgotten_favourites
import lastfm.lastfm_client as lastfm_client
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history
import listenbrainz.listenbrainz_client as listenbrainz_client
import playlists.playlist_cache as playlist_cache
import playlists.playlist_filter as playlist_filter

logger = logging.getLogger(__name__)

# Find More Like This: recent music by artists similar to the ones in a
# playlist. Similarity comes from Last.fm and ListenBrainz (what people
# listen to together), never from Spotify's own recommendations - those
# endpoints are gone for this app anyway (Related Artists and
# Recommendations both 404), and the point is to get away from Spotify's
# ranking.

MODES = ("tracks", "albums", "artists")
DEFAULT_YEARS = 3

# A big playlist can have hundreds of artists. The ones with the most
# tracks in it say the most about what it sounds like, and every seed costs
# two Last.fm calls and a ListenBrainz call the first time it's seen.
MAX_SEEDS = 100
SIMILAR_PER_SEED = 50

# With "more from artists you know" on, artists you know are mostly the
# strongest matches (they're why the playlist sounds like it does), so they
# could take every place. At most this share of the results goes to them.
MAX_KNOWN_SHARE = 0.5

# Candidates are checked best-first (each check is a few Spotify and
# Last.fm calls), stopping once this many qualify or this many have been
# checked, whichever comes first - plenty of strong matches have nothing
# out recently, so the second limit keeps a strict recency window from
# walking the whole candidate list.
TARGET_ARTISTS = 40
MAX_CANDIDATES_CHECKED = 250

# Artists you've scrobbled this many times or more count as already known,
# even if they're in none of your playlists - once or twice is just
# something you've come across. Songs too, for "more from artists you know":
# one you've played this often counts as heard.
SCROBBLES_TO_EXCLUDE = 3

TRACKS_PER_ARTIST = 3
SAMPLE_TRACKS_PER_ARTIST = 3
# Only the newest few releases in the window are looked through for tracks.
RELEASES_PER_ARTIST = 5
# A Spotify "single" with this many tracks or more is shown as an EP.
MIN_EP_TRACKS = 4

SIMILAR_TTL_DAYS = 30
INFO_TTL_DAYS = 30
RELEASES_TTL_DAYS = 3

_LASTFM_WORKERS = 4
_LISTENBRAINZ_WORKERS = 3
_CANDIDATE_WORKERS = 6
# Give up on ListenBrainz for the rest of a run after this many failures in
# a row, rather than waiting out every retry for every seed while it's down.
_LISTENBRAINZ_MAX_FAILURES = 3

_PROFILE_TAGS = 15
# Tags that say how someone files an artist rather than what it sounds like.
_IGNORED_TAGS = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "albums i own",
    "my favorites", "love", "awesome", "beautiful", "under 2000 listeners",
}

SOURCE_LABELS = {"lastfm": "Last.fm", "listenbrainz": "ListenBrainz"}

# A release or track that's an old recording put out again rather than new
# music: live archive albums, remasters, deluxe/anniversary editions and so
# on. Matched in a bracketed or " - " suffix ("Tush - Live 1980",
# "Contraband (Deluxe Edition)"), or a release name starting "Live at/in/
# from".
_REISSUE_WORDS = (
    r"live|remaster|remastered|demo|demos|anniversary|deluxe|expanded|reissue|"
    r"special edition|re-recorded|rerecorded|mono|stereo mix|bonus track|"
    r"rough mix|(?:19|20)\d\d mix|(?:19|20)\d\d\b[^\)\]]*\bremix|takes? \d+|outtakes?|"
    r"alternate(?: take| version)?|rehearsals?|early version|home demo|original radio edit|"
    r"recording|recorded|undubbed|previously unreleased|segue|sessions?"
)
_REISSUE_SUFFIX_RE = re.compile(rf"(?:[\(\[][^\)\]]*\b(?:{_REISSUE_WORDS})\b|\s-\s.*\b(?:{_REISSUE_WORDS})\b)", re.I)
_LIVE_RELEASE_RE = re.compile(r"^live\s+(?:at|in|from)\b", re.I)
# Words that mark a whole release as an old one repackaged, wherever they
# appear in its name ("Absolution XX Anniversary", "Saviors (édition de
# luxe)") - they're rarely part of a genuinely new album's title.
_REISSUE_RELEASE_RE = re.compile(
    r"\b(?:anniversary|deluxe|de luxe|remaster(?:ed)?|expanded edition|collector'?s edition|reissue|"
    r"anthology|vault|rarities|demos|archives?|previously unreleased|bbc sessions?)\b",
    re.I,
)
# Archive material, wherever it appears in a track's name ("Haddon Hall
# Rehearsals Segue: ...").
_ARCHIVAL_TRACK_RE = re.compile(r"\b(?:rehearsals?|outtakes?|undubbed|previously unreleased|bbc sessions?)\b", re.I)
_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")
# A recent release where more than this share of the artist's tracks are
# already among their top tracks on Last.fm is checked against their older
# albums - if the songs are on those, it's a compilation.
_FAMILIAR_SHARE = 0.5
# How many of the artist's older albums that check reads (newest first).
OLDER_ALBUMS_TO_CHECK = 15


def artist_key(name: str) -> str:
    """Spelling-insensitive key for an artist name, so Last.fm's,
    ListenBrainz's and Spotify's versions of a name line up."""
    return most_played._normalize_name(name)


def _title_key(name: str) -> str:
    return most_played._normalize_name(playlist_filter._normalize_title(name))


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 February
        return today.replace(year=today.year - years, day=28)


def released_on_or_after(release_date: str | None, cutoff: date) -> bool:
    """Spotify gives some releases only a year ("2024") or year and month
    ("2024-05"). Those count from the end of that period, so a release that
    might be in the window isn't dropped."""
    if not release_date:
        return False
    parts = release_date.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 12
        day = int(parts[2]) if len(parts) > 2 else calendar.monthrange(year, month)[1]
        return date(year, month, day) >= cutoff
    except (ValueError, IndexError):
        return False


def mentions_year_before(name: str, cutoff: date | None) -> bool:
    """True if name mentions a year before the window starts - "Scream Dream
    1980 Demos", "recorded 11th January 1972" - so it's from the archive."""
    return cutoff is not None and any(int(y) < cutoff.year for y in _YEAR_RE.findall(name))


def looks_like_reissue(name: str, cutoff: date | None = None) -> bool:
    return bool(
        _REISSUE_SUFFIX_RE.search(name)
        or _LIVE_RELEASE_RE.search(name)
        or _ARCHIVAL_TRACK_RE.search(name)
        or mentions_year_before(name, cutoff)
    )


def release_looks_like_reissue(name: str, older_titles: set[str], cutoff: date | None = None) -> bool:
    """A release that's an older one again: a reissue word anywhere in its
    name, a year from before the window, the same title as an older
    release, or an older release's title with something added ("Wish You
    Were Here 50")."""
    if looks_like_reissue(name, cutoff) or _REISSUE_RELEASE_RE.search(name):
        return True
    key = _title_key(name)
    if key in older_titles:
        return True
    return any(
        key.startswith(old + " ") for old in older_titles if len(old) >= 8 or " " in old
    )


class _Caches:
    """The on-disk lookups this tool uses, loaded once per run and saved at
    the end. Entries are {"fetched": unix time, "value": ...} so stale ones
    can be refetched; ttl_days=None keeps them forever."""

    _NAMES = (
        "lastfm_similar",
        "listenbrainz_similar",
        "artist_info",
        "artist_top_tracks",
        "artist_releases",
        "album_tracks",
        "find_more_spotify_artists",
    )

    def __init__(self) -> None:
        self.data = {name: resolve_cache.load(name) for name in self._NAMES}

    def save(self) -> None:
        for name, data in self.data.items():
            resolve_cache.save(name, data)

    def get(self, name: str, key: str, ttl_days: int | None, fetch):
        entry = self.data[name].get(key)
        if entry is not None and (ttl_days is None or time.time() - entry.get("fetched", 0) < ttl_days * 86400):
            return entry["value"]
        value = fetch()
        self.data[name][key] = {"fetched": time.time(), "value": value}
        return value


def _lastfm_or_default(fn, description: str, default, cancel_check: CancelCheck | None):
    """Last.fm answers an artist it doesn't know with an API error - that's
    a real "nothing here" (worth caching), unlike a network failure, which
    is left to raise."""
    try:
        return lastfm_client.call_with_retry(fn, description=description, cancel_check=cancel_check)
    except RuntimeError as exc:
        logger.info("%s: %s", description, exc)
        return default


def _artist_info(caches: _Caches, key: str, name: str, cancel_check: CancelCheck | None) -> dict:
    return caches.get(
        "artist_info",
        key,
        INFO_TTL_DAYS,
        lambda: _lastfm_or_default(
            lambda: lastfm_client.get_artist_info(name),
            f"artist info for {name!r}",
            {"name": name, "mbid": None, "listeners": None, "tags": []},
            cancel_check,
        ),
    )


def _report(progress_id: str, label: str, current: int, total: int) -> None:
    logger.info(
        "%s: %d/%d",
        label,
        current,
        total,
        extra={"progress": {"id": progress_id, "name": label, "current": current, "total": total}},
    )


def _seeds_from(playlists: list[dict]) -> list[dict]:
    """Every primary artist in the playlists, most tracks first, with a
    weight that grows with track count but slowly - an artist with 20
    tracks says more about the playlist than one with 1, but not 20 times
    more."""
    by_key: dict[str, dict] = {}
    seen_uris: set[str] = set()
    for playlist in playlists:
        for track in playlist["tracks"]:
            if track["uri"] in seen_uris:
                continue
            seen_uris.add(track["uri"])
            name = track["artists"].split(",")[0].strip()
            key = artist_key(name)
            if not key:
                continue
            entry = by_key.setdefault(key, {"key": key, "name": name, "tracks": 0})
            entry["tracks"] += 1
    seeds = sorted(by_key.values(), key=lambda s: (-s["tracks"], s["key"]))
    for seed in seeds:
        seed["weight"] = 1 + math.log(seed["tracks"])
    return seeds


def _library_artists(
    sp: Spotify, cancel_check: CancelCheck | None, heard_playlist_ids: list[str] | None = None
) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    """(artist keys, Spotify artist ids, songs) for the part of your
    library that counts: Liked Songs and heard_playlist_ids (every owned or
    collaborative playlist if it's None), so an artist or song that's only
    in some side playlist can still be suggested. songs holds (artist key,
    title key) for every artist on every track, so any version of a song
    counts."""
    owned = forgotten_favourites.owned_playlists(sp)
    heard_ids = set(heard_playlist_ids) if heard_playlist_ids is not None else None
    logger.info(
        "checking %d playlist(s) and Liked Songs for artists and songs you already have",
        len(owned) if heard_ids is None else len(heard_ids & owned.keys()),
    )
    keys: set[str] = set()
    ids: set[str] = set()
    by_uri: dict[str, dict] = {}
    songs: set[tuple[str, str]] = set()

    def add(track: dict) -> None:
        if track.get("artist_id"):
            ids.add(track["artist_id"])
        title = _title_key(track["name"])
        for name in track["artists"].split(","):
            key = artist_key(name)
            keys.add(key)
            songs.add((key, title))

    # Every playlist is read (from cache) even when only some count, so a
    # liked song in one of the others doesn't need looking up.
    for pid, playlist in playlist_cache.get_playlists(sp, list(owned), cancel_check, known=owned).items():
        for track in playlist["tracks"]:
            by_uri.setdefault(track["uri"], track)
            if heard_ids is None or pid in heard_ids:
                add(track)

    # Liked Songs is only cached as uris. Most liked songs are in a playlist
    # too, so only the rest need looking up.
    liked = playlist_cache.get_liked_songs(sp, cancel_check)
    extra = [u for u in liked if u not in by_uri and not u.startswith("spotify:local:")]
    for uri in liked:
        if uri in by_uri:
            add(by_uri[uri])
    if extra:
        logger.info("looking up %d liked song(s) that aren't in a playlist", len(extra))
        for track in playlist_cache.track_details_for_uris(sp, extra).values():
            add(track)
    keys.discard("")
    return keys, ids, songs


def _listening_history(cancel_check: CancelCheck | None) -> dict | None:
    """Your all-time scrobbles as {"artists": {artist key: plays}, "songs":
    {(artist key, title key): plays}}, every spelling and version of a name
    added together. None if your listening history has never been
    downloaded - that first download takes about 20 minutes, which is too
    much to spring on someone from here, so until another tool has done it
    scrobbles aren't checked."""
    if not scrobble_history.history_complete():
        logger.info("listening history not downloaded yet, so not checking scrobbles")
        return None
    logger.info("topping up your Last.fm listening history")
    scrobble_history.sync(cancel_check)
    artists: dict[str, int] = {}
    for name, count in scrobble_history.artist_plays().items():
        key = artist_key(name)
        artists[key] = artists.get(key, 0) + count
    songs: dict[tuple[str, str], int] = {}
    for artist, name, count in scrobble_history.track_plays():
        song = (artist_key(artist), _title_key(name))
        songs[song] = songs.get(song, 0) + count
    return {"artists": artists, "songs": songs}


def _gather_seed_data(
    seeds: list[dict], caches: _Caches, cancel_check: CancelCheck | None
) -> dict:
    """Fills each seed's "info" and "lastfm_similar"/"listenbrainz_similar"
    lists in place. Returns {"seeds_without_mbid", "listenbrainz_failed"}."""
    total = len(seeds)
    done = 0
    _report("seeds", "Looking up similar artists on Last.fm", 0, total)

    def lastfm_task(seed: dict) -> None:
        seed["info"] = _artist_info(caches, seed["key"], seed["name"], cancel_check)
        seed["lastfm_similar"] = caches.get(
            "lastfm_similar",
            seed["key"],
            SIMILAR_TTL_DAYS,
            lambda: _lastfm_or_default(
                lambda: lastfm_client.get_similar_artists(seed["name"], limit=SIMILAR_PER_SEED),
                f"similar artists for {seed['name']!r}",
                [],
                cancel_check,
            ),
        )

    with ThreadPoolExecutor(max_workers=_LASTFM_WORKERS) as pool:
        futures = {pool.submit(lastfm_task, seed): seed for seed in seeds}
        for future in as_completed(futures):
            seed = futures[future]
            try:
                future.result()
            except Exception:
                logger.warning("Last.fm lookup failed for %r, skipping it", seed["name"], exc_info=True)
                seed.setdefault("info", {"name": seed["name"], "mbid": None, "listeners": None, "tags": []})
                seed.setdefault("lastfm_similar", [])
            done += 1
            _report("seeds", "Looking up similar artists on Last.fm", done, total)
            check_cancelled(cancel_check)

    with_mbid = [s for s in seeds if s["info"].get("mbid")]
    for seed in seeds:
        seed["listenbrainz_similar"] = []

    state = {"failures": 0, "gave_up": False}
    lock = threading.Lock()

    def listenbrainz_task(seed: dict) -> None:
        with lock:
            if state["gave_up"]:
                return
        mbid = seed["info"]["mbid"]
        try:
            seed["listenbrainz_similar"] = caches.get(
                "listenbrainz_similar",
                mbid,
                SIMILAR_TTL_DAYS,
                lambda: listenbrainz_client.get_similar_artists(mbid, cancel_check),
            )
        except Exception:
            logger.warning("ListenBrainz lookup failed for %r", seed["name"], exc_info=True)
            with lock:
                state["failures"] += 1
                if state["failures"] >= _LISTENBRAINZ_MAX_FAILURES and not state["gave_up"]:
                    state["gave_up"] = True
                    logger.warning("ListenBrainz isn't responding - carrying on with Last.fm only")
            return
        with lock:
            state["failures"] = 0

    done = 0
    _report("listenbrainz", "Looking up similar artists on ListenBrainz", 0, len(with_mbid))
    with ThreadPoolExecutor(max_workers=_LISTENBRAINZ_WORKERS) as pool:
        futures = [pool.submit(listenbrainz_task, seed) for seed in with_mbid]
        for future in as_completed(futures):
            future.result()
            done += 1
            _report("listenbrainz", "Looking up similar artists on ListenBrainz", done, len(with_mbid))
            check_cancelled(cancel_check)

    return {"seeds_without_mbid": len(seeds) - len(with_mbid), "listenbrainz_failed": state["gave_up"]}


def _profile_tags(seeds: list[dict]) -> dict[str, dict]:
    """The playlist's sound as Last.fm tags: each seed's top tags, weighted
    by the seed's weight and by how high the tag ranks for it. Returns the
    top _PROFILE_TAGS as {lowercased tag: {"name", "weight"}}."""
    profile: dict[str, dict] = {}
    for seed in seeds:
        for i, tag in enumerate(seed["info"].get("tags") or []):
            key = tag.strip().lower()
            if not key or key in _IGNORED_TAGS:
                continue
            entry = profile.setdefault(key, {"name": tag.strip(), "weight": 0.0})
            entry["weight"] += seed["weight"] * max(1.0 - 0.15 * i, 0.25)
    top = sorted(profile.items(), key=lambda kv: -kv[1]["weight"])[:_PROFILE_TAGS]
    return dict(top)


def _score_candidates(seeds: list[dict], include_seeds: bool = False) -> list[dict]:
    """Every artist similar to at least one seed, best first. A candidate's
    score from each source is the sum, over the seeds it's similar to, of
    that seed's weight times how similar it is - so being similar to many
    of the playlist's artists counts for more than being very similar to
    one. Last.fm's match is already 0-1; ListenBrainz's score isn't, so
    it's scaled to 0-1 against the seed's most similar artist. The two
    sources' scores are added, so an artist both agree on ranks above one
    only one of them suggests, other things being equal.

    include_seeds also ranks the playlist's own artists, for "more from
    artists you know": each scores as if both sources rated it a perfect
    match for itself (twice its weight, so more tracks in the playlist
    rank higher), plus whatever it gets from being similar to the
    playlist's other artists. They're marked with "playlist_tracks"."""
    seed_keys = set() if include_seeds else {s["key"] for s in seeds}
    candidates: dict[str, dict] = {}

    def add(name: str, source: str, seed: dict, similarity: float) -> None:
        key = artist_key(name)
        if not key or key in seed_keys or similarity <= 0:
            return
        entry = candidates.setdefault(
            key, {"key": key, "name": name, "scores": {"lastfm": 0.0, "listenbrainz": 0.0}, "seeds": {}}
        )
        contribution = seed["weight"] * similarity
        entry["scores"][source] += contribution
        entry["seeds"][seed["name"]] = entry["seeds"].get(seed["name"], 0.0) + contribution

    for seed in seeds:
        for artist in seed["lastfm_similar"][:SIMILAR_PER_SEED]:
            add(artist["name"], "lastfm", seed, artist["match"])
        listenbrainz = seed["listenbrainz_similar"][:SIMILAR_PER_SEED]
        best = max((a["score"] for a in listenbrainz), default=0)
        if best > 0:
            for artist in listenbrainz:
                add(artist["name"], "listenbrainz", seed, artist["score"] / best)

    for entry in candidates.values():
        entry["score"] = entry["scores"]["lastfm"] + entry["scores"]["listenbrainz"]
    if include_seeds:
        for seed in seeds:
            entry = candidates.setdefault(
                seed["key"],
                {"key": seed["key"], "name": seed["name"], "scores": {"lastfm": 0.0, "listenbrainz": 0.0}, "seeds": {}, "score": 0.0},
            )
            # A seed's similarity to itself isn't "similar to" anything.
            entry["seeds"].pop(seed["name"], None)
            entry["playlist_tracks"] = seed["tracks"]
            entry["score"] += 2 * seed["weight"]
    return sorted(candidates.values(), key=lambda c: (-c["score"], c["key"]))


def _small_image(images: list[dict]) -> str | None:
    """The smallest Spotify image at least 160px wide (for the thumbnails
    this page shows), else the largest there is."""
    if not images:
        return None
    big_enough = [i for i in images if (i.get("width") or 0) >= 160]
    if big_enough:
        return min(big_enough, key=lambda i: i.get("width") or 0)["url"]
    return max(images, key=lambda i: i.get("width") or 0)["url"]


def _search_spotify_artist(sp: Spotify, name: str) -> dict:
    """The Spotify artist with exactly this name (ignoring case, accents and
    punctuation). Stricter than Most Played's artist lookup, which accepts
    a name that merely contains the other - fine for your own top artists,
    but here it turned "Rush" into Patrice Rushen and "Dio" into Dionne
    Warwick."""
    key = artist_key(name)
    resp = sp.search(q=f'artist:"{name}"', type="artist", limit=5)
    for a in (resp.get("artists") or {}).get("items") or []:
        if artist_key(a["name"]) == key:
            return {
                "found": True,
                "uri": a["uri"],
                "name": a["name"],
                "image_url": _small_image(a.get("images") or []),
            }
    return {"found": False}


def _spotify_artist(sp: Spotify, caches: _Caches, key: str, name: str) -> dict:
    return caches.get("find_more_spotify_artists", key, None, lambda: _search_spotify_artist(sp, name))


def _fetch_releases(sp: Spotify, artist_id: str) -> list[dict]:
    """The artist's own albums and singles (not compilations or ones they
    only appear on), one entry per distinct title and type."""
    releases: list[dict] = []
    seen: set[tuple[str, str]] = set()
    results = sp.artist_albums(artist_id, include_groups="album,single", limit=50)
    pages = 0
    while results and pages < 3:
        pages += 1
        for album in results["items"]:
            ident = (album["name"].strip().lower(), album.get("album_type") or "")
            if ident in seen:
                continue
            seen.add(ident)
            releases.append(
                {
                    "id": album["id"],
                    "uri": album["uri"],
                    "name": album["name"],
                    "album_type": album.get("album_type"),
                    "release_date": album.get("release_date"),
                    "total_tracks": album.get("total_tracks") or 0,
                    "image_url": _small_image(album.get("images") or []),
                }
            )
        results = sp.next(results) if results.get("next") else None
    return releases


def _fetch_album_tracks(sp: Spotify, album_id: str) -> list[dict]:
    tracks: list[dict] = []
    results = sp.album_tracks(album_id, limit=50)
    while results:
        for t in results["items"]:
            artists = t.get("artists") or []
            tracks.append(
                {
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": ", ".join(a["name"] for a in artists),
                    "artist_ids": [a["id"] for a in artists if a.get("id")],
                    "explicit": bool(t.get("explicit")),
                    "duration_ms": t.get("duration_ms"),
                }
            )
        results = sp.next(results) if results.get("next") else None
    return tracks


def _album_tracks(sp: Spotify, caches: _Caches, album_id: str) -> list[dict]:
    return caches.get("album_tracks", album_id, None, lambda: _fetch_album_tracks(sp, album_id))


def _release_kind(release: dict) -> str | None:
    if release["album_type"] == "album":
        return "Album"
    if release["album_type"] == "single" and release["total_tracks"] >= MIN_EP_TRACKS:
        return "EP"
    if release["album_type"] == "single":
        return "Single"
    return None


_CREDIT_RE = re.compile(r"\s*(?:[\(\[](?:feat\.?|ft\.?|with)\s[^\)\]]*[\)\]]|\s-\s(?:feat\.?|ft\.?|with)\s.*$)", re.I)


def _top_track_plays(
    caches: _Caches, lastfm_key: str, lastfm_name: str, cancel_check: CancelCheck | None
) -> dict[str, int]:
    """Title key -> all-time Last.fm play count for the artist's top tracks."""
    top = caches.get(
        "artist_top_tracks",
        lastfm_key,
        INFO_TTL_DAYS,
        lambda: _lastfm_or_default(
            lambda: lastfm_client.get_artist_top_tracks(lastfm_name, limit=100),
            f"top tracks for {lastfm_name!r}",
            [],
            cancel_check,
        ),
    )
    plays: dict[str, int] = {}
    for t in top:
        plays.setdefault(_title_key(t["name"]), t["playcount"])
    return plays


def is_old_song(name: str, top_titles, older_titles: set[str], cutoff: date | None = None) -> bool:
    """True for a track on a recent release that isn't new music: a live or
    remastered version, a song titled like one of the artist's older
    releases, or a new version ("Rough Mix", "Remix", "New Orchestrated
    Version") of a song that's already one of their top tracks on Last.fm.
    A featured-artist credit alone doesn't make it a version."""
    if looks_like_reissue(name, cutoff):
        return True
    key = _title_key(name)
    if key in older_titles:
        return True
    base = _CREDIT_RE.sub("", name)
    is_version = _title_key(base) != most_played._normalize_name(base)
    return is_version and key in top_titles


def _tracks_for(
    sp: Spotify,
    caches: _Caches,
    artist_id: str,
    plays: dict[str, int],
    releases: list[dict],
    older_titles: set[str],
    snoozed: set[str],
    heard: set[str],
    cutoff: date | None,
) -> list[dict]:
    """The artist's new tracks on these releases (see is_old_song), most
    played on Last.fm first (newest release first among equals), leaving
    out any whose title key is in heard. A song on both a single and an
    album shows once, as the album version."""
    by_title: dict[str, dict] = {}
    for release in releases:
        for t in _album_tracks(sp, caches, release["id"]):
            if artist_id not in t["artist_ids"] or t["uri"] in snoozed:
                continue
            if is_old_song(t["name"], plays, older_titles, cutoff):
                continue
            key = _title_key(t["name"])
            if key in heard:
                continue
            entry = {
                "uri": t["uri"],
                "name": t["name"],
                "artists": t["artists"],
                "explicit": t["explicit"],
                "album": release["name"],
                "album_type": release["album_type"],
                "release_date": release["release_date"],
                "image_url": release["image_url"],
                "playcount": plays.get(key, 0),
            }
            previous = by_title.get(key)
            if previous is None or (previous["album_type"] != "album" and release["album_type"] == "album"):
                by_title[key] = entry

    tracks = sorted(by_title.values(), key=lambda t: t["release_date"] or "", reverse=True)
    tracks.sort(key=lambda t: -t["playcount"])
    return tracks


def _check_candidate(
    sp: Spotify,
    caches: _Caches,
    candidate: dict,
    mode: str,
    cutoff: date | None,
    max_listeners: int | None,
    library_ids: set[str],
    snoozed: set[str],
    heard: set[str],
    cancel_check: CancelCheck | None,
) -> tuple[str, dict | None]:
    """Returns ("ok", result) or (reason it was dropped, None). A candidate
    with "known" set is an artist you already know: the listener cap and
    library check don't apply, and songs in heard (title keys) are left
    out instead."""
    known = candidate.get("known")
    info = _artist_info(caches, candidate["key"], candidate["name"], cancel_check)
    listeners = info.get("listeners")
    if not known and max_listeners is not None and listeners is not None and listeners > max_listeners:
        return "over_cap", None

    spotify = _spotify_artist(sp, caches, candidate["key"], candidate["name"])
    if not spotify.get("found"):
        return "not_on_spotify", None
    artist_id = spotify["uri"].split(":")[-1]
    if not known and artist_id in library_ids:
        return "in_library", None

    releases = caches.get("artist_releases", artist_id, RELEASES_TTL_DAYS, lambda: _fetch_releases(sp, artist_id))
    in_window = [r for r in releases if cutoff is None or released_on_or_after(r["release_date"], cutoff)]
    in_window_ids = {r["id"] for r in in_window}
    # A recent release named like an older one ("Contraband (Deluxe
    # Edition)" after "Contraband") is the old one again.
    older_titles = {_title_key(r["name"]) for r in releases if r["id"] not in in_window_ids}
    recent = []
    seen_titles: set[str] = set()
    # Oldest first, so of two in-window releases with the same title (an
    # album and its deluxe edition a year later) the original is kept.
    for r in sorted(in_window, key=lambda r: r["release_date"] or ""):
        key = _title_key(r["name"])
        if key in seen_titles or release_looks_like_reissue(r["name"], older_titles, cutoff):
            continue
        seen_titles.add(key)
        recent.append(r)
    recent.sort(key=lambda r: r["release_date"] or "", reverse=True)

    result = {
        "key": candidate["key"],
        "name": spotify["name"],
        "spotify_id": artist_id,
        "image_url": spotify.get("image_url"),
        "listeners": listeners,
        "tags": info.get("tags") or [],
    }

    plays = _top_track_plays(caches, candidate["key"], info.get("name") or candidate["name"], cancel_check)

    # A recent release that's mostly songs already among the artist's top
    # tracks might be a compilation ("Who's Breakthroughs", a soundtrack of
    # old hits) - or a popular new album by a small artist, whose new songs
    # are their top tracks. Their older albums tell the two apart, so
    # they're read (once, cached for good) only when a release looks like
    # this, and any song on them counts as old from then on.
    older_songs: set[str] | None = None

    def older_album_songs() -> set[str]:
        nonlocal older_songs
        if older_songs is None:
            albums = sorted(
                (r for r in releases if r["id"] not in in_window_ids and r["album_type"] == "album"),
                key=lambda r: r["release_date"] or "",
                reverse=True,
            )[:OLDER_ALBUMS_TO_CHECK]
            older_songs = {
                _title_key(t["name"]) for r in albums for t in _album_tracks(sp, caches, r["id"])
            }
        return older_songs

    def is_compilation(release: dict) -> bool:
        if release["total_tracks"] < 3:
            return False
        tracks = [t for t in _album_tracks(sp, caches, release["id"]) if artist_id in t["artist_ids"]]
        titles = [_title_key(t["name"]) for t in tracks]
        familiar = sum(1 for k in titles if k in plays)
        if not titles or familiar <= len(titles) * _FAMILIAR_SHARE:
            return False
        old = older_album_songs()
        return sum(1 for k in titles if k in old) > len(titles) * _FAMILIAR_SHARE

    recent = [r for r in recent if not is_compilation(r)]
    if older_songs:
        older_titles = older_titles | older_songs

    if mode == "albums":
        albums = []
        for release in recent:
            kind = _release_kind(release)
            if kind not in ("Album", "EP") or release["uri"] in snoozed:
                continue
            # A "new" album that's mostly old songs (rarities, rough mixes,
            # re-recordings) isn't new music.
            tracks = [t for t in _album_tracks(sp, caches, release["id"]) if artist_id in t["artist_ids"]]
            old = sum(1 for t in tracks if is_old_song(t["name"], plays, older_titles, cutoff))
            if tracks and old * 2 > len(tracks):
                continue
            # By an artist you know: only albums you've mostly not heard.
            heard_count = sum(1 for t in tracks if _title_key(t["name"]) in heard)
            if tracks and heard_count * 2 >= len(tracks):
                continue
            albums.append({**release, "kind": kind, "artists": spotify["name"]})
            if len(albums) >= RELEASES_PER_ARTIST:
                break
        if not albums:
            return "no_recent", None
        return "ok", {**result, "albums": albums}

    tracks = _tracks_for(
        sp, caches, artist_id, plays, recent[:RELEASES_PER_ARTIST], older_titles, snoozed, heard, cutoff
    )
    if not tracks:
        return "no_recent", None
    limit = TRACKS_PER_ARTIST if mode == "tracks" else SAMPLE_TRACKS_PER_ARTIST
    return "ok", {**result, "tracks": tracks[:limit]}


def find_more_like_this(
    sp: Spotify,
    playlist_ids: list[str],
    mode: str,
    years: int | None,
    max_listeners: int | None,
    include_known: bool = False,
    heard_playlist_ids: list[str] | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    """Suggestions for the playlists' sound, from artists that aren't in
    Liked Songs or heard_playlist_ids (every playlist if None) - or, with
    include_known, also songs you haven't heard by
    artists you already know (the playlist's own artists, and similar ones
    in your library or listening history), ranked in with the rest. A song
    counts as heard if it's in Liked Songs or heard_playlist_ids (every
    playlist if None), or scrobbled SCROBBLES_TO_EXCLUDE+ times. Returns a
    dict with "artists" (best first, each with
    "tracks" or "albums" depending on mode) and what's needed to explain
    the run: the source playlists, how many seeds were used, the
    playlist's tag profile and how many candidates were dropped and why."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    cutoff = _years_ago(date.today(), years) if years else None

    sources = playlist_cache.get_playlists(sp, playlist_ids, cancel_check)
    source_list = [{"id": pid, "name": sources[pid]["name"]} for pid in playlist_ids if pid in sources]
    all_seeds = _seeds_from([sources[pid] for pid in playlist_ids if pid in sources])
    seeds = all_seeds[:MAX_SEEDS]
    logger.info(
        "%d artist(s) in the playlist(s)%s",
        len(all_seeds),
        f", using the {len(seeds)} with the most tracks" if len(seeds) < len(all_seeds) else "",
    )

    caches = _Caches()
    try:
        seed_stats = _gather_seed_data(seeds, caches, cancel_check)
        profile = _profile_tags(seeds)
        candidates = _score_candidates(seeds, include_seeds=include_known)
        logger.info("%d similar artist(s) found", len(candidates))

        library_keys, library_ids, library_songs = _library_artists(sp, cancel_check, heard_playlist_ids)
        history = _listening_history(cancel_check)
        artist_plays = history["artists"] if history else {}
        blocked = find_more_hidden.blocked_keys()
        snoozed = find_more_hidden.snoozed_uris()

        # Songs you count as heard, per artist: anything in Liked Songs or
        # the heard playlists, or scrobbled SCROBBLES_TO_EXCLUDE+ times in any version.
        heard_by_artist: dict[str, set[str]] = {}
        if include_known:
            for artist, title in library_songs:
                heard_by_artist.setdefault(artist, set()).add(title)
            for (artist, title), count in (history["songs"] if history else {}).items():
                if count >= SCROBBLES_TO_EXCLUDE:
                    heard_by_artist.setdefault(artist, set()).add(title)

        skipped = {
            "in_library": 0, "scrobbled": 0, "blocked": 0, "over_cap": 0, "not_on_spotify": 0, "no_recent": 0,
        }
        known_count = 0
        pool_candidates = []
        for candidate in candidates:
            key = candidate["key"]
            if candidate.get("playlist_tracks"):
                known = "playlist"
            elif key in library_keys:
                known = "library"
            elif artist_plays.get(key, 0) >= SCROBBLES_TO_EXCLUDE:
                known = "scrobbled"
            else:
                known = None
            if key in blocked:
                skipped["blocked"] += 1
            elif known and not include_known:
                skipped["in_library" if known == "library" else "scrobbled"] += 1
            else:
                candidate["known"] = known
                known_count += bool(known)
                pool_candidates.append(candidate)

        results: list[dict] = []
        max_known = int(TARGET_ARTISTS * MAX_KNOWN_SHARE)
        known_results = 0
        checked = 0
        to_check = pool_candidates[:MAX_CANDIDATES_CHECKED]
        batch_size = _CANDIDATE_WORKERS * 2
        _report("candidates", "Checking similar artists for recent releases", 0, len(to_check))
        with ThreadPoolExecutor(max_workers=_CANDIDATE_WORKERS) as pool:
            for start in range(0, len(to_check), batch_size):
                if len(results) >= TARGET_ARTISTS:
                    break
                batch = to_check[start : start + batch_size]
                futures = [
                    pool.submit(
                        _check_candidate,
                        sp,
                        caches,
                        c,
                        mode,
                        cutoff,
                        max_listeners,
                        library_ids,
                        snoozed,
                        heard_by_artist.get(c["key"], set()) if c.get("known") else set(),
                        cancel_check,
                    )
                    for c in batch
                ]
                # Collected in the batch's own order, so results stay best-first.
                for candidate, future in zip(batch, futures):
                    try:
                        reason, result = future.result()
                    except Exception:
                        logger.warning("couldn't check %r, skipping it", candidate["name"], exc_info=True)
                        reason, result = "not_on_spotify", None
                    checked += 1
                    if reason != "ok":
                        skipped[reason] += 1
                        continue
                    if candidate.get("known"):
                        if known_results >= max_known:
                            continue
                        known_results += 1
                    ranked = sorted(candidate["seeds"].items(), key=lambda kv: -kv[1])
                    result.update(
                        {
                            "score": round(candidate["score"], 3),
                            "known": candidate.get("known"),
                            "playlist_tracks": candidate.get("playlist_tracks"),
                            "your_scrobbles": artist_plays.get(candidate["key"], 0),
                            "seeds": [name for name, _ in ranked],
                            "sources": [
                                SOURCE_LABELS[s] for s in ("lastfm", "listenbrainz") if candidate["scores"][s] > 0
                            ],
                            "shared_tags": [
                                profile[t.strip().lower()]["name"]
                                for t in result.pop("tags")
                                if t.strip().lower() in profile
                            ],
                        }
                    )
                    results.append(result)
                _report("candidates", "Checking similar artists for recent releases", checked, len(to_check))
                check_cancelled(cancel_check)
    finally:
        caches.save()

    results = results[:TARGET_ARTISTS]
    logger.info("%d artist(s) suggested after checking %d", len(results), checked)
    return {
        "mode": mode,
        "years": years,
        "max_listeners": max_listeners,
        "sources": source_list,
        "seed_count": len(seeds),
        "artist_count": len(all_seeds),
        "seeds_without_mbid": seed_stats["seeds_without_mbid"],
        "listenbrainz_failed": seed_stats["listenbrainz_failed"],
        "scrobbles_checked": history is not None,
        "include_known": include_known,
        "known_count": known_count,
        "scrobbles_to_exclude": SCROBBLES_TO_EXCLUDE,
        "profile_tags": [p["name"] for p in profile.values()],
        "candidate_count": len(candidates),
        "checked": checked,
        "skipped": skipped,
        "artists": results,
    }


def album_track_uris(sp: Spotify, album_ids: list[str]) -> list[str]:
    """Every track on these albums, in album order (albums in the order
    given). Uses the same cache the scan filled, so it's usually free."""
    caches = _Caches()
    try:
        uris: list[str] = []
        for album_id in album_ids:
            uris.extend(t["uri"] for t in _album_tracks(sp, caches, album_id))
        return uris
    finally:
        resolve_cache.save("album_tracks", caches.data["album_tracks"])
