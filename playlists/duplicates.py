from __future__ import annotations

import logging
import re

from spotipy import Spotify

from core.cancellation import CancelCheck
import playlists.playlist_cache as playlist_cache_module
import playlists.removal_history as removal_history_module

REMOVE_BATCH_SIZE = 100
# How far apart two releases' lengths can be and still count as the same
# song - reissues and remasters often drift by a second or so.
RELEASE_DURATION_TOLERANCE_MS = 2000

# Title suffixes that only label a release of the same recording, so they're
# stripped before matching. This is deliberately an allowlist: any other
# suffix ("- Live", "- Radio Edit", "- Mono", "- 2019 Mix", "(So Far Away)")
# stays part of the title, so an unfamiliar one reads as a different song -
# the safe mistake for a tool that removes tracks. The length tolerance
# still applies on top, which is what keeps e.g. an "Original Mix" that's
# really an extended cut apart from the radio-length track.
_SAME_RECORDING_SUFFIX_RE = re.compile(
    r"""(
        ((19|20)\d\d\s+(-\s+)?)?remaster(ed)?(\s+version)?(\s+(19|20)\d\d)?
        | original\s+mix
        | prod(\.|uced)?\s+(by\s+)?.+
        | bonus(\s+track)?
        | (theme\s+)?from\s+(the\s+motion\s+picture\s+)?["“].+
        | from\s+.+\s+(soundtrack|the\s+album|score)
        | .+\s+soundtrack(\s+version)?
    )""",
    re.VERBOSE,
)
# A featured-artist credit, wherever it sits in the title ("Show Me Love
# (feat. X) - Skrillex Remix") - often on one release but not another, and
# the artists field already says who's on the track.
_CREDIT_RE = re.compile(r"\s*[\(\[](feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]")
_BRACKETED_SUFFIX_RE = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]$")
_DASH_RE = re.compile(r"\s+-\s+")
_LAST_DASH_SUFFIX_RE = re.compile(r"\s+-\s+((?:(?!\s-\s).)+)$")
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)$")
# Words that mark bracketed text as naming a version rather than being part
# of the song's name - see _album_title.
_VERSION_WORD_RE = re.compile(
    r"""\b(
        live | acoustic | accoustic | unplugged | stripped | remix(ed)? | mix | edit
        | version | instrumental | demo | mono | stereo | reprise | interlude | intro
        | outro | vip | flip | extended | radio | single | re-?record(ed|ings?)
        | session | take | karaoke | a\s?cappella | acapella | dub | rework | bootleg
        | (19|20)\d\d
    )\b""",
    re.VERBOSE,
)

logger = logging.getLogger(__name__)


def _strip_same_recording_suffix(title: str) -> str | None:
    """title with one trailing same-recording suffix removed, or None if it
    doesn't end in one. A dash suffix is matched from the earliest " - " whose
    remainder is a whole suffix, so "- 2015 - Remaster" goes in one step."""
    bracketed = _BRACKETED_SUFFIX_RE.search(title)
    if bracketed and _SAME_RECORDING_SUFFIX_RE.fullmatch(bracketed.group(1).strip()):
        return title[: bracketed.start()]
    for dash in _DASH_RE.finditer(title):
        if _SAME_RECORDING_SUFFIX_RE.fullmatch(title[dash.end() :].strip()):
            return title[: dash.start()]
    return None


def _release_title(name: str) -> str:
    """The title used to match the same song across releases: lowercased,
    curly apostrophes straightened, featured-artist credits dropped, and
    same-recording suffixes (see _SAME_RECORDING_SUFFIX_RE) stripped -
    repeatedly, since a title can carry several ("Song - Bonus Track - 2009
    Remaster")."""
    title = name.strip().lower().replace("’", "'").replace("[", "(").replace("]", ")")
    title = _CREDIT_RE.sub("", title)
    while (stripped := _strip_same_recording_suffix(title)) is not None:
        title = stripped.strip()
    # Releases write the same label both ways ("- Club Mix" / "(Club Mix)"),
    # so any dash suffix left is rewritten in bracket form to compare equal.
    while dash := _LAST_DASH_SUFFIX_RE.search(title):
        title = f"{title[: dash.start()]} ({dash.group(1)})"
    return _CREDIT_RE.sub("", title).strip()


def _album_title(release_title: str) -> str:
    """release_title with any trailing bracketed text dropped unless it names
    a version (see _VERSION_WORD_RE) - so "the kill (bury me)" gives "the
    kill" but "miss understood (instrumental)" is left alone. Only used to
    compare tracks on the same album, where the same track is sometimes
    titled two ways; across albums "(So Far Away)" style text still counts."""
    title = release_title
    while (paren := _TRAILING_PAREN_RE.search(title)) and not _VERSION_WORD_RE.search(paren.group(1)):
        title = title[: paren.start()].strip()
    return title


def _release_clusters(tracks: list[dict]) -> list[list[dict]]:
    """Groups tracks (distinct uris) that look like the same song on
    different releases: same artists, same _release_title (or, on the same
    album, the same _album_title), and lengths within
    RELEASE_DURATION_TOLERANCE_MS of each other. Local files and tracks with
    no length are left out - they only ever match by exact uri. Returns only
    clusters of 2+ tracks."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for track in tracks:
        if track.get("is_local") or track.get("duration_ms") is None:
            continue
        key = (track["artists"].strip().lower(), _release_title(track["name"]))
        by_key.setdefault(key, []).append(track)

    # Merge title keys that meet on one album (union-find over the keys), so
    # their tracks go through the length check together.
    parent = {key: key for key in by_key}

    def root(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            key = parent[key]
        return key

    first_key_by_album_title: dict[tuple[str, str, str], tuple[str, str]] = {}
    for key, candidates in by_key.items():
        for track in candidates:
            album = (track.get("album") or "").strip().lower()
            if not album:
                continue
            other = first_key_by_album_title.setdefault((key[0], album, _album_title(key[1])), key)
            parent[root(key)] = root(other)

    merged: dict[tuple[str, str], list[dict]] = {}
    for key, candidates in by_key.items():
        merged.setdefault(root(key), []).extend(candidates)

    clusters = []
    for candidates in merged.values():
        # Anchoring each cluster on its shortest track (rather than chaining
        # neighbours) keeps every pair in it within the tolerance.
        cluster: list[dict] = []
        for track in sorted(candidates, key=lambda t: t["duration_ms"]):
            gap = track["duration_ms"] - cluster[0]["duration_ms"] if cluster else 0
            if gap > RELEASE_DURATION_TOLERANCE_MS:
                clusters.append(cluster)
                cluster = []
            cluster.append(track)
        clusters.append(cluster)
    return [c for c in clusters if len(c) > 1]


def find_duplicates_from_tracks(playlists: list[dict]) -> list[dict]:
    """playlists: [{"id": ..., "name": ..., "tracks": [...]}, ...] with
    already-fetched tracks (each needing "uri", "name", "artists",
    "added_at", and ideally "album", "duration_ms", "is_local").

    Returns duplicate groups, sorted by artist/name. Every group lists one
    or more releases (distinct uris) and where each one appears:
    [{"kind": "exact" | "release", "uri", "name", "artists",
      "releases": [{"uri", "name", "album", "duration_ms",
                    "occurrences": [{"playlist_id", "playlist_name",
                                     "added_at"}, ...]}, ...]}]

    "exact" groups are one uri in more than one place. "release" groups are
    the same song on two or more different releases (see _release_clusters),
    with every occurrence of each of them - a song that matches both ways
    only appears as a "release" group."""
    occurrences_by_uri: dict[str, list[dict]] = {}
    track_by_uri: dict[str, dict] = {}

    for playlist in playlists:
        for track in playlist["tracks"]:
            occurrences_by_uri.setdefault(track["uri"], []).append(
                {
                    "playlist_id": playlist["id"],
                    "playlist_name": playlist["name"],
                    "added_at": track["added_at"],
                }
            )
            track_by_uri[track["uri"]] = track

    def release(uri: str) -> dict:
        track = track_by_uri[uri]
        return {
            "uri": uri,
            "name": track["name"],
            "album": track.get("album") or "",
            "duration_ms": track.get("duration_ms"),
            "occurrences": sorted(occurrences_by_uri[uri], key=lambda o: o["added_at"] or ""),
        }

    def group(kind: str, releases: list[dict]) -> dict:
        # The shortest title is usually the plain one ("Dreams" rather than
        # "Dreams - Single Version"), so it makes the best heading.
        first = track_by_uri[min(releases, key=lambda r: len(r["name"]))["uri"]]
        return {
            "kind": kind,
            "uri": first["uri"],
            "name": first["name"],
            "artists": first["artists"],
            "releases": releases,
        }

    duplicates = []
    grouped_uris: set[str] = set()
    for cluster in _release_clusters(list(track_by_uri.values())):
        releases = sorted(
            (release(t["uri"]) for t in cluster),
            key=lambda r: r["occurrences"][0]["added_at"] or "",
        )
        duplicates.append(group("release", releases))
        grouped_uris.update(t["uri"] for t in cluster)

    for uri, occurrences in occurrences_by_uri.items():
        if len(occurrences) > 1 and uri not in grouped_uris:
            duplicates.append(group("exact", [release(uri)]))

    duplicates.sort(key=lambda d: (d["artists"].lower(), d["name"].lower()))
    logger.info(
        "found %d duplicate song(s) across %d playlists (%d on different releases)",
        len(duplicates),
        len(playlists),
        sum(1 for d in duplicates if d["kind"] == "release"),
    )
    return duplicates


def find_duplicates(
    sp: Spotify, playlist_ids: list[str], cancel_check: CancelCheck | None = None
) -> list[dict]:
    playlists = playlist_cache_module.get_playlists(sp, playlist_ids, cancel_check)
    fetched = [
        {"id": pid, "name": playlists[pid]["name"], "tracks": playlists[pid]["tracks"]}
        for pid in playlist_ids
    ]
    return find_duplicates_from_tracks(fetched)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def remove_from_playlists(
    sp: Spotify, removals: list[dict], source: str = "Duplicate Finder"
) -> str | None:
    """removals: [{"playlist_id": ..., "uri": ...}, ...]

    Logs the removal to removal_history under source first; returns that
    history entry's id (None if there was nothing to remove)."""
    by_playlist: dict[str, list[str]] = {}
    for removal in removals:
        by_playlist.setdefault(removal["playlist_id"], []).append(removal["uri"])

    playlists = {pid: playlist_cache_module.get_playlist(sp, pid) for pid in by_playlist}
    history_id = removal_history_module.record(
        source,
        [
            removal_history_module.snapshot_removed(
                pid, playlists[pid]["name"], playlists[pid]["tracks"], uris
            )
            for pid, uris in by_playlist.items()
        ],
    )

    for playlist_id, uris in by_playlist.items():
        current_tracks = playlists[playlist_id]["tracks"]

        batches = list(_chunks(uris, REMOVE_BATCH_SIZE))
        for i, batch in enumerate(batches, start=1):
            logger.info(
                "removing %d/%d track(s) from playlist %s (batch %d/%d)",
                len(batch),
                len(uris),
                playlist_id,
                i,
                len(batches),
            )
            sp.playlist_remove_all_occurrences_of_items(playlist_id, batch)

        removed_uris = set(uris)
        updated_tracks = [t for t in current_tracks if t["uri"] not in removed_uris]
        try:
            playlist_cache_module.refresh_after_mutation(sp, playlist_id, updated_tracks)
        except Exception:
            # The removal itself already succeeded; don't let a failure in
            # this housekeeping call make it look like the whole operation
            # failed. Worst case the cache stays stale until it self-heals
            # on the next read (snapshot_id won't match).
            logger.warning(
                "playlist %s: track(s) removed, but refreshing the cache afterwards "
                "failed - it'll self-correct on the next read",
                playlist_id,
                exc_info=True,
            )

    return history_id
