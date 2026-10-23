# Rotation

A small local web app with a homepage listing Spotify library-management
functions. Currently available:

- **Liked Songs Sync** - keeps your Liked Songs in sync with the union of
  two playlists: reads both playlists, reads your current Liked Songs, adds
  what's missing, and removes liked tracks that are no longer in either
  playlist.
- **Duplicate Finder** - pick two or more playlists and find tracks that
  appear in more than one of them, with the date each copy was added, so
  you can select which playlist(s) to remove each duplicate from. It also
  catches the same song on different releases (e.g. the single and the
  album cut): same artists, same title once version suffixes like
  "- Remastered 2011" are stripped, and lengths within 2 seconds.
- **Track Search** - search Spotify's catalog and preview any track with
  the embedded player.
- **All Playlists** - lists every playlist in your library; click a name or
  ID to copy it (handy for filling in the `DEFAULT_*` variables in `.env`).
- **Playlist Filter** - pick one or more playlists and one or more
  conditions (added date, release year, popularity, duration, explicit,
  artist name, track name, or genre), and add every matching track to
  another playlist - an existing one or a brand new one.
  Flags tracks already in the destination, and unchecks any that look like
  a different version (remaster, live, single edit, etc.) of a track already
  there.
- **Playlist Cleanup** - pick a playlist and a rule for what to keep (added
  date, release year, popularity, duration, explicit, artist name, track
  name, or genre); everything that doesn't match gets removed.
- **Playlist Diff** - pick one or more source playlists and one or more
  target playlists, and find tracks in the sources that aren't in any
  target (a different version of the same song - same artist, album and
  title - counts as already there). Preview each one, then choose which
  target(s) to add it to. Tracks you don't want right now can be snoozed
  instead, so they aren't suggested again for 30 days - in any diff, a
  Cascade, or the scheduled auto-scan. Active snoozes are listed on the
  Playlist Diff page, where they can be un-snoozed early.
- **Playlist Search** - search one or more playlists with any combination
  of conditions (the same ones as Playlist Filter), optionally excluding
  tracks already in other playlists (including a different version of the
  same song). Read-only until you pick tracks from the results and add
  them to an existing or new playlist.
- **Playlist Prepend** - add every track from one playlist to the top of
  another, keeping their order and skipping any already there. Review the
  list and untick any you don't want first.
- **Album Swap** - when a single you've added to your playlists gets a full
  album release, search for the album (or pick one of up to 5 suggestions:
  albums you've recently scrobbled on Last.fm that came out in the last 30
  days, if Last.fm is configured), choose the playlists to check, and it
  finds tracks that are the single version of an album track. Review the
  matches, then each confirmed single is replaced with the album version in
  the exact position it occupied.
- **Cascade** - chain the tools above together, fetching each playlist only
  once no matter how many steps use it, then walk through each step's
  review screen in order. Optionally, set `CASCADE_AUTO_ENABLED=1` to run
  the Cascade's default steps automatically as a read-only scan (every day
  at `CASCADE_AUTO_HOUR`, or on specific days/times via `CASCADE_AUTO_SCHEDULE`)
  - it never applies changes on its own, but if it finds something to
  review it posts a summary and a link back to the app to a Discord or
  Slack webhook (`CASCADE_WEBHOOK_URL`) and shows a "pending review" banner
  on the homepage until you review it.
- **Most Played** - see your most-scrobbled tracks, albums, or artists from
  Last.fm (top 25-200, over all time or the last 7 days to 12 months),
  enriched with Spotify metadata (release year, genre, popularity) so you
  can filter them. With conditions set, it looks past your literal top N to
  find that many matches - "top 200 released before 1990" keeps scanning
  your history until it finds 200 that qualify. Tracks can be added to an
  existing or new playlist; albums and artists are read-only. Requires
  Last.fm to be configured (see Setup).
- **Forgotten Favourites** - find tracks you played a lot but haven't
  played in a while: pick a minimum all-time play count and how many months
  it's been since you last played them (e.g. at least 20 plays, not in the
  last 6 months). Results are listed most played first with their play
  count and last-played date, and can be previewed with the embedded
  player. Tracks already in one of your playlists or Liked Songs are still
  shown, flagged with where they are. Select the ones you want and add them
  to an existing or new playlist; any that can't be found on Spotify are
  listed separately. The first run downloads your full Last.fm listening
  history (see Listening history below), which takes about 20 minutes.
  Requires Last.fm to be configured (see Setup).
- **Listening Visuals** - your Last.fm listening history as art.
  **Listening Shape** draws every track you played as a glowing stroke,
  placed by when you played it (in your browser's time zone) and coloured
  from its album art, so each listening session reads as one strand. Switch
  instantly between five shapes: a clock (one ring per day), a week clock
  (one ring per week, Monday to Sunday), day rows, week rows, and a long
  exposure (every day stacked onto one 24-hour ring, so the times you
  listen most glow brightest). Show the last 7, 30 or 90 days, the last 12
  months or a custom range of up to a year, hover a strand to see what it
  was, and download the picture as a PNG. Album art comes from Last.fm,
  with a Spotify search for albums Last.fm has no art for.
  The **artist river** is a streamgraph of your top 5-30 artists (15 by
  default): each is a band whose thickness is your plays of them that
  week, so obsessions swell and fade while long-term favourites flow
  underneath. Each artist's band is coloured from the art of their
  most-played album of all time, so it stays the same whatever range you
  pick. Optionally include everyone else as a faint band along the
  top, so the river's width is all your listening. Show the last 3, 6 or
  12 months, all time or a custom range - ranges over two years are drawn
  by month. Hover a band to see that artist's plays that week, and
  download the river as a PNG. **Colour of your month** turns a month into
  tiles coloured by the album art of what you played in each, in one of
  five styles: top album (the colour of the album you played most), blend
  (every play's colour mixed in OKLab, which keeps mixes from turning
  muddy), weave (a mosaic shared out between albums by plays), stripes
  (each play as a band, in order, as wide as it lasted) or gradient
  (flowing through the album colours in the order played).
  Choose a calendar with one tile per day, or an hour grid with one row
  per day and one column per hour. Step back through past months, hover a
  tile for its plays, top albums and top tracks, and download the picture
  as a PNG. Each
  visualisation's settings are remembered in the browser. Read-only. Requires Last.fm to be configured
  (see Setup), and the first run downloads your listening history (see
  Listening history below).
- **Removal History** - every track Duplicate Finder or Playlist Cleanup
  removes (standalone or in a Cascade) is logged, keeping the last 20
  removals, so you can put tracks back later - all of them or just the
  ones you pick. Restored tracks go back in their original positions if
  the playlist hasn't changed since, otherwise at the end.

## Setup

1. Register a Spotify app at https://developer.spotify.com/dashboard:
    - Click **Create app**.
    - Redirect URI: `http://127.0.0.1:8888/callback` (must match exactly).
    - APIs used: **Web API**.
    - Save, then copy the **Client ID** and **Client Secret** from Settings.

2. Install dependencies:

    ```bash
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3. Copy `.env.example` to `.env` and fill in `SPOTIFY_CLIENT_ID` and
   `SPOTIFY_CLIENT_SECRET` (`SPOTIFY_REDIRECT_URI` is already set to match
   the redirect URI above).

    The app only lets one Spotify account in: `SPOTIFY_OWNER_ID`. Leave it
    blank for now - the first time you log in, the app shows your Spotify
    user ID and tells you to set it, then restart and log in again.

    Optionally, to enable Most Played, Forgotten Favourites and Album Swap's
    album suggestions, also fill in `LASTFM_API_KEY` and `LASTFM_USERNAME` -
    get a free API key
    at https://www.last.fm/api/account/create. Everything else works
    without them.

4. Run the app:
    ```bash
    python app.py
    ```
    Open http://127.0.0.1:8888/, log in with Spotify if prompted, then pick
    a function from the homepage. Each one follows the same pattern:
    configure it (pick playlist(s) and, where relevant, a condition - your
    choice is remembered in the browser, use **Edit** to change it later),
    then run it. Scanning your library can take a few minutes for large
    playlists, and shows a progress screen with a **Cancel** option. Once
    it finishes you get a preview of the changes (tracks to add/remove) -
    review it and select what you actually want, then apply to make the
    change in Spotify.

## Listening history

Forgotten Favourites, Listening Visuals and Most Played's First/Last Played
filters use a local copy of every Last.fm scrobble, kept in a SQLite database at
`.lastfm_cache/scrobbles.sqlite3`. The first tool that needs it downloads
your full history (about 20 minutes, with a progress screen and **Cancel** -
a cancelled download carries on where it stopped next time); after that,
each run only fetches new scrobbles.

Local runs and Railway each keep their own copy, so each downloads your
history once. To back up a copy (or move it between them), snapshot it with
SQLite rather than copying the file directly, since recent writes can still
be in a separate `-wal` file:

```bash
sqlite3 .lastfm_cache/scrobbles.sqlite3 ".backup scrobbles-backup.sqlite3"
```

## Deploying to Railway

The app ships with a `Procfile` (`gunicorn app:app --bind 0.0.0.0:$PORT
--workers 1 --threads 8`), so Railway can build and run it with no extra
config. Keep it to one worker: background jobs and their progress live in
memory, so a second worker process wouldn't see them. The threads let a slow
request (like Album Swap's suggestions) run without holding up the others.

1. In Railway, create a new project from this GitHub repo.
2. Under the service's **Variables**, add `SPOTIFY_CLIENT_ID`,
   `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI` (set the redirect URI
   to `https://<your-railway-domain>/callback` - you'll get the domain in
   the next step, so come back and update this after), and
   `SPOTIFY_OWNER_ID`. Only a browser that has logged in as that account
   can use the app - anyone else who finds the URL just gets a login
   button, and logging in with any other account is refused.
3. Under **Settings > Networking**, click **Generate Domain** to get a
   public `*.up.railway.app` URL.
4. Update `SPOTIFY_REDIRECT_URI` in Railway's variables to
   `https://<that-domain>/callback`, and add the same redirect URI to the
   app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard)
   (Settings > Redirect URIs) - it must match exactly.
5. Keep the service at 1 replica - job progress and the OAuth token cache
   are kept in memory/on local disk per instance, so multiple replicas
   would see inconsistent state.
6. Attach a Railway volume and mount it via `RAILWAY_VOLUME_MOUNT_PATH` (it
   already needs to exist for the playlist/token caches to survive
   redeploys - see `core/paths.py`). If you enable `CASCADE_AUTO_ENABLED`,
   this same volume is what lets a pending review survive a redeploy: the
   scheduler's "last ran" marker and any pending review are written there
   too, so a restart won't cause a duplicate notification or lose a review
   you haven't gotten to yet.
