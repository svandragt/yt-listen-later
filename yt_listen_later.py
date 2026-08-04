#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "yt-dlp>=2024.8.6",
#     "python-dotenv>=1.0.1",
# ]
# ///
"""Turn a YouTube playlist into a podcast RSS feed you can subscribe to in Overcast.

    uv run ./yt_listen_later.py sync     # download new audio + rebuild feed.xml
    uv run ./yt_listen_later.py serve    # serve the feed and audio over HTTP
    uv run ./yt_listen_later.py run      # sync, then serve and re-sync periodically

Configuration lives in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from dotenv import load_dotenv

STATE_VERSION = 1
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


# --------------------------------------------------------------------------- config


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        die(f"{name} must be an integer, got {raw!r}")


@dataclass
class Config:
    playlist: str
    base_url: str
    public_dir: Path
    media_subdir: str
    feed_name: str
    title: str | None
    author: str | None
    description: str | None
    link: str | None
    language: str
    category: str
    explicit: bool
    max_episodes: int
    audio_format: str
    audio_quality: str
    prune: bool
    host: str
    port: int
    refresh_minutes: int
    cookies_from_browser: str | None
    cookie_file: Path | None

    @property
    def media_dir(self) -> Path:
        return self.public_dir / self.media_subdir

    @property
    def state_path(self) -> Path:
        return self.public_dir / ".state.json"

    @property
    def feed_path(self) -> Path:
        return self.public_dir / self.feed_name

    def url_for(self, *parts: str) -> str:
        tail = "/".join(quote(p) for p in parts)
        return f"{self.base_url}/{tail}"


def find_env_file(explicit: str | None) -> Path | None:
    """Prefer an explicit path, then ./.env, then a .env beside the script."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            die(f"env file not found: {path}")
        return path
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if candidate.exists():
            return candidate
    return None


def load_config(env_file: str | None) -> Config:
    found = find_env_file(env_file)
    if found:
        load_dotenv(dotenv_path=found, override=False)
    else:
        log("no .env found, reading configuration from the environment only")

    playlist = (
        os.environ.get("YOUTUBE_PLAYLIST_URL")
        or os.environ.get("YOUTUBE_PLAYLIST_ID")
        or ""
    ).strip()
    if not playlist:
        die(
            "No playlist configured. Set YOUTUBE_PLAYLIST_URL (or YOUTUBE_PLAYLIST_ID) "
            "in .env — copy .env.example to get started."
        )
    if not playlist.startswith(("http://", "https://")):
        playlist = f"https://www.youtube.com/playlist?list={playlist}"

    port = env_int("PORT", 8000)
    base_url = os.environ.get("BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = f"http://{local_ip()}:{port}"
        log(f"BASE_URL not set, guessing {base_url} (fine on a LAN, not for Overcast over the internet)")

    cookie_file = os.environ.get("COOKIE_FILE", "").strip()
    return Config(
        playlist=playlist,
        base_url=base_url,
        public_dir=Path(os.environ.get("PUBLIC_DIR", "public")).expanduser().resolve(),
        media_subdir=os.environ.get("MEDIA_SUBDIR", "media").strip("/") or "media",
        feed_name=os.environ.get("FEED_NAME", "feed.xml").strip("/") or "feed.xml",
        title=(os.environ.get("FEED_TITLE") or "").strip() or None,
        author=(os.environ.get("FEED_AUTHOR") or "").strip() or None,
        description=(os.environ.get("FEED_DESCRIPTION") or "").strip() or None,
        link=(os.environ.get("FEED_LINK") or "").strip() or None,
        language=os.environ.get("FEED_LANGUAGE", "en").strip() or "en",
        category=os.environ.get("FEED_CATEGORY", "Technology").strip() or "Technology",
        explicit=env_bool("FEED_EXPLICIT", False),
        max_episodes=env_int("MAX_EPISODES", 50),
        audio_format=os.environ.get("AUDIO_FORMAT", "m4a").strip().lower() or "m4a",
        audio_quality=os.environ.get("AUDIO_QUALITY", "0").strip() or "0",
        prune=env_bool("PRUNE_REMOVED", True),
        host=os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        refresh_minutes=env_int("REFRESH_MINUTES", 60),
        cookies_from_browser=(os.environ.get("COOKIES_FROM_BROWSER") or "").strip() or None,
        cookie_file=Path(cookie_file).expanduser() if cookie_file else None,
    )


# --------------------------------------------------------------------------- helpers


def log(msg: str) -> None:
    print(f"[yt-listen-later] {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"[yt-listen-later] error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def local_ip() -> str:
    """Best-effort LAN address, so the default BASE_URL is at least reachable."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))  # TEST-NET-1, never actually routed
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def parse_upload_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hms(seconds: int) -> str:
    hours, rem = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def mime_for(path: Path) -> str:
    known = {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
    }
    return known.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "audio/mpeg"


def tag(name: str, text: str | None, indent: str = "    ") -> str:
    if text is None or text == "":
        return ""
    return f"{indent}<{name}>{escape(str(text))}</{name}>\n"


# --------------------------------------------------------------------------- state


@dataclass
class Episode:
    video_id: str
    title: str
    filename: str
    size: int
    duration: int = 0
    description: str = ""
    uploader: str = ""
    webpage_url: str = ""
    published: str = ""  # ISO 8601
    added: str = ""  # ISO 8601, when we downloaded it
    position: int = 0

    @property
    def pub_datetime(self) -> datetime:
        for value in (self.published, self.added):
            if value:
                try:
                    return datetime.fromisoformat(value)
                except ValueError:
                    continue
        return datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "Episode":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class State:
    episodes: dict[str, Episode] = field(default_factory=dict)
    playlist_title: str = ""
    playlist_url: str = ""
    cover: str = ""

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"ignoring unreadable state file {path}: {exc}")
            return cls()
        return cls(
            episodes={
                vid: Episode.from_dict(data)
                for vid, data in (raw.get("episodes") or {}).items()
            },
            playlist_title=raw.get("playlist_title", ""),
            playlist_url=raw.get("playlist_url", ""),
            cover=raw.get("cover", ""),
        )

    def save(self, path: Path) -> None:
        payload = {
            "version": STATE_VERSION,
            "playlist_title": self.playlist_title,
            "playlist_url": self.playlist_url,
            "cover": self.cover,
            "episodes": {vid: ep.to_dict() for vid, ep in self.episodes.items()},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- sync


def ydl_common_opts(cfg: Config) -> dict:
    opts: dict = {"quiet": True, "no_warnings": True, "noprogress": True}
    if cfg.cookies_from_browser:
        opts["cookiesfrombrowser"] = (cfg.cookies_from_browser,)
    if cfg.cookie_file:
        if not cfg.cookie_file.exists():
            die(f"COOKIE_FILE does not exist: {cfg.cookie_file}")
        opts["cookiefile"] = str(cfg.cookie_file)
    return opts


def fetch_playlist(cfg: Config) -> tuple[dict, list[dict]]:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    opts = ydl_common_opts(cfg) | {"extract_flat": "in_playlist", "skip_download": True}
    log(f"reading playlist {cfg.playlist}")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(cfg.playlist, download=False)
    except DownloadError as exc:
        die(f"could not read the playlist: {exc}")

    entries = [
        entry
        for entry in (info.get("entries") or [])
        if entry and entry.get("id") and entry.get("_type") != "playlist"
    ]
    # Unavailable videos still show up in flat extraction; they have no duration
    # and downloading them fails, which we handle per-entry below.
    if cfg.max_episodes > 0:
        entries = entries[: cfg.max_episodes]
    return info, entries


def download_episode(cfg: Config, entry: dict, position: int) -> Episode | None:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    video_id = entry["id"]
    url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    outtmpl = str(cfg.media_dir / "%(id)s.%(ext)s")
    opts = ydl_common_opts(cfg) | {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "writethumbnail": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": cfg.audio_format,
                "preferredquality": cfg.audio_quality,
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }

    log(f"downloading {entry.get('title') or video_id}")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        log(f"skipping {video_id}: {exc}")
        return None

    path = locate_audio(cfg.media_dir, video_id)
    if path is None:
        log(f"skipping {video_id}: no audio file produced")
        return None

    published = parse_upload_date(info.get("upload_date"))
    return Episode(
        video_id=video_id,
        title=(info.get("title") or video_id).strip(),
        filename=path.name,
        size=path.stat().st_size,
        duration=int(info.get("duration") or 0),
        description=(info.get("description") or "").strip(),
        uploader=(info.get("uploader") or info.get("channel") or "").strip(),
        webpage_url=info.get("webpage_url") or url,
        published=published.isoformat() if published else "",
        added=datetime.now(timezone.utc).isoformat(),
        position=position,
    )


def locate_audio(media_dir: Path, video_id: str) -> Path | None:
    """Find the converted file; the extension depends on the postprocessor."""
    candidates = [
        p
        for p in media_dir.glob(f"{glob_escape(video_id)}.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".webp", ".jpg", ".png"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def download_cover(cfg: Config, info: dict, state: State) -> None:
    import urllib.request

    thumbnails = info.get("thumbnails") or []
    url = info.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
    if not url:
        return
    suffix = Path(urlsplit(url).path).suffix or ".jpg"
    if suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        suffix = ".jpg"
    target = cfg.public_dir / f"cover{suffix}"
    if target.exists() and state.cover == target.name:
        return
    try:
        with urllib.request.urlopen(url, timeout=30) as response, target.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    except OSError as exc:
        log(f"could not fetch cover art: {exc}")
        return
    state.cover = target.name
    log(f"saved cover art to {target}")


def sync(cfg: Config) -> None:
    if shutil.which("ffmpeg") is None:
        die("ffmpeg is required to extract audio — install it and try again")

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    state = State.load(cfg.state_path)
    info, entries = fetch_playlist(cfg)
    state.playlist_title = info.get("title") or state.playlist_title
    state.playlist_url = info.get("webpage_url") or cfg.playlist
    download_cover(cfg, info, state)

    wanted = {entry["id"] for entry in entries}
    new_count = 0
    for position, entry in enumerate(entries):
        video_id = entry["id"]
        existing = state.episodes.get(video_id)
        if existing and (cfg.media_dir / existing.filename).exists():
            existing.position = position
            continue
        episode = download_episode(cfg, entry, position)
        if episode is None:
            continue
        state.episodes[video_id] = episode
        new_count += 1
        state.save(cfg.state_path)  # checkpoint, so a crash doesn't redo everything

    removed = 0
    if cfg.prune:
        for video_id in [v for v in state.episodes if v not in wanted]:
            episode = state.episodes.pop(video_id)
            path = cfg.media_dir / episode.filename
            path.unlink(missing_ok=True)
            removed += 1
        for stray in cfg.media_dir.iterdir():
            if stray.is_file() and stray.name not in {e.filename for e in state.episodes.values()}:
                stray.unlink(missing_ok=True)

    state.save(cfg.state_path)
    write_feed(cfg, state)
    log(
        f"{len(state.episodes)} episode(s) in feed "
        f"(+{new_count} new, -{removed} removed) → {cfg.feed_path}"
    )
    log(f"subscribe in Overcast with: {cfg.url_for(cfg.feed_name)}")


# --------------------------------------------------------------------------- feed


def write_feed(cfg: Config, state: State) -> None:
    episodes = sorted(
        state.episodes.values(), key=lambda e: (e.pub_datetime, e.position), reverse=True
    )
    title = cfg.title or state.playlist_title or "YouTube Listen Later"
    author = cfg.author or next((e.uploader for e in episodes if e.uploader), "YouTube")
    description = cfg.description or f"Audio from the YouTube playlist “{title}”."
    link = cfg.link or state.playlist_url or cfg.base_url
    feed_url = cfg.url_for(cfg.feed_name)
    cover_url = cfg.url_for(state.cover) if state.cover else None

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<rss version="2.0" xmlns:itunes={quoteattr(ITUNES_NS)} '
        f'xmlns:atom={quoteattr(ATOM_NS)}>\n',
        "  <channel>\n",
    ]
    out.append(tag("title", title, "    "))
    out.append(tag("link", link, "    "))
    out.append(tag("description", description, "    "))
    out.append(tag("language", cfg.language, "    "))
    out.append(tag("generator", "yt-listen-later", "    "))
    out.append(f"    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>\n")
    out.append(f'    <atom:link href={quoteattr(feed_url)} rel="self" type="application/rss+xml"/>\n')
    out.append(tag("itunes:author", author, "    "))
    out.append(tag("itunes:summary", description, "    "))
    out.append(f'    <itunes:explicit>{"yes" if cfg.explicit else "no"}</itunes:explicit>\n')
    out.append('    <itunes:type>episodic</itunes:type>\n')
    out.append(f"    <itunes:category text={quoteattr(cfg.category)}/>\n")
    out.append("    <itunes:owner>\n")
    out.append(tag("itunes:name", author, "      "))
    out.append("    </itunes:owner>\n")
    if cover_url:
        out.append(f"    <itunes:image href={quoteattr(cover_url)}/>\n")
        out.append("    <image>\n")
        out.append(tag("url", cover_url, "      "))
        out.append(tag("title", title, "      "))
        out.append(tag("link", link, "      "))
        out.append("    </image>\n")

    for episode in episodes:
        enclosure_url = cfg.url_for(cfg.media_subdir, episode.filename)
        path = cfg.media_dir / episode.filename
        out.append("    <item>\n")
        out.append(tag("title", episode.title, "      "))
        out.append(tag("link", episode.webpage_url or link, "      "))
        out.append(f"      <guid isPermaLink=\"false\">yt:video:{escape(episode.video_id)}</guid>\n")
        out.append(f"      <pubDate>{format_datetime(episode.pub_datetime)}</pubDate>\n")
        out.append(tag("description", episode.description or episode.title, "      "))
        out.append(tag("itunes:author", episode.uploader or author, "      "))
        if episode.duration:
            out.append(tag("itunes:duration", hms(episode.duration), "      "))
        out.append(
            f"      <enclosure url={quoteattr(enclosure_url)} "
            f'length="{episode.size}" type={quoteattr(mime_for(path))}/>\n'
        )
        out.append('      <itunes:explicit>%s</itunes:explicit>\n' % ("yes" if cfg.explicit else "no"))
        out.append("    </item>\n")

    out.append("  </channel>\n</rss>\n")
    cfg.feed_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.feed_path.write_text("".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- serve


class RangeRequestHandler(BaseHTTPRequestHandler):
    """Static file handler with Range support — podcast apps need it to seek."""

    server_version = "yt-listen-later"
    protocol_version = "HTTP/1.1"
    root: Path = Path(".")

    def do_HEAD(self) -> None:  # noqa: N802
        self.serve(body=False)

    def do_GET(self) -> None:  # noqa: N802
        self.serve(body=True)

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def resolve(self) -> Path | None:
        rel = unquote(urlsplit(self.path).path).lstrip("/")
        if rel in ("", "/"):
            rel = "index.html"
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None  # path traversal attempt
        if candidate.name.startswith("."):
            return None
        return candidate if candidate.is_file() else None

    def serve(self, body: bool) -> None:
        path = self.resolve()
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        size = path.stat().st_size
        ctype = (
            "application/rss+xml; charset=utf-8"
            if path.suffix == ".xml"
            else mimetypes.guess_type(path.name)[0] or mime_for(path)
        )
        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range")
        if rng:
            parsed = parse_range(rng, size)
            if parsed is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            partial = True

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(int(path.stat().st_mtime)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size == 0:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":  # suffix range: last N bytes
        length = int(last)
        if length == 0:
            return None
        return max(size - length, 0), size - 1
    start = int(first)
    if start >= size:
        return None
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return None
    return start, end


def serve(cfg: Config) -> None:
    if not cfg.feed_path.exists():
        log(f"no feed at {cfg.feed_path} yet — run `sync` first")
    handler = type("Handler", (RangeRequestHandler,), {"root": cfg.public_dir})
    cfg.public_dir.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    log(f"serving {cfg.public_dir} on http://{cfg.host}:{cfg.port}")
    log(f"subscribe in Overcast with: {cfg.url_for(cfg.feed_name)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()


def run(cfg: Config) -> None:
    sync(cfg)
    if cfg.refresh_minutes > 0:
        def loop() -> None:
            while True:
                time.sleep(cfg.refresh_minutes * 60)
                try:
                    sync(cfg)
                except SystemExit as exc:
                    log(f"refresh failed: {exc}")
                except Exception as exc:  # keep serving even if a refresh breaks
                    log(f"refresh failed: {exc!r}")

        threading.Thread(target=loop, daemon=True, name="refresh").start()
        log(f"will re-sync every {cfg.refresh_minutes} minute(s)")
    serve(cfg)


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yt-listen-later", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", help="path to the .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="download new playlist audio and rebuild the feed")
    sub.add_parser("feed", help="rebuild feed.xml from existing state without downloading")
    sub.add_parser("serve", help="serve the feed and audio over HTTP")
    sub.add_parser("run", help="sync, then serve and re-sync periodically")
    args = parser.parse_args(argv)

    cfg = load_config(args.env_file)
    if args.command == "sync":
        sync(cfg)
    elif args.command == "feed":
        state = State.load(cfg.state_path)
        write_feed(cfg, state)
        log(f"wrote {cfg.feed_path} with {len(state.episodes)} episode(s)")
    elif args.command == "serve":
        serve(cfg)
    elif args.command == "run":
        run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
