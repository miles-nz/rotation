from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import playlists.playlist_cache as playlist_cache_module

logger = logging.getLogger(__name__)

# Strips a trailing single-release title suffix so "Song Name - Single",
# "Song Name (Single)" and "Song Name (Single Version)" all normalize to
# "Song Name" for matching against the album cut. Extend only as real
# false-negatives show up - the review-and-confirm step before anything is
# swapped is the actual safety net for an imperfect match, not this regex.
_SINGLE_SUFFIX_RE = re.compile(r"\s*[-(]\s*single(\s+version)?\)?\s*$", re.IGNORECASE)


def normalized_signature(artists: str, name: str) -> tuple[str, str]:
    """Identifies "the same song" for single-vs-album matching: artist +
    title, case/whitespace insensitive, with a trailing single-release
    suffix stripped from the title (see _SINGLE_SUFFIX_RE)."""
    name = _SINGLE_SUFFIX_RE.sub("", name).strip()
    return (artists.strip().lower(), name.strip().lower())


# Edition matching: an album's other editions are the artist's full albums
# whose title is the same once bracketed/dashed suffixes and punctuation are
# dropped, or extends it by whole words ("SMILE! :D" -> "SMILE! :D plus+",
# "Butterfly" -> "Butterfly (Expanded Edition)"). A title match alone isn't
# trusted - see _EDITION_MIN_OVERLAP.
_BRACKETED_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")
_DASH_SUFFIX_RE = re.compile(r"\s+-\s+.*$")
_NON_WORD_RE = re.compile(r"[^\w]+")

# Share of the smaller edition's songs the two editions must have in common
# to count as editions of one album rather than two albums that happen to
# share a title prefix (e.g. "Butterfly" and "Butterfly Effect").
_EDITION_MIN_OVERLAP = 0.5


def _edition_base_title(name: str) -> str:
    name = _DASH_SUFFIX_RE.sub("", _BRACKETED_RE.sub("", name))
    return _NON_WORD_RE.sub(" ", name.lower()).strip()


def _titles_could_be_editions(a: str, b: str) -> bool:
    shorter, longer = sorted((_edition_base_title(a), _edition_base_title(b)), key=len)
    return bool(shorter) and (longer == shorter or longer.startswith(shorter + " "))


def _signatures(album: dict) -> set[tuple[str, str]]:
    return {normalized_signature(t["artists"], t["name"]) for t in album["tracks"]}


def get_album_tracks(sp: Spotify, album_id: str) -> dict:
    """Fetches an album's own metadata and full tracklist (in album order)
    straight from Spotify's catalog - not from any playlist. Returns
    {"id", "uri", "name", "artists", "artist_id", "image_url",
     "release_date", "tracks": [{"uri", "name", "artists"}, ...]}.
    """
    album = sp.album(album_id)
    artists = album.get("artists") or []
    images = album.get("images") or []

    tracks: list[dict] = []
    page = album.get("tracks")
    while page:
        for t in page.get("items") or []:
            track_artists = t.get("artists") or []
            tracks.append(
                {
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": ", ".join(a["name"] for a in track_artists),
                }
            )
        page = sp.next(page) if page.get("next") else None

    return {
        "id": album["id"],
        "uri": album["uri"],
        "name": album["name"],
        "artists": ", ".join(a["name"] for a in artists),
        "artist_id": artists[0]["id"] if artists else None,
        "image_url": images[0]["url"] if images else None,
        "release_date": album.get("release_date") or "",
        "tracks": tracks,
    }


def get_album_family(
    sp: Spotify, album_id: str, cancel_check: CancelCheck | None = None
) -> dict:
    """The album plus its other editions (deluxe, expanded, "plus+" and so
    on - see _titles_could_be_editions and _EDITION_MIN_OVERLAP), and which
    edition each song should be swapped to: the earliest-released edition
    it's on, so songs from the original album go to the original and
    songs only on a later edition go to that edition. Editions released
    the same day prefer the one with fewer tracks (the standard), then the
    album asked for.

    Returns {"editions": [album dicts from get_album_tracks, earliest first],
             "by_signature": {signature: (edition, track)},
             "uris": every track uri on every edition}.
    """
    album = get_album_tracks(sp, album_id)
    editions = [album]
    if album["artist_id"]:
        page = sp.artist_albums(album["artist_id"], include_groups="album", limit=50)
        seen = {album["id"]}
        while page:
            for candidate in page.get("items") or []:
                if candidate["id"] in seen or not _titles_could_be_editions(album["name"], candidate["name"]):
                    continue
                seen.add(candidate["id"])
                check_cancelled(cancel_check)
                other = get_album_tracks(sp, candidate["id"])
                shared = len(_signatures(album) & _signatures(other))
                smaller = min(len(album["tracks"]), len(other["tracks"])) or 1
                if shared / smaller >= _EDITION_MIN_OVERLAP:
                    editions.append(other)
            page = sp.next(page) if page.get("next") else None

    editions.sort(key=lambda e: (e["release_date"], len(e["tracks"]), e["id"] != album_id))
    by_signature: dict[tuple[str, str], tuple[dict, dict]] = {}
    uris: set[str] = set()
    for edition in editions:
        for track in edition["tracks"]:
            uris.add(track["uri"])
            by_signature.setdefault(normalized_signature(track["artists"], track["name"]), (edition, track))

    if len(editions) > 1:
        logger.info(
            "album swap: '%s' has %d editions - each song goes to the earliest one it's on: %s",
            album["name"],
            len(editions),
            ", ".join(f"'{e['name']}' ({e['release_date']})" for e in editions),
        )
    return {"editions": editions, "by_signature": by_signature, "uris": uris}


# A later edition only gets a song when that edition is close in time to
# the single - a deluxe adding recent songs. A reissue years later that
# happens to include an old single (or a remix of it) doesn't count: the
# single stays.
_MAX_YEARS_SINGLE_BEFORE_EDITION = 4

# Words that mark a track title's suffix as a version of the song (a remix,
# rework, live take...) rather than part of its name, e.g. the "Flume
# Re-work" in "Gold - Flume Re-work" or "Gold (Flume Re-work)".
_VERSION_WORDS_RE = re.compile(
    r"\b(re-?mix|mix|re-?work|edit|version|live|acoustic|remaster(ed)?|demo|"
    r"instrumental|vip|flip|bootleg|reprise|stripped|unplugged|dub|session|cover)\b",
    re.IGNORECASE,
)
_DASH_SUFFIX_RE = re.compile(r"^(.+?)\s+-\s+(.+)$")
_BRACKET_SUFFIX_RE = re.compile(r"^(.+?)\s*[(\[]([^()\[\]]+)[)\]]\s*$")


def track_version(name: str) -> tuple[str, str | None]:
    """Splits a track title into (song title, version label), e.g.
    "Gold - Flume Re-work" -> ("Gold", "Flume Re-work"). The label is None
    when the title doesn't carry a version suffix ("feat." brackets and
    other suffixes are left in the title)."""
    for pattern in (_DASH_SUFFIX_RE, _BRACKET_SUFFIX_RE):
        match = pattern.match(name)
        if match and _VERSION_WORDS_RE.search(match.group(2)):
            return match.group(1).strip(), match.group(2).strip()
    return name, None


def _release_day(release_date: str | None) -> date | None:
    """Spotify release dates come as "YYYY", "YYYY-MM" or "YYYY-MM-DD";
    a missing month or day counts as the first."""
    parts = (release_date or "").split("-")
    try:
        return date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None


def _years_apart_more_than(earlier: date, later: date, years: int) -> bool:
    try:
        cutoff = later.replace(year=later.year - years)
    except ValueError:  # 29 February
        cutoff = later.replace(year=later.year - years, day=28)
    return earlier < cutoff


def _release_kind(album: dict) -> str:
    """A label for what kind of release a track is on, as the review
    screen shows it. Spotify files EPs under album_type "single" too, so
    those are told apart by length: up to 3 tracks is a single."""
    album_type = album.get("album_type")
    if album_type == "single":
        return "Single" if (album.get("total_tracks") or 0) <= 3 else "EP"
    return {"album": "Album", "compilation": "Compilation"}.get(album_type, "Release")


def _releases_for_tracks(sp: Spotify, uris: list[str]) -> dict[str, dict]:
    """The release each track uri is on, as {uri: {"name", "kind", "year",
    "image_url"}} - for showing which version a swap replaces. Playlist
    cache tracks only carry the album's name, not its type or art, so this
    looks the (usually few) swap candidates up directly, 50 at a time."""
    releases: dict[str, dict] = {}
    unique = list(dict.fromkeys(uris))
    for i in range(0, len(unique), 50):
        batch = [uri.split(":")[-1] for uri in unique[i : i + 50]]
        for track in sp.tracks(batch)["tracks"]:
            if not track:
                continue
            album = track.get("album") or {}
            images = album.get("images") or []
            releases[track["uri"]] = {
                "name": album.get("name") or "",
                "kind": _release_kind(album),
                "year": (album.get("release_date") or "")[:4],
                "release_date": album.get("release_date") or "",
                "image_url": images[-1]["url"] if images else None,
            }
    return releases


def find_swaps(
    sp: Spotify,
    album_ids: list[str],
    playlist_ids: list[str],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """The scan job target. Expands each album into its family of editions
    (get_album_family) and finds, in each of playlist_ids, any track that
    is a single-version match (normalized_signature) for a song in one of
    those families. Each match is swapped to the earliest edition that
    song is on.

    Not retroactive: a playlist track that's already on any edition of any
    of these albums is left alone, so an existing deluxe entry is never
    moved to the standard edition (or the other way round). Local files are
    never candidates - the Web API can't remove/reinsert them at a specific
    position any more than it can add them.

    Albums that are editions of one already in the list (e.g. detection
    found both the standard and the deluxe) are folded into that family.
    When a track matches songs in more than one family, the first family
    wins, so each playlist position is offered at most once. Playlists are
    read once however many albums there are.

    A track's position is just its index in playlist_cache's already-
    ordered track list - there's no separate position field on a track
    dict.

    Returns {"albums": [{"id","name","artists","image_url","edition_ids",
                         "editions": [{"id","name","release_date"}, ...]}, ...],
             "album": the first of "albums",
             "swaps": [{"id", "album_id", "playlist_id", "playlist_name",
                        "position", "title", "version",
                        "single": {"uri","name","artists"},
                        "album_track": {"uri","name","artists"},
                        "edition": {"id","name"} or None,
                        "from_release"/"to_release": {"name", "kind",
                            "year", "image_url"}}, ...]}
    One entry in "albums" per family, shown as its earliest edition; a
    swap's "album_id" is that family's id, and "edition" is set when the
    swap goes to a later edition rather than that one - skipped when the
    single came out more than _MAX_YEARS_SINGLE_BEFORE_EDITION years before
    that edition. "title"/"version" split the song's name from a version
    label like "Flume Re-work" (None for the original). "from_release" is
    the release the playlist's current version is on (None if Spotify
    didn't return it) and "to_release" the edition it'll be swapped to. "id"
    (f"{playlist_id}:{position}") is a stable per-row key used to
    round-trip which swaps the user confirmed on the review screen - the
    apply step re-reads this same result from server memory rather than
    trusting track data sent back from the client.
    """
    albums: list[dict] = []
    by_signature: dict[tuple[str, str], tuple[str, dict, dict]] = {}
    album_uris: set[str] = set()
    covered: set[str] = set()
    for album_id in album_ids:
        if album_id in covered:
            continue
        check_cancelled(cancel_check)
        family = get_album_family(sp, album_id, cancel_check)
        editions = family["editions"]
        covered.update(e["id"] for e in editions)
        first = editions[0]
        albums.append(
            {
                "id": first["id"],
                "name": first["name"],
                "artists": first["artists"],
                "image_url": first["image_url"],
                "edition_ids": [e["id"] for e in editions],
                "editions": [
                    {"id": e["id"], "name": e["name"], "release_date": e["release_date"]}
                    for e in editions
                ],
            }
        )
        album_uris |= family["uris"]
        for signature, (edition, track) in family["by_signature"].items():
            by_signature.setdefault(signature, (first["id"], edition, track))

    playlists = playlist_cache_module.get_playlists(sp, playlist_ids, cancel_check)

    swaps: list[dict] = []
    for playlist_id in playlist_ids:
        playlist = playlists[playlist_id]
        for position, track in enumerate(playlist["tracks"]):
            if track.get("is_local") or track["uri"] in album_uris:
                continue
            match = by_signature.get(normalized_signature(track["artists"], track["name"]))
            if match is None:
                continue
            family_id, edition, album_track = match
            title, version = track_version(track["name"])
            if version is None:
                title, version = track_version(album_track["name"])
            swaps.append(
                {
                    "id": f"{playlist_id}:{position}",
                    "album_id": family_id,
                    "playlist_id": playlist_id,
                    "playlist_name": playlist["name"],
                    "position": position,
                    "title": title,
                    "version": version,
                    "single": {
                        "uri": track["uri"],
                        "name": track["name"],
                        "artists": track["artists"],
                    },
                    "album_track": album_track,
                    "edition": (
                        {"id": edition["id"], "name": edition["name"]}
                        if edition["id"] != family_id
                        else None
                    ),
                    "to_release": {
                        "name": edition["name"],
                        "kind": "Album",
                        "year": edition["release_date"][:4],
                        "release_date": edition["release_date"],
                        "image_url": edition["image_url"],
                    },
                }
            )
        check_cancelled(cancel_check)

    from_releases = _releases_for_tracks(sp, [s["single"]["uri"] for s in swaps])
    for swap in swaps:
        swap["from_release"] = from_releases.get(swap["single"]["uri"])

    # Spotify keeps old copies of some albums under separate ids that the
    # artist's album list doesn't return, so a playlist track can be on
    # "the same album" without its uri being on any edition found above.
    # That's already the album version - nothing to swap.
    edition_names = {a["id"]: [e["name"] for e in a["editions"]] for a in albums}
    kept = [
        s
        for s in swaps
        if not (
            s["from_release"]
            and s["from_release"]["kind"] == "Album"
            and any(
                _titles_could_be_editions(s["from_release"]["name"], name)
                for name in edition_names[s["album_id"]]
            )
        )
    ]
    if len(kept) < len(swaps):
        logger.info(
            "album swap scan: skipped %d track(s) already on another copy of the album",
            len(swaps) - len(kept),
        )
    swaps = kept

    # A later edition released long after the single (an anniversary
    # reissue with bonus tracks, say) isn't where the song first came out
    # on an album in any meaningful sense - keep the single.
    kept = []
    for s in swaps:
        single_day = _release_day((s["from_release"] or {}).get("release_date"))
        edition_day = _release_day(s["to_release"]["release_date"])
        if (
            s["edition"]
            and single_day
            and edition_day
            and _years_apart_more_than(single_day, edition_day, _MAX_YEARS_SINGLE_BEFORE_EDITION)
        ):
            logger.info(
                "album swap scan: kept the single of '%s' - it came out more than %d years "
                "before '%s'",
                s["single"]["name"],
                _MAX_YEARS_SINGLE_BEFORE_EDITION,
                s["to_release"]["name"],
            )
            continue
        kept.append(s)
    swaps = kept

    logger.info(
        "album swap scan: found %d swap(s) across %d playlist(s) for %s",
        len(swaps),
        len(playlist_ids),
        f"'{albums[0]['name']}'" if len(albums) == 1 else f"{len(albums)} albums",
    )
    return {
        "albums": albums,
        "album": albums[0] if albums else None,
        "swaps": swaps,
    }


# Spotify's playlist write endpoints take at most 100 tracks per request.
_MAX_ITEMS_PER_REQUEST = 100


def _position_runs(slots: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Splits (position, uri) slots, sorted by position, into runs of
    consecutive positions, so each run can go back in one insert call."""
    runs: list[list[tuple[int, str]]] = []
    for slot in slots:
        if runs and slot[0] == runs[-1][-1][0] + 1:
            runs[-1].append(slot)
        else:
            runs.append([slot])
    return runs


def _apply_playlist_swaps(
    sp: Spotify, playlist_id: str, current_uris: list[str], swaps: list[dict]
) -> tuple[list[dict], list[int]]:
    """Swaps singles for album tracks in one playlist with as few requests
    as possible, while every other track keeps its position. current_uris
    is the playlist's track list as it is now. Returns the swaps applied
    and the positions of other copies of a single that were re-added.

    Spotify's remove endpoint ignores positions and drops every copy of a
    uri, so a single can't be removed from just one spot. Instead, for up
    to 100 singles at a time, one request removes them outright, then
    every position that emptied goes back in ascending order, one insert
    per run of consecutive positions: the album track where it's a swap,
    and the single itself where it's another copy that isn't being
    swapped (that copy's added date resets, unavoidably). Inserting in
    ascending order puts each track back at its original index, because
    everything before it is already restored, and each chunk leaves the
    playlist whole again, so the next chunk's positions still hold.

    Removing and restoring per chunk (not all removals first) keeps the
    window where tracks are missing to a single chunk if a request fails
    midway."""
    applied: list[dict] = []
    readded: list[int] = []
    replacement_at: dict[int, str] = {}
    for swap in swaps:
        position = swap["position"]
        if position < len(current_uris) and current_uris[position] == swap["single"]["uri"]:
            replacement_at[position] = swap["album_track"]["uri"]
            applied.append(swap)
        else:
            logger.warning(
                "playlist %s: skipped swapping '%s' - it's no longer at position %d "
                "(the playlist changed since the scan)",
                playlist_id,
                swap["single"]["name"],
                position + 1,
            )

    singles = list(dict.fromkeys(s["single"]["uri"] for s in applied))
    for i in range(0, len(singles), _MAX_ITEMS_PER_REQUEST):
        chunk = set(singles[i : i + _MAX_ITEMS_PER_REQUEST])
        slots = [
            (position, replacement_at.get(position, uri))
            for position, uri in enumerate(current_uris)
            if uri in chunk
        ]
        readded.extend(position for position, _ in slots if position not in replacement_at)

        sp.playlist_remove_all_occurrences_of_items(playlist_id, list(chunk))

        restored = 0
        try:
            for run in _position_runs(slots):
                for j in range(0, len(run), _MAX_ITEMS_PER_REQUEST):
                    part = run[j : j + _MAX_ITEMS_PER_REQUEST]
                    sp.playlist_add_items(playlist_id, [uri for _, uri in part], position=part[0][0])
                    restored += len(part)
        except Exception:
            logger.error(
                "playlist %s: removed tracks but failed to put them all back - "
                "these need re-adding by hand: %s",
                playlist_id,
                ", ".join(f"{uri} at position {position + 1}" for position, uri in slots[restored:]),
            )
            raise

    for swap in applied:
        logger.info(
            "playlist %s: swapped '%s' for '%s' at position %d",
            playlist_id,
            swap["single"]["name"],
            swap["album_track"]["name"],
            swap["position"],
        )
    return applied, readded


def apply_swaps(sp: Spotify, swaps: list[dict]) -> dict[str, dict]:
    """swaps: the confirmed subset of find_swaps()'s "swaps" list. Replaces
    each swap's single with its album track at the same position, batching
    the requests per playlist (see _apply_playlist_swaps).

    Returns {playlist_id: {"playlist_name", "swapped": n}}.
    """
    by_playlist: dict[str, list[dict]] = {}
    for swap in swaps:
        by_playlist.setdefault(swap["playlist_id"], []).append(swap)

    album_uris = list({s["album_track"]["uri"] for s in swaps})
    album_track_details = playlist_cache_module.track_details_for_uris(sp, album_uris)

    summary: dict[str, dict] = {}
    for playlist_id, playlist_swaps in by_playlist.items():
        playlist_name = playlist_swaps[0]["playlist_name"]
        current_tracks = playlist_cache_module.get_playlist(sp, playlist_id)["tracks"]
        playlist_swaps, readded = _apply_playlist_swaps(
            sp, playlist_id, [t["uri"] for t in current_tracks], playlist_swaps
        )
        summary[playlist_id] = {"playlist_name": playlist_name, "swapped": len(playlist_swaps)}
        if not playlist_swaps:
            continue

        # The removals and insertions above already succeeded; nothing from
        # here on should turn into an exception that makes it look like the
        # swap failed. Worst case the cache stays stale until it
        # self-corrects on the next read (snapshot_id won't match).
        try:
            new_tracks = list(current_tracks)
            for swap in playlist_swaps:
                details = album_track_details.get(swap["album_track"]["uri"])
                if details:
                    new_tracks[swap["position"]] = details
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for position in readded:
                new_tracks[position] = {**new_tracks[position], "added_at": now}
            playlist_cache_module.refresh_after_mutation(sp, playlist_id, new_tracks)
        except Exception:
            logger.warning(
                "playlist %s: swap(s) applied, but refreshing the cache afterwards "
                "failed - it'll self-correct on the next read",
                playlist_id,
                exc_info=True,
            )

    return summary
