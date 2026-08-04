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

### With devbox

[devbox](https://www.jetify.com/devbox) provides both dependencies, so nothing
lands on your system:

```sh
devbox shell           # uv + ffmpeg on PATH
devbox run sync        # also: feed, serve, run, test
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

## Hosting on a VPS

`deploy/install.sh` sets the whole thing up on a Debian/Ubuntu box — installs
ffmpeg and uv, creates a system user, copies the script to `/srv/yt-listen-later`,
and enables a systemd timer that syncs every 30 minutes:

```sh
sudo ./deploy/install.sh
sudo -e /srv/yt-listen-later/.env       # playlist + BASE_URL
sudo systemctl start yt-listen-later-sync.service
journalctl -fu yt-listen-later-sync.service
```

It's idempotent, so re-run it after pulling new commits.

Then serve `/srv/yt-listen-later/public` over HTTPS. `deploy/Caddyfile` does that
with automatic certificates; point it at the directory and **no long-running
Python process is needed at all** — just the sync timer. If you'd rather proxy to
the built-in server, uncomment the `reverse_proxy` line and
`systemctl enable --now yt-listen-later.service` (it binds to loopback only).

`deploy/` contains:

| File | What |
|---|---|
| `install.sh` | one-shot installer, re-runnable |
| `yt-listen-later-sync.service` + `.timer` | periodic sync, niced so ffmpeg doesn't hog a 1-vCPU box |
| `yt-listen-later.service` | the optional built-in HTTP server |
| `Caddyfile` | HTTPS, cache headers, optional basic auth |

### Small VPS notes

Disk is usually the binding constraint, and audio accumulates quietly:

- **`MAX_TOTAL_MB`** caps the media directory in MiB. Past the cap the oldest
  episodes are deleted and *not* re-downloaded on the next sync — `MAX_EPISODES`
  bounds the episode count, this bounds the bytes. Set it to something like
  two thirds of your free space.
- **`AUDIO_FORMAT=opus`** with `AUDIO_QUALITY=48K` is roughly a third the size of
  default m4a for talking-head video, and Overcast plays opus fine.
- The sync unit is `Nice=10` with idle IO so transcoding doesn't make the box
  unresponsive while you're using it for something else.
- Downloads are checkpointed per episode, so a sync killed by an OOM or a reboot
  resumes rather than starting the backlog again.

## Keeping it fresh

`run` re-syncs every `REFRESH_MINUTES` in-process, which is convenient on a
laptop. On a server prefer the systemd timer above, or plain cron:

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
- `MAX_TOTAL_MB` evictions are remembered in the state file, so a capped feed
  doesn't re-download the same old episode every sync forever. Clearing the cap
  backfills them on the next run.
- Private or age-gated playlists need cookies; see `COOKIES_FROM_BROWSER` and
  `COOKIE_FILE` in `.env.example`.

## Tests

`uv run ./test_yt_listen_later.py` covers the sync logic — incremental
downloads, resume after a failed video, pruning, `MAX_EPISODES`, and feed
ordering — with the network stubbed out, so it needs neither YouTube nor ffmpeg.

## Configuration

Everything lives in `.env` — see [.env.example](.env.example) for the full,
commented list.
