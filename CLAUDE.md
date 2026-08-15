# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file tool that turns a YouTube playlist into a podcast RSS feed. It reads the
playlist with `yt-dlp`, extracts audio with `ffmpeg`, writes an iTunes-flavoured
`feed.xml`, and serves both over HTTP with byte-range support. Everything lives in
`yt_listen_later.py` — no package, no framework.

## Commands

```sh
uv run ./yt_listen_later.py sync     # download new audio, rebuild feed.xml
uv run ./yt_listen_later.py feed     # rebuild feed.xml only, no downloads
uv run ./yt_listen_later.py serve    # serve public/ over HTTP
uv run ./yt_listen_later.py run      # sync now, then serve + re-sync on a timer

uv run ./test_yt_listen_later.py     # run the test suite (single file, no pytest)
```

`devbox shell` puts `uv` and `ffmpeg` on `PATH`; `devbox run <sync|feed|serve|run|test>`
wraps the same commands. Config comes from `.env` (see `.env.example`); copy it before
running anything.

There's no linter or formatter configured — match the existing style (dataclasses,
type hints, `from __future__ import annotations`).

## Architecture

`yt_listen_later.py` is a [PEP 723](https://peps.python.org/pep-0723/) inline-script —
dependencies are declared in the header comment, and `uv run` resolves them on the fly
with no separate lockfile or venv to manage. The file is organised into sections marked
by `# --- name` banner comments: config, helpers, state, sync, feed, serve, cli.

Data flow through the four subcommands:

- **`sync`** — `fetch_playlist` lists videos via `yt-dlp` (flat, no download), then
  `download_episode` pulls audio + metadata per video. Results go into `State`
  (`public/.state.json`), keyed by video ID. `enforce_disk_budget` evicts oldest
  episodes if `MAX_TOTAL_MB` is set, then `write_feed` regenerates `feed.xml`.
- **`feed`** — reads `State` and calls `write_feed` only; no network or `ffmpeg` needed.
- **`serve`** — `RangeRequestHandler` (stdlib `http.server`) serves `public/` with
  `Range` support, required for podcast apps to seek.
- **`run`** — calls `sync` once, starts a background thread that re-syncs every
  `REFRESH_MINUTES`, then calls `serve` in the foreground. This is the Docker default.

Key invariants worth knowing before touching sync logic:

- `State` is saved after every single episode download (not batched), so an
  interrupted sync resumes instead of restarting the backlog.
- Episodes are keyed by video ID; the RSS `guid` is `yt:video:<id>`, stable across
  re-runs so podcast apps don't re-download what's already been heard.
- `state.evicted` tracks videos dropped by the disk budget so they aren't
  re-downloaded next sync just to be evicted again. Clearing `MAX_TOTAL_MB` backfills
  them.
- A failed download is recorded in `state.failures` (`{video_id: {count, last}}`) and
  retried with exponential backoff — never blacklisted. Failures here are usually the
  blocking layer below misbehaving, not bad videos, so they have to recover on their
  own once it's fixed. Don't "fix" repeated failures by dropping the video.
- `Config` is a frozen-at-load dataclass built once from `.env` + environment; there's
  no config reloading — a changed `.env` needs a restart.

## Getting past YouTube's blocking

Most sync failures are here, not in the code above. Three separate mechanisms, each
with its own failure signature — check them in this order before suspecting `sync`:

1. **Cookies** — `COOKIES_FROM_BROWSER` / `COOKIE_FILE`, needed for private and
   age-gated videos. Setting both is rejected at load; a missing `COOKIE_FILE` is a
   hard `die()`, not a skip. Expiry looks like `Sign in to confirm you're not a bot`.
2. **PO tokens** — YouTube won't return audio formats to a datacenter IP without one.
   Compose runs a `pot-provider` sidecar and points `POT_PROVIDER_URL` at it; it's
   `depends_on` only, so it can be up but unhealthy. Absence looks like
   `Requested format is not available`, and a bare `Video unavailable` for a video
   that plays fine elsewhere generally means the request was blocked by IP.
3. **yt-dlp itself** — needs a JS runtime for the "n" challenge (deno, in the image)
   and fetches a remote challenge-solver script at runtime.

The version trap: the PEP 723 header pins `yt-dlp>=2025.5.22`, a floor with no
ceiling, and the Docker image resolves it at **build** time. The container therefore
stays on whatever was current when the image was last built while YouTube keeps
changing. Check `app-python -m yt_dlp --version` against the latest release before
digging further — `docker compose up -d --build` is the fix.

Docker deployment builds the PEP 723 dependencies at image-build time (`uv sync
--script`) and symlinks the resulting interpreter to `app-python`, so the running
container needs no network access or `uv` at runtime — `docker-entrypoint.sh` execs
`app-python` directly for `sync`/`serve`/`run`/`feed`.

## Tests

`test_yt_listen_later.py` stubs the network and `ffmpeg` entirely, covering incremental
sync, resume-after-failure, pruning, `MAX_EPISODES`, and feed ordering. Run it directly
with `uv run` — it needs no fixtures or test runner beyond the stdlib.
