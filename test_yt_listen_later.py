# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp", "python-dotenv"]
# ///
"""Exercise sync()'s state handling, resume and prune logic with stubbed network."""
import importlib.util
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "yt_listen_later.py"
ROOT = Path(tempfile.mkdtemp(prefix="yt-listen-later-test-"))
os.chdir(ROOT)
(ROOT / ".env").write_text("")  # isolate from any real .env beside the script

os.environ.update(
    YOUTUBE_PLAYLIST_URL="https://www.youtube.com/playlist?list=PLtest",
    BASE_URL="https://listen.example.com",
    PUBLIC_DIR=str(ROOT / "public"),
)

spec = importlib.util.spec_from_file_location("m", str(SCRIPT))
m = importlib.util.module_from_spec(spec)
sys.modules["m"] = m
spec.loader.exec_module(m)

# --- stubs -----------------------------------------------------------------
PLAYLIST = ["vid1", "vid2", "vid3"]
FAIL = set()
downloads = []

m.shutil.which = lambda name: "/usr/bin/ffmpeg"  # pretend ffmpeg exists
m.download_cover = lambda cfg, info, state: None


def fake_fetch(cfg):
    info = {"title": "Test Playlist", "webpage_url": "https://www.youtube.com/playlist?list=PLtest"}
    entries = [{"id": v, "title": f"Video {v}"} for v in PLAYLIST]
    if cfg.max_episodes > 0:  # mirror the real fetch_playlist's contract
        entries = entries[: cfg.max_episodes]
    return info, entries


def fake_download(cfg, entry, position):
    vid = entry["id"]
    downloads.append(vid)
    if vid in FAIL:
        return None
    path = cfg.media_dir / f"{vid}.m4a"
    path.write_bytes(os.urandom(1000 + position))
    return m.Episode(
        video_id=vid, title=f"Video {vid}", filename=path.name, size=path.stat().st_size,
        duration=600 + position, description=f"desc {vid}", uploader="Chan",
        webpage_url=f"https://youtu.be/{vid}",
        published=datetime(2026, 7, position + 1, tzinfo=timezone.utc).isoformat(),
        added=datetime.now(timezone.utc).isoformat(), position=position,
    )


m.fetch_playlist = fake_fetch
m.download_episode = fake_download

cfg = m.load_config(None)


def items():
    return [i.findtext("title") for i in ET.parse(cfg.feed_path).getroot().findall("./channel/item")]


def media():
    return sorted(p.name for p in cfg.media_dir.iterdir())


# 1. first sync downloads everything
m.sync(cfg)
assert downloads == PLAYLIST, downloads
assert media() == ["vid1.m4a", "vid2.m4a", "vid3.m4a"], media()
assert len(items()) == 3, items()
print("PASS first sync downloaded 3, feed has 3")

# 2. second sync is a no-op (no re-downloads)
downloads.clear()
m.sync(cfg)
assert downloads == [], downloads
assert len(items()) == 3
print("PASS re-sync downloaded nothing")

# 3. a new video appears; only it is fetched
PLAYLIST.append("vid4")
downloads.clear()
m.sync(cfg)
assert downloads == ["vid4"], downloads
assert len(items()) == 4
print("PASS new video fetched incrementally")

# 4. a failing video is skipped but retried next run, and doesn't break the rest
PLAYLIST.append("vid5")
FAIL.add("vid5")
downloads.clear()
m.sync(cfg)
assert downloads == ["vid5"], downloads
assert len(items()) == 4, items()
FAIL.clear()
downloads.clear()
m.sync(cfg)
assert downloads == ["vid5"], downloads
assert len(items()) == 5
print("PASS failed download skipped, retried and recovered next run")

# 5. removing from the playlist prunes the file and the item
PLAYLIST.remove("vid2")
downloads.clear()
m.sync(cfg)
assert downloads == [], downloads
assert "vid2.m4a" not in media(), media()
assert len(items()) == 4, items()
print("PASS removed video pruned from disk and feed")

# 6. a missing media file is re-downloaded
(cfg.media_dir / "vid1.m4a").unlink()
downloads.clear()
m.sync(cfg)
assert downloads == ["vid1"], downloads
assert "vid1.m4a" in media()
print("PASS deleted media file re-downloaded")

# 7. PRUNE_REMOVED=false keeps the audio
os.environ["PRUNE_REMOVED"] = "false"
cfg2 = m.load_config(None)
PLAYLIST.remove("vid3")
m.sync(cfg2)
assert "vid3.m4a" in media(), media()
print("PASS PRUNE_REMOVED=false keeps audio on disk")

# 8. MAX_EPISODES caps the feed
os.environ["PRUNE_REMOVED"] = "true"
os.environ["MAX_EPISODES"] = "2"
cfg3 = m.load_config(None)
m.sync(cfg3)
assert len(items()) == 2, items()
assert len(media()) == 2, media()
print("PASS MAX_EPISODES caps episodes and prunes extras")

# 9. newest-first ordering and stable guids
root = ET.parse(cfg.feed_path).getroot()
guids = [i.findtext("guid") for i in root.findall("./channel/item")]
dates = [i.findtext("pubDate") for i in root.findall("./channel/item")]
from email.utils import parsedate_to_datetime
parsed = [parsedate_to_datetime(d) for d in dates]
assert parsed == sorted(parsed, reverse=True), dates
assert all(g.startswith("yt:video:") for g in guids), guids
print("PASS feed is newest-first with stable guids")
shutil.rmtree(ROOT, ignore_errors=True)
print("\nALL SYNC TESTS PASSED")
