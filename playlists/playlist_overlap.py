from __future__ import annotations

import logging
import unicodedata

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import playlists.playlist_cache as playlist_cache_module
import playlists.ranking_check as ranking_check

logger = logging.getLogger(__name__)

# Playlist Overlap: draws one playlist (e.g. the yearly playlist) as a
# stack of bands, one per song in playlist order, each coloured by which of
# a set of other playlists (e.g. the ranking playlists, best first) the song
# is in. Read-only, manual only - not a Cascade step.

# The colour playlists are in order (best first), so they share one blue
# ramp with monotone lightness: the order reads straight off the shade.
# Best is the strongest end - darkest on a light page, lightest on a dark
# one - and the faintest still clears 2:1 against the page. Shades of one
# hue get hard to tell apart one by one, so each band also carries its
# playlist's marker (see marker()) - the shade shows the overall pattern,
# the marker says exactly which playlist. Up to five playlists the shades
# were checked to step clearly apart in both modes.
LIGHT_RAMP = [
    "#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf",
    "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef",
]
DARK_RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
    "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95",
]
# A single colour playlist has no order to show, so it takes the middle blue.
SINGLE_COLOUR = {"light": "#2a78d6", "dark": "#3987e5"}

# Past this the ramp would have to repeat shades.
MAX_COLOUR_PLAYLISTS = len(LIGHT_RAMP)

# Songs in none of the colour playlists: a neutral grey, clearly not blue
# (and pale on a light page, so they recede).
NONE_COLOUR = {"light": "#d6d5d0", "dark": "#5c5c57"}


def colours(count: int) -> list[dict]:
    """count shades, best first, as [{"light", "dark"}] - spread evenly
    across the ramp so neighbouring playlists are as far apart as possible."""
    if count == 1:
        return [dict(SINGLE_COLOUR)]

    def pick(steps: list[str], i: int) -> str:
        return steps[round(i * (len(steps) - 1) / (count - 1))]

    return [{"light": pick(LIGHT_RAMP, i), "dark": pick(DARK_RAMP, i)} for i in range(count)]


def _is_emoji_char(ch: str) -> bool:
    # Keycaps like 1️⃣ are a plain digit plus these two, so they count too.
    return unicodedata.category(ch) == "So" or ch in "\u20e3\ufe0f"


def marker(name: str, rank: int) -> str:
    """The emoji a playlist is known by - the last word of its name with an
    emoji in it ("one 1️⃣" -> "1️⃣") - or its rank (1 = best) if it has none."""
    for word in reversed(name.split()):
        if any(_is_emoji_char(ch) for ch in word):
            return word
    return str(rank)


def build(
    sp: Spotify,
    main_playlist_id: str,
    colour_playlist_ids: list[str],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """Returns {"main_name", "colour_playlists": [{"name", "marker", "marker_is_number", "count", "colour"}],
    "bands": [{"position", "name", "artists", "album", "tiers"}],
    "none_count", "multi_count", "none_colour"}. A band's tiers are the
    indexes (best first) of the colour playlists the song is in.

    Songs are matched across releases with Duplicate Finder's rule, as in
    Ranking Check, so a remaster in the main playlist still counts as being
    in the colour playlist that has the single."""
    if not 1 <= len(colour_playlist_ids) <= MAX_COLOUR_PLAYLISTS:
        raise ValueError(f"pick 1 to {MAX_COLOUR_PLAYLISTS} playlists to colour by")
    playlists = playlist_cache_module.get_playlists(
        sp, colour_playlist_ids + [main_playlist_id], cancel_check
    )
    check_cancelled(cancel_check)

    main = playlists[main_playlist_id]
    all_tracks: dict[str, dict] = {}
    for playlist_id in colour_playlist_ids + [main_playlist_id]:
        for track in playlists[playlist_id]["tracks"]:
            all_tracks.setdefault(track["uri"], track)
    song_number = ranking_check._song_groups(list(all_tracks.values()))

    tiers_of: dict[int, set[int]] = {}
    for index, playlist_id in enumerate(colour_playlist_ids):
        for track in playlists[playlist_id]["tracks"]:
            tiers_of.setdefault(song_number[track["uri"]], set()).add(index)

    bands = []
    counts = [0] * len(colour_playlist_ids)
    none_count = multi_count = 0
    for position, track in enumerate(main["tracks"], start=1):
        tiers = sorted(tiers_of.get(song_number[track["uri"]], ()))
        for tier in tiers:
            counts[tier] += 1
        if not tiers:
            none_count += 1
        elif len(tiers) > 1:
            multi_count += 1
        bands.append(
            {
                "position": position,
                "name": track["name"],
                "artists": track["artists"],
                "album": track.get("album") or "",
                "tiers": tiers,
            }
        )

    palette = colours(len(colour_playlist_ids))
    logger.info(
        "playlist overlap: %d song(s) in '%s' across %d colour playlist(s)",
        len(bands),
        main["name"],
        len(colour_playlist_ids),
    )
    return {
        "main_name": main["name"],
        "colour_playlists": [
            {
                "name": playlists[playlist_id]["name"],
                "marker": marker(playlists[playlist_id]["name"], i + 1),
                "marker_is_number": marker(playlists[playlist_id]["name"], i + 1) == str(i + 1),
                "count": counts[i],
                "colour": palette[i],
            }
            for i, playlist_id in enumerate(colour_playlist_ids)
        ],
        "bands": bands,
        "none_count": none_count,
        "multi_count": multi_count,
        "none_colour": NONE_COLOUR,
    }
