# yt-listen-later

Turn a YouTube playlist into a podcast RSS feed you can subscribe to in Overcast
(or any other podcast app). Add a video to the playlist on your phone, and the
audio shows up as a new episode.

A single [uv](https://docs.astral.sh/uv/) script does the whole job: it reads the
playlist with `yt-dlp`, extracts audio with `ffmpeg`, writes an iTunes-flavoured
`feed.xml`, and serves both over HTTP with byte-range support so seeking works.

## Requirements

- `uv` (fetches Python and the script's dependencies itself)
- `ffmpeg` on `PATH`

## Setup

```sh
cp .env.example .env
$EDITOR .env          # set YOUTUBE_PLAYLIST_URL and BASE_URL
```

## Usage

```sh
uv run ./yt_listen_later.py sync     # download new audio, rebuild feed.xml
uv run ./yt_listen_later.py serve    # serve public/ over HTTP
uv run ./yt_listen_later.py run      # sync now, then serve + re-sync on a timer
uv run ./yt_listen_later.py feed     # rebuild feed.xml only, no downloads
```

The script is executable, so `./yt_listen_later.py run` works too.

Then subscribe in Overcast: **+ → Add URL** and paste `$BASE_URL/feed.xml`.

## Making it reachable from Overcast

Overcast fetches the feed from its own servers, so `localhost` won't do —
`BASE_URL` has to be publicly reachable and must match how the feed is served.
Any of these work:

- `tailscale funnel 8000`
- `cloudflared tunnel --url http://localhost:8000`
- `ngrok http 8000`
- nginx/Caddy in front of `serve`, or just rsync `public/` to any static host

Set `BASE_URL` to the resulting HTTPS URL and re-run `sync` (or `feed`) so the
enclosure URLs are regenerated.

Anyone with the URL can read the feed and the audio — there's no auth. Keep the
URL private, or put basic auth on the reverse proxy.

## Keeping it fresh

`run` re-syncs every `REFRESH_MINUTES`. For a server, a timer is tidier:

```sh
*/30 * * * * cd /srv/yt-listen-later && /usr/local/bin/uv run ./yt_listen_later.py sync >> sync.log 2>&1
```

## How it works

- `public/.state.json` records every downloaded episode, so `sync` only fetches
  what's new and survives interruptions mid-playlist.
- Episodes are keyed by video ID and the RSS `guid` is `yt:video:<id>`, stable
  across re-runs — podcast apps won't re-download episodes you've already heard.
- `pubDate` comes from the video's upload date, falling back to download time.
- When a video leaves the playlist its audio is deleted (`PRUNE_REMOVED=false`
  keeps it). Overcast drops the episode on its next refresh.
- Private or age-gated playlists need cookies; see `COOKIES_FROM_BROWSER` and
  `COOKIE_FILE` in `.env.example`.

## Tests

`uv run ./test_yt_listen_later.py` covers the sync logic — incremental
downloads, resume after a failed video, pruning, `MAX_EPISODES`, and feed
ordering — with the network stubbed out, so it needs neither YouTube nor ffmpeg.

## Configuration

Everything lives in `.env` — see [.env.example](.env.example) for the full,
commented list.
