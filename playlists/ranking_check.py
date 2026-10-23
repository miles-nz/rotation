from __future__ import annotations

import logging
from datetime import datetime, timezone
from statistics import quantiles

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

# A ranking needs at least this many songs in a comparison group for its
# quartiles to mean anything.
MIN_GROUP_SONGS = 4

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


def _percent(count: int, total: int) -> int:
    """count as a whole percentage of total, never rounded down to 0."""
    return max(round(100 * count / total), 1) if total else 0


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

    Returns {"results": [...], "total_flagged", "tiers",
    "year_playlist_name", "period_label", "period_plays", "only_added_in_period",
    "demotions_enabled", "year_check_enabled", "unranked_count",
    "dismissed_hidden", "stale_dismissals"}. Each result is a song with at
    least one flag, strongest first, capped at MAX_RESULTS."""
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
                {"check": "multiple", "strength": 1, "text": f"In more than one ranking playlist: {names}"}
            )
            for i in sorted(song["tier_uris"]):
                suggest_move(song, i)

    # 1. Yearly order vs ranking playlists.
    fitted = _fit_year_order([s["tier"] for s in year_songs], len(tiers))
    region: dict[int, list[int]] = {}
    for song, fit in zip(year_songs, fitted):
        region.setdefault(fit, []).append(song["year_position"])
    for song, fit in zip(year_songs, fitted):
        position = song["year_position"]
        if song["tier"] is None:
            song["flags"].append(
                {
                    "check": "year_order",
                    "strength": 1,
                    "text": f"Not in a ranking playlist. At #{position} in {year_name}, "
                    f"it sits among {tier_names[fit]} songs",
                }
            )
            suggest_move(song, fit)
        elif fit != song["tier"]:
            song["flags"].append(
                {
                    "check": "year_order",
                    "strength": abs(fit - song["tier"]),
                    "text": f"At #{position} in {year_name}, it sits among {tier_names[fit]} songs",
                }
            )
            suggest_move(song, fit)
            own_region = region.get(song["tier"])
            if own_region:
                song["reorder"].append(
                    f"To keep it in {tier_names[song['tier']]}, it fits around "
                    f"#{min(own_region)}–{max(own_region)} in {year_name} (currently #{position})"
                )

    # 2. Plays vs ranking playlists. Each ranking playlist spans years and
    # most of it isn't played in any given period, so only songs played in
    # the period are judged (no plays isn't evidence either way), and each
    # is compared against played songs from the same group: ones first
    # heard during the period, or ones first heard before it and come back
    # to. Kept apart because they behave nothing alike - new songs get
    # dozens of plays and line up with the rankings, while songs come back
    # to mostly get one or two whichever ranking they're in, so a returning
    # song with ten is exactly the "I like it more now" case.
    def group(song: dict) -> str:
        return "new" if song["first_ts"] >= start_ts else "returning"

    group_labels = {
        "new": "songs you've played" if start_ts == 0 else "songs you first heard in the period",
        "returning": "older songs you played in the period",
    }
    judged = [s for s in songs.values() if s["tier"] is not None and s["plays"] > 0 and old_enough(s)]
    group_plays: dict[tuple[str, int], list[int]] = {}
    for song in judged:
        for i in song["tier_uris"]:
            group_plays.setdefault((group(song), i), []).append(song["plays"])
    # Quartiles rather than the middle song: "more than half" flags
    # hundreds of songs on noise alone.
    group_quartiles = {
        key: quantiles(plays, n=4) for key, plays in group_plays.items() if len(plays) >= MIN_GROUP_SONGS
    }

    for song in judged:
        tier, plays, song_group = song["tier"], song["plays"], group(song)
        target = tier
        if plays >= MIN_PROMOTION_PLAYS:
            # Walk up while it out-plays three quarters of the next ranking up.
            while target > 0 and plays > group_quartiles.get((song_group, target - 1), [0, 0, float("inf")])[2]:
                target -= 1
        if target < tier:
            others = group_plays[(song_group, target)]
            song["flags"].append(
                {
                    "check": "plays",
                    "strength": tier - target,
                    "text": f"{_plural(plays, 'play')} in the period - more than "
                    f"{_percent(sum(1 for p in others if p < plays), len(others))}% of the "
                    f"{tier_names[target]} {group_labels[song_group]}",
                }
            )
            suggest_move(song, target)
        elif demotions_enabled:
            # Walk down while three quarters of the next ranking down out-play it.
            while (
                target < len(tiers) - 1
                and plays < group_quartiles.get((song_group, target + 1), [0])[0]
            ):
                target += 1
            if target > tier:
                others = group_plays[(song_group, target)]
                song["flags"].append(
                    {
                        "check": "plays",
                        "strength": target - tier,
                        "text": f"{_plural(plays, 'play')} in the period - fewer than "
                        f"{_percent(sum(1 for p in others if p > plays), len(others))}% of the "
                        f"{tier_names[target]} {group_labels[song_group]}",
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
            low = year_judged[max(0, plays_rank - 2)]["year_position"]
            high = year_judged[min(len(year_judged) - 1, plays_rank + 2)]["year_position"]
            song["flags"].append(
                {
                    "check": "year_plays",
                    "strength": abs(gap) / threshold,
                    "text": f"Played {'more' if gap > 0 else 'less'} this year than its spot in {year_name} "
                    f"suggests ({_plural(song['plays_ytd'], 'play')})",
                }
            )
            song["reorder"].append(
                f"By plays this year it fits around #{low}–{high} in {year_name} "
                f"(currently #{song['year_position']})"
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
                "flags": [{"check": f["check"], "text": f["text"]} for f in song["flags"]],
                "moves": moves,
                "reorder": song["reorder"],
                "status": None,
                "_sort": (-len(song["flags"]), -sum(f["strength"] for f in song["flags"]), -song["plays"]),
            }
        )

    results.sort(key=lambda r: r["_sort"])
    for r in results:
        del r["_sort"]

    logger.info("ranking check: %d song(s) flagged", len(results))
    return {
        "results": results[:MAX_RESULTS],
        "total_flagged": len(results),
        "tiers": [{"id": t["id"], "name": name} for t, name in zip(tiers, tier_names)],
        "year_playlist_name": year_name,
        "period_label": period["label"],
        "period_plays": period["plays"],
        "only_added_in_period": only_added_in_period,
        "demotions_enabled": demotions_enabled,
        "year_check_enabled": year_check_enabled,
        "unranked_count": len(year_tracks) - ranked_count,
        "dismissed_hidden": dismissed_hidden,
        "stale_dismissals": stale_dismissals,
    }


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
