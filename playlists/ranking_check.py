from __future__ import annotations

import logging
from datetime import datetime, timezone
from statistics import median, quantiles

from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
import lastfm.forgotten_favourites as forgotten_favourites
import lastfm.scrobble_history as scrobble_history
import playlists.duplicates as duplicates
import playlists.playlist_cache as playlist_cache_module
import playlists.playlist_filter as playlist_filter

logger = logging.getLogger(__name__)

# Ranking Check: compares the user's ranking playlists (best first, e.g.
# "one" to "four"), their yearly playlist (ranked best to worst) and their
# scrobbles, and suggests songs that may belong in a different ranking
# playlist (or a different spot in the yearly playlist). Read-only apart
# from move_song, which the user triggers per song.

# "plays" is how the results page describes which plays counted.
PERIODS = [
    {"key": "7d", "label": "7 days", "days": 7, "plays": "plays in the last 7 days"},
    {"key": "30d", "label": "30 days", "days": 30, "plays": "plays in the last 30 days"},
    {"key": "90d", "label": "90 days", "days": 90, "plays": "plays in the last 90 days"},
    {"key": "180d", "label": "180 days", "days": 180, "plays": "plays in the last 180 days"},
    {"key": "12m", "label": "12 months", "plays": "plays in the last 12 months"},
    {"key": "year", "label": "This year", "plays": "plays this year"},
    {"key": "all", "label": "All time", "plays": "all-time plays"},
]
DEFAULT_PERIOD = "year"

# Zero plays in a week says nothing about whether a song still deserves its
# ranking, so moving-down suggestions only start once the period is long
# enough for "rarely played" to mean something.
DEMOTION_MIN_DAYS = 90

# A song first played more recently than this isn't judged on plays at
# all: a new album's first-week binge out-plays nearly everything, and says
# little about where the songs will settle.
MIN_AGE_DAYS = 30

# A ranking needs at least this many new songs played in the period for
# its quartiles to mean anything as a scale.
MIN_GROUP_SONGS = 4

# A song heard before the period moves up a ranking once it reaches this
# share of a typical new song's plays there (and out-plays three quarters
# of the older songs there). 3/4 rather than 90%: typical counts are small
# (3, 5, 9, 18), so 90% rounds up to needing the typical count itself.
OLDER_SONG_SHARE = 0.75

# Short periods leave most songs on 1 or 2 plays, where "more than most"
# is a coin toss - so a song needs at least this many plays in the period
# to be suggested for moving up on plays alone.
MIN_PROMOTION_PLAYS = 3

# Yearly position vs plays this year: only flagged when the two are more
# than this fraction of the yearly playlist apart, and only once the
# playlist is long enough for a fraction of it to mean anything.
FAR_APART_FRACTION = 0.25
MIN_YEAR_SONGS = 8

# New songs land at the bottom of the yearly playlist (Playlist Filter
# appends them in the Cascade) until they're dragged up into place, so a
# run of songs at the very bottom added this recently is treated as not
# ranked yet and left out of both yearly checks. Stops at the first older
# song, so a new song already dragged into place still counts.
UNRANKED_TAIL_DAYS = 30

# Strongest first, so the top of the list is what's worth a look; the rest
# would mostly be one-flag, one-ranking-off suggestions.
MAX_RESULTS = 100

# How strong a song's inconsistency is: each flag scores 1-3 points for how
# far past its line it is (FLAG_POINTS_MAX), and the song's meter is the
# total, capped at METER_MAX, so a song several checks agree on fills more
# of it. A song in more than one ranking playlist, or in none, is always 1 -
# it's either the case or it isn't.
FLAG_POINTS_MAX = 3
METER_MAX = 5
STRENGTH_LABELS = [(4, "Strong"), (3, "Moderate"), (1, "Mild")]

# Yearly order vs ranking playlists: an extra point each for being at least
# this far (as a fraction of the yearly playlist) past the edge of the
# song's own ranking's section.
YEAR_DEPTH_FRACTIONS = (0.05, 0.15)

# Plays: an extra point for out-playing the bar by this much (moving up) or
# falling this far under it (moving down).
PLAYS_FAR_PAST = 2


def _period(key: str) -> dict:
    return next((p for p in PERIODS if p["key"] == key), None) or next(
        p for p in PERIODS if p["key"] == DEFAULT_PERIOD
    )


def period_start(key: str, now: datetime) -> int:
    """Unix timestamp the period starts at (0 for all time)."""
    period = _period(key)
    if period["key"] == "all":
        return 0
    if period["key"] == "year":
        return int(now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    if period["key"] == "12m":
        return int(forgotten_favourites.months_ago(now, 12).timestamp())
    return int(now.timestamp()) - period["days"] * 86400


def _signature(artists: str, name: str) -> tuple[str, str]:
    # Forgotten Favourites' loose "same song" key (primary artist, title
    # without version suffixes) - only used to match scrobbles, which have
    # no length to tell a remix from the original by.
    return playlist_filter._primary_artist(artists), playlist_filter._normalize_title(name)


def _exact_key(artists: str, name: str) -> tuple[str, str]:
    return playlist_filter._primary_artist(artists), name.strip().lower()


def _song_groups(tracks: list[dict]) -> dict[str, int]:
    """Maps each uri to a song number, putting the same song on different
    releases (Duplicate Finder's rule: same artists and title, lengths
    within a couple of seconds, not a live/acoustic take) under one number.
    Numbered in order of first appearance."""
    cluster_of: dict[str, int] = {}
    for n, cluster in enumerate(duplicates._release_clusters(tracks)):
        for track in cluster:
            cluster_of[track["uri"]] = n
    groups: dict[str, int] = {}
    song_of_cluster: dict[int, int] = {}
    next_song = 0
    for track in tracks:
        cluster = cluster_of.get(track["uri"])
        if cluster is not None and cluster in song_of_cluster:
            groups[track["uri"]] = song_of_cluster[cluster]
            continue
        groups[track["uri"]] = next_song
        if cluster is not None:
            song_of_cluster[cluster] = next_song
        next_song += 1
    return groups


def _parse_added_at(added_at: str | None) -> int | None:
    if not added_at:
        return None
    try:
        return int(datetime.strptime(added_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _points(base: float, extra: int = 0) -> int:
    return max(1, min(FLAG_POINTS_MAX, int(base) + extra))


def _strength_label(score: int) -> str:
    return next(label for floor, label in STRENGTH_LABELS if score >= floor)


def _times(ratio: float) -> str:
    """"3×", or "2.5×" - one decimal only where it matters."""
    return f"{ratio:.0f}×" if ratio >= 10 or abs(ratio - round(ratio)) < 0.05 else f"{ratio:.1f}×"


def _fit_year_order(sequence: list[int | None], tier_count: int) -> list[int]:
    """The best non-decreasing tier for each yearly position - i.e. three
    cut points splitting the yearly playlist into the rankings - minimising
    how many tiers off each song's current ranking is. None (a song in no
    ranking playlist) costs nothing either way, so it just takes whatever
    tier its neighbours fit.

    dp[i][k] is the lowest total cost of positions 0..i with position i
    fitted to tier k; since fits can only get worse going down the list,
    position i-1 can be any tier <= k."""
    if not sequence:
        return []
    dp: list[list[int]] = []
    back: list[list[int]] = []
    for i, tier in enumerate(sequence):
        costs = [0 if tier is None else abs(tier - k) for k in range(tier_count)]
        if i == 0:
            dp.append(costs)
            back.append([0] * tier_count)
            continue
        row, row_back = [], []
        best_j, best = 0, dp[i - 1][0]
        for k in range(tier_count):
            # <= so a tie keeps the previous song in the same tier as this
            # one, rather than putting a cut point earlier than it needs to.
            if dp[i - 1][k] <= best:
                best_j, best = k, dp[i - 1][k]
            row.append(costs[k] + best)
            row_back.append(best_j)
        dp.append(row)
        back.append(row_back)

    k = min(range(tier_count), key=lambda t: dp[-1][t])
    fitted = [0] * len(sequence)
    for i in range(len(sequence) - 1, -1, -1):
        fitted[i] = k
        k = back[i][k]
    return fitted


def find_inconsistencies(
    sp: Spotify,
    tiers: list[dict],
    year_playlist_id: str,
    period_key: str,
    only_added_in_period: bool,
    dismissed: dict[str, dict],
    cancel_check: CancelCheck | None = None,
) -> dict:
    """tiers: [{"id", "name"}, ...] best first. dismissed: active
    dismissals keyed by song key (see ranking_dismissals); a dismissal only
    hides a song while it's still in the same ranking playlist(s) it was in
    when dismissed.

    Returns {"results": [...], "tiers",
    "year_playlist_name", "period_label", "period_plays", "only_added_in_period",
    "demotions_enabled", "year_check_enabled", "plays_check_enabled",
    "plays_scale", "unranked_count", "year_sections", "year_length",
    "meter_max", "dismissed_hidden",
    "stale_dismissals"}. Each result is a song with at least one flag,
    strongest first - all of them; view() picks what the page shows."""
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    period = _period(period_key)
    start_ts = period_start(period["key"], now)
    year_start_ts = period_start("year", now)
    demotions_enabled = period["key"] == "all" or (now_ts - start_ts) / 86400 >= DEMOTION_MIN_DAYS

    # Topping up first means this week's plays count; the first ever run
    # builds the whole store (shared with every other Last.fm tool).
    scrobble_history.sync(cancel_check)
    check_cancelled(cancel_check)

    playlists = playlist_cache_module.get_playlists(
        sp, [t["id"] for t in tiers] + [year_playlist_id], cancel_check
    )
    tier_names = [playlists[t["id"]]["name"] for t in tiers]
    year_playlist = playlists[year_playlist_id]
    year_name = year_playlist["name"]

    # Every distinct track, ranking playlists best first, then the yearly
    # playlist - so the first release seen of each song is the one in the
    # best ranking playlist it's in, which is the one a move keeps.
    all_tracks: dict[str, dict] = {}
    for playlist_id in [t["id"] for t in tiers] + [year_playlist_id]:
        for track in playlists[playlist_id]["tracks"]:
            all_tracks.setdefault(track["uri"], track)
    song_number = _song_groups(list(all_tracks.values()))

    songs: dict[int, dict] = {}

    def song_for(track: dict) -> dict:
        number = song_number[track["uri"]]
        song = songs.get(number)
        if song is None:
            song = songs[number] = {
                "key": track["uri"],
                "uri": track["uri"],
                "name": track["name"],
                "artists": track["artists"],
                "album": track.get("album") or "",
                "explicit": bool(track.get("explicit")),
                "is_local": bool(track.get("is_local")),
                "tier_uris": {},
                "added_at": {},
                "year_position": None,
                "first_ts": None,
                "plays": 0,
                "plays_ytd": 0,
                "flags": [],
                "move_tiers": [],
                "reorder": [],
            }
        return song

    for index, tier in enumerate(tiers):
        for track in playlists[tier["id"]]["tracks"]:
            song = song_for(track)
            uris = song["tier_uris"].setdefault(index, [])
            if track["uri"] not in uris:
                uris.append(track["uri"])
            added = _parse_added_at(track.get("added_at"))
            if added is not None and (index not in song["added_at"] or added < song["added_at"][index]):
                song["added_at"][index] = added

    year_tracks = year_playlist["tracks"]
    ranked_count = len(year_tracks)
    while ranked_count > 0:
        added = _parse_added_at(year_tracks[ranked_count - 1].get("added_at"))
        if added is None or now_ts - added > UNRANKED_TAIL_DAYS * 86400:
            break
        ranked_count -= 1

    year_songs: list[dict] = []
    for position, track in enumerate(year_tracks, start=1):
        song = song_for(track)
        if song["year_position"] is None:
            song["year_position"] = position
            if position <= ranked_count:
                year_songs.append(song)

    logger.info(
        "ranking check: %d song(s) across %d ranking playlist(s) and '%s'",
        len(songs),
        len(tiers),
        year_name,
    )

    # Scrobbles have names but no length, so they're matched on the exact
    # name first, then on the title without version suffixes - but only
    # when that points at a single song, so a remix's plays don't land on
    # the original (or the other way round) when both are in a playlist.
    by_exact: dict[tuple[str, str], dict] = {}
    by_signature: dict[tuple[str, str], list[dict]] = {}
    for uri, track in all_tracks.items():
        song = songs[song_number[uri]]
        by_exact.setdefault(_exact_key(track["artists"], track["name"]), song)
        matches = by_signature.setdefault(_signature(track["artists"], track["name"]), [])
        if song not in matches:
            matches.append(song)
    for row in scrobble_history.track_plays([start_ts, year_start_ts]):
        song = by_exact.get(_exact_key(row["artist"], row["name"]))
        if song is None:
            matches = by_signature.get(_signature(row["artist"], row["name"]), [])
            song = matches[0] if len(matches) == 1 else None
        if song is None:
            continue
        window_plays, ytd_plays = row["plays"]
        song["plays"] += window_plays or 0
        song["plays_ytd"] += ytd_plays or 0
        if song["first_ts"] is None or row["first"] < song["first_ts"]:
            song["first_ts"] = row["first"]
    for song in songs.values():
        song["tier"] = min(song["tier_uris"]) if song["tier_uris"] else None
    check_cancelled(cancel_check)

    def old_enough(song: dict) -> bool:
        return song["first_ts"] is not None and now_ts - song["first_ts"] >= MIN_AGE_DAYS * 86400

    def suggest_move(song: dict, tier: int) -> None:
        if tier not in song["move_tiers"]:
            song["move_tiers"].append(tier)

    # Songs in more than one ranking playlist: offer to keep each one.
    for song in songs.values():
        if len(song["tier_uris"]) > 1:
            names = ", ".join(tier_names[i] for i in sorted(song["tier_uris"]))
            song["flags"].append(
                {"check": "multiple", "strength": 1, "points": 1, "chart": None,
                 "text": f"In more than one ranking playlist: {names}"}
            )
            for i in sorted(song["tier_uris"]):
                suggest_move(song, i)

    # 1. Yearly order vs ranking playlists. Fits only ever get worse going
    # down the list, so each ranking's region is one unbroken section.
    fitted = _fit_year_order([s["tier"] for s in year_songs], len(tiers))
    region: dict[int, list[int]] = {}
    for song, fit in zip(year_songs, fitted):
        region.setdefault(fit, []).append(song["year_position"])
    year_length = year_songs[-1]["year_position"] if year_songs else 0
    year_sections = [
        {"tier": tier, "name": tier_names[tier], "start": min(positions), "end": max(positions)}
        for tier, positions in sorted(region.items())
    ]
    for song, fit in zip(year_songs, fitted):
        position = song["year_position"]
        chart = {"type": "strip", "position": position}
        if song["tier"] is None:
            song["flags"].append(
                {
                    "check": "year_order",
                    "strength": 1,
                    "points": 1,
                    "chart": chart,
                    "text": f"Not in a ranking playlist. At #{position} in {year_name}, "
                    f"it sits among {tier_names[fit]} songs",
                }
            )
            suggest_move(song, fit)
        elif fit != song["tier"]:
            tiers_off = abs(fit - song["tier"])
            own_region = region.get(song["tier"])
            # How far past the edge of its own ranking's section it is.
            if not own_region:
                text = (
                    f"At #{position} in {year_name}, it sits among {tier_names[fit]} songs - "
                    f"no {tier_names[song['tier']]} songs are ranked there"
                )
                extra = 0
            else:
                above = fit < song["tier"]
                edge = min(own_region) if above else max(own_region)
                depth = abs(edge - position)
                text = (
                    f"At #{position} in {year_name} - {_plural(depth, 'place')} "
                    f"{'above where' if above else 'below where'} the {tier_names[song['tier']]} section "
                    f"{'starts' if above else 'ends'} (#{edge})"
                )
                extra = sum(1 for f in YEAR_DEPTH_FRACTIONS if depth >= f * year_length)
            song["flags"].append(
                {
                    "check": "year_order",
                    "strength": tiers_off,
                    "points": _points(tiers_off, extra),
                    "chart": chart,
                    "text": text,
                }
            )
            suggest_move(song, fit)
            if own_region:
                song["reorder"].append(
                    f"To keep it in {tier_names[song['tier']]}, it fits around "
                    f"#{min(own_region)}–{max(own_region)} in {year_name} (currently #{position})"
                )

    # 2. Plays vs ranking playlists. Each ranking playlist spans years and
    # most of it isn't played in any given period, so only songs played in
    # the period are judged (no plays isn't evidence either way).
    #
    # What each ranking looks like in plays comes from songs first heard in
    # the period: new songs get dozens of plays and line up with the
    # rankings. A new song is suggested for a higher ranking when it
    # out-plays three quarters of the new songs there.
    #
    # Songs heard before the period mostly get one or two plays whichever
    # ranking they're in. Compared only with each other, any older song with
    # three plays "deserved" the top ranking; held to the new songs' bar,
    # none ever qualified. So an older song moves up one ranking at a time,
    # when it out-plays three quarters of the older songs there and reaches
    # OLDER_SONG_SHARE of a typical new song there - the "I like it more
    # now" case, at a level that still means something. Only new songs are
    # suggested for moving down - an older song getting fewer plays than
    # new ones is just how listening goes, not a sign it's ranked too high.
    def is_new(song: dict) -> bool:
        return song["first_ts"] >= start_ts

    scale_label = "song you've played" if start_ts == 0 else "song you first heard in the period"
    judged = [s for s in songs.values() if s["tier"] is not None and s["plays"] > 0 and old_enough(s)]
    # The scale counts new songs too recent to be judged themselves, or a
    # period of 30 days or less would have no new songs to build it from.
    scale_plays: dict[int, list[int]] = {}
    older_plays: dict[int, list[int]] = {}
    for song in songs.values():
        if song["tier"] is not None and song["plays"] > 0:
            for i in song["tier_uris"]:
                (scale_plays if is_new(song) else older_plays).setdefault(i, []).append(song["plays"])
    # Quartiles rather than the middle song: "more than half" flags
    # hundreds of songs on noise alone.
    scale_quartiles = {i: quantiles(plays, n=4) for i, plays in scale_plays.items() if len(plays) >= MIN_GROUP_SONGS}
    scale_median = {i: median(plays) for i, plays in scale_plays.items() if len(plays) >= MIN_GROUP_SONGS}
    scale_typical = {i: round(m) for i, m in scale_median.items()}
    older_quartiles = {i: quantiles(plays, n=4) for i, plays in older_plays.items() if len(plays) >= MIN_GROUP_SONGS}

    def older_bar(tier: int) -> float | None:
        """Plays an older song needs to beat to move up into tier."""
        if tier not in scale_median:
            return None
        return max(older_quartiles[tier][2] if tier in older_quartiles else 0, OLDER_SONG_SHARE * scale_median[tier])
    # Moving up needs a ranking above to compare with, so with fewer than
    # two rankings on the scale the plays check can't suggest anything.
    plays_check_enabled = len(scale_quartiles) >= 2

    def plays_chart(plays: int, target: int) -> dict:
        """The song's plays against a typical song from the suggested ranking."""
        return {
            "type": "plays",
            "plays": plays,
            "typical": {"name": tier_names[target], "plays": scale_typical[target]},
        }

    for song in judged:
        tier, plays = song["tier"], song["plays"]
        target = tier
        if plays >= MIN_PROMOTION_PLAYS and is_new(song):
            # Walk up while it out-plays three quarters of the next ranking up.
            while target > 0 and target - 1 in scale_quartiles and plays > scale_quartiles[target - 1][2]:
                target -= 1
        elif plays >= MIN_PROMOTION_PLAYS and tier > 0:
            bar = older_bar(tier - 1)
            if bar is not None and plays > bar:
                target = tier - 1
        if target < tier:
            typical = scale_typical[target]
            if is_new(song):
                far_past = plays >= PLAYS_FAR_PAST * scale_quartiles[target][2]
                text = (
                    f"{_plural(plays, 'play')} in the period - "
                    + (f"{_times(plays / typical)} a typical " if typical else "a typical ")
                    + f"{tier_names[target]} {scale_label} ({typical})"
                )
            else:
                far_past = plays >= scale_median[target]
                text = (
                    f"{_plural(plays, 'play')} in the period for a song you'd heard before it - "
                    + (f"{_times(plays / typical)} a typical " if plays >= typical else "close to a typical ")
                    + f"{tier_names[target]} {scale_label} ({typical})"
                )
            song["flags"].append(
                {
                    "check": "plays",
                    "played_more": True,
                    "tier": target,
                    "strength": tier - target,
                    "points": _points(tier - target, int(far_past)),
                    "chart": plays_chart(plays, target),
                    "text": text,
                }
            )
            suggest_move(song, target)
        elif demotions_enabled and is_new(song):
            # Walk down while three quarters of the next ranking down out-play it.
            while (
                target < len(tiers) - 1
                and target + 1 in scale_quartiles
                and plays < scale_quartiles[target + 1][0]
            ):
                target += 1
            if target > tier:
                typical = scale_typical[target]
                far_under = plays * PLAYS_FAR_PAST <= scale_quartiles[target][0]
                song["flags"].append(
                    {
                        "check": "plays",
                        "tier": target,
                        "strength": target - tier,
                        "points": _points(target - tier, int(far_under)),
                        "chart": plays_chart(plays, target),
                        "text": f"{_plural(plays, 'play')} in the period - a typical "
                        f"{tier_names[target]} {scale_label} has {typical}",
                    }
                )
                suggest_move(song, target)

    # 3. Yearly order vs plays this year (always since 1 January), among
    # songs played long enough ago to judge. Ties in plays keep their
    # yearly order, so equal songs don't count as apart.
    year_judged = [s for s in year_songs if old_enough(s)]
    year_check_enabled = len(year_judged) >= MIN_YEAR_SONGS
    if year_check_enabled:
        by_plays = sorted(range(len(year_judged)), key=lambda i: (-year_judged[i]["plays_ytd"], i))
        threshold = len(year_judged) * FAR_APART_FRACTION
        for plays_rank, ordinal in enumerate(by_plays):
            song = year_judged[ordinal]
            gap = ordinal - plays_rank
            if abs(gap) <= threshold:
                continue
            # Where its plays would put it, as a position in the playlist.
            plays_position = year_judged[plays_rank]["year_position"]
            apart = abs(gap) / threshold
            song["flags"].append(
                {
                    "check": "year_plays",
                    "played_more": gap > 0,
                    "strength": apart,
                    "points": 1 if apart < 1.5 else 2 if apart < 2 else 3,
                    "chart": {"type": "strip", "position": song["year_position"]},
                    "text": f"#{song['year_position']} in {year_name}, but #{plays_position} by plays this year - "
                    f"{_plural(abs(song['year_position'] - plays_position), 'place')} apart "
                    f"({_plural(song['plays_ytd'], 'play')})",
                }
            )

    results = []
    dismissed_hidden = 0
    stale_dismissals = []
    for song in songs.values():
        current_ids = sorted(tiers[i]["id"] for i in song["tier_uris"])
        dismissal = dismissed.get(song["key"])
        if dismissal is not None and dismissal.get("tier_ids") != current_ids:
            # Moved since it was dismissed, so the dismissal no longer applies.
            stale_dismissals.append(song["key"])
            dismissal = None
        if not song["flags"]:
            continue
        if only_added_in_period and not any(ts >= start_ts for ts in song["added_at"].values()):
            continue
        if dismissal is not None:
            dismissed_hidden += 1
            continue

        score = min(METER_MAX, sum(f["points"] for f in song["flags"]))
        moves = []
        if not song["is_local"]:
            for tier in song["move_tiers"]:
                if not song["tier_uris"]:
                    label = f"Add to {tier_names[tier]}"
                elif set(song["tier_uris"]) == {tier}:
                    continue
                elif tier in song["tier_uris"]:
                    label = f"Keep only in {tier_names[tier]}"
                else:
                    label = f"Move to {tier_names[tier]}"
                moves.append({"tier": tier, "label": label})

        results.append(
            {
                "key": song["key"],
                "uri": song["uri"],
                "name": song["name"],
                "artists": song["artists"],
                "album": song["album"],
                "explicit": song["explicit"],
                "is_local": song["is_local"],
                "tiers": [
                    {
                        "index": i,
                        "name": tier_names[i],
                        "added": datetime.fromtimestamp(song["added_at"][i], timezone.utc).strftime("%Y-%m-%d")
                        if i in song["added_at"]
                        else None,
                    }
                    for i in sorted(song["tier_uris"])
                ],
                "tier_ids": current_ids,
                "tier_uris": {i: list(uris) for i, uris in song["tier_uris"].items()},
                "year_position": song["year_position"],
                "plays": song["plays"],
                "plays_ytd": song["plays_ytd"],
                "flags": [
                    {
                        "check": f["check"],
                        "text": f["text"],
                        "points": f["points"],
                        "chart": f["chart"],
                        # Listened to more than its rankings suggest - what
                        # the results page's "played more" view keeps.
                        "played_more": f.get("played_more", False),
                        "tier": f.get("tier"),
                    }
                    for f in song["flags"]
                ],
                "moves": moves,
                "reorder": song["reorder"],
                "status": None,
                "score": score,
                "strength_label": _strength_label(score),
                "_sort": (-score, -len(song["flags"]), -sum(f["strength"] for f in song["flags"]), -song["plays"]),
            }
        )

    results.sort(key=lambda r: r["_sort"])
    for r in results:
        del r["_sort"]

    logger.info("ranking check: %d song(s) flagged", len(results))
    return {
        "results": results,
        "tiers": [{"id": t["id"], "name": name} for t, name in zip(tiers, tier_names)],
        "year_playlist_name": year_name,
        "period_label": period["label"],
        "period_plays": period["plays"],
        "only_added_in_period": only_added_in_period,
        "demotions_enabled": demotions_enabled,
        "year_check_enabled": year_check_enabled,
        "plays_check_enabled": plays_check_enabled,
        "plays_scale": [{"name": tier_names[i], "plays": scale_typical[i]} for i in sorted(scale_typical)],
        "unranked_count": len(year_tracks) - ranked_count,
        "year_sections": year_sections,
        "year_length": year_length,
        "meter_max": METER_MAX,
        "dismissed_hidden": dismissed_hidden,
        "stale_dismissals": stale_dismissals,
    }


def view(results: list[dict], played_more_only: bool) -> dict:
    """What the results page shows from find_inconsistencies' results:
    {"results": up to MAX_RESULTS, strongest first, "total": how many there
    are before that cap}.

    played_more_only keeps just the songs listened to more than their
    rankings suggest - the plays check saying move up, or played more this
    year than the yearly position suggests - with only those flags, scored
    and sorted on them alone and offering only the moves up they suggest.
    Each is a copy, so a move or dismissal marked on the scan's own result
    shows in either view."""
    if not played_more_only:
        return {"results": results[:MAX_RESULTS], "total": len(results)}
    shown = []
    for result in results:
        flags = [f for f in result["flags"] if f["played_more"]]
        if not flags:
            continue
        up_tiers = {f["tier"] for f in flags if f["tier"] is not None}
        score = min(METER_MAX, sum(f["points"] for f in flags))
        shown.append(
            {
                **result,
                "flags": flags,
                "moves": [m for m in result["moves"] if m["tier"] in up_tiers],
                "reorder": [],
                "score": score,
                "strength_label": _strength_label(score),
            }
        )
    shown.sort(key=lambda r: (-r["score"], -len(r["flags"]), -r["plays"]))
    return {"results": shown[:MAX_RESULTS], "total": len(shown)}


def move_song(sp: Spotify, result: dict, tiers: list[dict], tier_index: int) -> str | None:
    """Puts a flagged song in tiers[tier_index] and takes it out of every
    other ranking playlist it's in. Adds first, so a failed add never
    leaves the song in no ranking playlist at all; the removal is logged to
    Removal History. Returns that history entry's id (None if nothing was
    removed)."""
    if result["is_local"]:
        raise ValueError("local files can't be added to a playlist through the Web API")
    target = tiers[tier_index]
    if tier_index not in result["tier_uris"]:
        logger.info("adding '%s' to '%s'", result["name"], target["name"])
        playlist_filter.add_tracks_to_playlist(sp, target["id"], [result["uri"]])
    removals = [
        {"playlist_id": tiers[i]["id"], "uri": uri}
        for i, uris in result["tier_uris"].items()
        if i != tier_index
        for uri in uris
    ]
    if not removals:
        return None
    return duplicates.remove_from_playlists(sp, removals, source="Ranking Check")
