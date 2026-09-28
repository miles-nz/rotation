# Rotation

A small local web app with a homepage listing Spotify library-management
functions. Currently available:

- **Liked Songs Sync** - keeps your Liked Songs in sync with the union of
  two playlists: reads both playlists, reads your current Liked Songs, adds
  what's missing, and removes liked tracks that are no longer in either
  playlist.
- **Duplicate Finder** - pick two or more playlists and find tracks that
  appear in more than one of them, with the date each copy was added, so
  you can select which playlist(s) to remove each duplicate from.
- **Track Search** - search Spotify's catalog and preview any track with
  the embedded player.
- **All Playlists** - lists every playlist in your library; click a name or
  ID to copy it (handy for filling in the `DEFAULT_*` variables in `.env`).
- **Playlist Filter** - pick one or more playlists and one or more
  conditions (added date, release year, popularity, duration, explicit,
  artist name, track name, or genre), and add every matching track to
  another playlist - an existing one or a brand new one.
  Flags tracks already in the destination, plus any that look like a
  different version (remaster, live, single edit, etc.) of a track already
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

    Optionally, to enable Most Played and Album Swap's album suggestions,
    also fill in `LASTFM_API_KEY` and `LASTFM_USERNAME` - get a free API key
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

## Deploying to Railway

The app ships with a `Procfile` (`gunicorn app:app --bind 0.0.0.0:$PORT`), so
Railway can build and run it with no extra config.

1. In Railway, create a new project from this GitHub repo.
2. Under the service's **Variables**, add `SPOTIFY_CLIENT_ID`,
   `SPOTIFY_CLIENT_SECRET`, and `SPOTIFY_REDIRECT_URI` (set the redirect URI
   to `https://<your-railway-domain>/callback` - you'll get the domain in
   the next step, so come back and update this after).
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
