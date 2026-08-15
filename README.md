# yt-listen-later

Turn a YouTube playlist into a podcast RSS feed you can subscribe to in Overcast
(or any other podcast app). Add a video to the playlist on your phone, and the
audio shows up as a new episode.

A single [uv](https://docs.astral.sh/uv/) script does the whole job: it reads the
playlist with `yt-dlp`, extracts audio with `ffmpeg`, writes an iTunes-flavoured
`feed.xml`, and serves both over HTTP with byte-range support so seeking works.

To run it on a server, go straight to [Deploying with Docker](#deploying-with-docker)
— that needs nothing on the host but Docker itself.

## Running it locally

Needs `uv` (which fetches Python and the script's dependencies itself) and
`ffmpeg` on `PATH`.

```sh
cp .env.example .env
$EDITOR .env          # set YOUTUBE_PLAYLIST_URL and BASE_URL
```

Or let [devbox](https://www.jetify.com/devbox) provide both dependencies, so
neither lands on your system:

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

- the bundled Caddy container (see below) — the simplest option on a server
- `tailscale funnel 8000`
- `cloudflared tunnel --url http://localhost:8000`
- `ngrok http 8000`
- nginx in front of `serve`, or just rsync `public/` to any static host

Set `BASE_URL` to the resulting HTTPS URL and re-run `sync` (or `feed`) so the
enclosure URLs are regenerated.

Anyone with the URL can read the feed and the audio — there's no auth. Keep the
URL private, or put basic auth on the reverse proxy.

## Deploying with Docker

This is the supported way to run it on a server. The image carries ffmpeg and the
Python dependencies, so the VPS needs nothing but Docker.

```sh
git clone https://github.com/svandragt/yt-listen-later && cd yt-listen-later
cp .env.example .env
$EDITOR .env                          # playlist + BASE_URL

mkdir -p data && sudo chown -R 1000:1000 data   # container runs as uid 1000

docker compose up -d --build
docker compose logs -f
```

That runs `run`: one sync on startup, then serving while re-syncing every
`REFRESH_MINUTES`. A single foreground process, restarted by Docker if it dies.

Add HTTPS — which Overcast requires — with the bundled Caddy service:

```sh
$EDITOR deploy/Caddyfile               # replace listen.example.com
docker compose --profile tls up -d
```

Caddy fetches a certificate on first request and proxies to the app container.
Set `BASE_URL` to the same hostname, then subscribe in Overcast to
`$BASE_URL/feed.xml`.

### Day-to-day

```sh
docker compose exec yt-listen-later app-python /app/yt_listen_later.py sync   # sync now, don't wait for the timer
docker compose exec yt-listen-later app-python /app/yt_listen_later.py feed   # rebuild feed.xml after editing .env
docker compose run --rm yt-listen-later app-python /app/test_yt_listen_later.py
docker compose up -d --build               # upgrade after a git pull
docker compose logs -f yt-listen-later
```

Everything mutable lives in `./data` — audio, `feed.xml`, and the state file — so
that's the only thing to back up, and destroying the container loses nothing.

### Cookies

A server needs them. YouTube answers *"Sign in to confirm you're not a bot"* for
nearly every video from a datacentre IP, so without cookies a deployment syncs
`+0 new` forever while looking perfectly healthy.

Export your YouTube cookies in Netscape format and drop them at `data/cookies.txt`
— compose already points `COOKIE_FILE` there, so nothing else needs configuring:

```sh
install -m 600 -o 1000 -g 1000 cookies.txt data/cookies.txt
docker compose exec yt-listen-later /app/docker-entrypoint.sh sync
```

Export **only** `youtube.com`, not your whole browser — a full dump ships every
site's session cookies to the server for no benefit. `0600` owned by uid 1000
matches the container's `app` user.

Cookies expire every few months, and the symptom is silent: syncs keep succeeding
with `+0 new`. A working sync says `+N new`, so that number is the thing to check —
`+0 new` on its own only means the playlist hasn't changed, but `+0 new` while
you're waiting on a video you definitely added means the cookies have lapsed.
Re-export and re-run the sync above. A missing or expired file logs
`COOKIE_FILE does not exist, continuing without cookies` and keeps serving the
existing feed rather than taking the service down.

### Notes for a small VPS

Disk is usually the binding constraint, and audio accumulates quietly:

- **`MAX_TOTAL_MB`** caps the media directory in MiB. Past the cap the oldest
  episodes are deleted and *not* re-downloaded on the next sync — `MAX_EPISODES`
  bounds the episode count, this bounds the bytes. Set it to something like two
  thirds of the free space on the volume.
- **`AUDIO_FORMAT=opus`** with `AUDIO_QUALITY=48K` is roughly a third the size of
  the default m4a for talking-head video, and Overcast plays opus fine. Pick this
  before the first sync — changing it later won't re-encode what you already have.
- Compose caps the container at 1 CPU and 768 MB so ffmpeg can't make the box
  unresponsive, and caps the JSON log at 30 MB total.
- Downloads are checkpointed per episode, so a sync killed by an OOM or a reboot
  resumes instead of restarting the backlog.
- The app binds to `127.0.0.1:8000` on the host, so nothing is exposed until you
  put the reverse proxy in front of it.

### Without compose

```sh
docker build -t yt-listen-later .
docker run -d --name yt-listen-later --restart unless-stopped \
  --env-file .env -e PUBLIC_DIR=/data/public \
  -v "$PWD/data:/data" -p 127.0.0.1:8000:8000 \
  yt-listen-later
```

Pass a subcommand to override the default: `docker run --rm ... yt-listen-later sync`.

## Keeping it fresh

`run` re-syncs every `REFRESH_MINUTES` in-process, and that's what the Docker
deployment above uses. Outside Docker, plain cron works too:

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
- A video that fails to download is retried on the next sync, then backed off
  exponentially (1h, 2h, 4h … capped at 12h) and reported in the sync summary.
  Failures are never permanent: the usual cause is a transient block rather than
  a bad video, so everything still failing recovers by itself once the cause is
  fixed. `1 video(s) failing` in the log with no progress over a day means the
  block is real — start with cookies and `pot-provider`, below.
- Cookies are needed for private and age-gated videos, and in practice for any
  server deployment at all — see [Cookies](#cookies) above, plus
  `COOKIES_FROM_BROWSER` and `COOKIE_FILE` in `.env.example`.
- YouTube requires a PO token for datacenter IPs (any VPS) before it'll return
  audio formats, even with valid cookies — without one every download fails
  with "Requested format is not available". Docker runs a bundled
  `pot-provider` service for this automatically; see `POT_PROVIDER_URL` in
  `.env.example` if running outside Docker.

## Tests

`uv run ./test_yt_listen_later.py` covers the sync logic — incremental
downloads, resume after a failed video, pruning, `MAX_EPISODES`, and feed
ordering — with the network stubbed out, so it needs neither YouTube nor ffmpeg.

## Configuration

Everything lives in `.env` — see [.env.example](.env.example) for the full,
commented list.
