# /// script
# requires-python = ">=3.11"
# dependencies = ["yt-dlp==2026.8.19", "python-dotenv", "bgutil-ytdlp-pot-provider==1.3.2"]
# ///
"""Exercise sync()'s state handling, resume and prune logic with stubbed network."""
import importlib.util
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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


SIZES: dict[str, int] = {}  # video id -> bytes, for disk-budget tests


def fake_download(cfg, entry, position):
    vid = entry["id"]
    downloads.append(vid)
    if vid in FAIL:
        return None
    path = cfg.media_dir / f"{vid}.m4a"
    path.write_bytes(b"\0" * SIZES.get(vid, 1000 + position))
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

# --- disk budget (MAX_TOTAL_MB) --------------------------------------------
MIB = 1024 * 1024


def reset(playlist, **env):
    """Start from a clean feed/state with a known playlist and env."""
    shutil.rmtree(cfg.public_dir, ignore_errors=True)
    PLAYLIST[:] = playlist
    SIZES.clear()
    downloads.clear()
    os.environ.update(MAX_EPISODES="0", MAX_TOTAL_MB="0", PRUNE_REMOVED="true")
    os.environ.update(env)
    return m.load_config(None)


def state():
    return m.State.load(cfg.state_path)


# 10. the budget evicts oldest episodes, keeping the newest that fit
c = reset(["v1", "v2", "v3", "v4"], MAX_TOTAL_MB="2")
SIZES.update({v: MIB for v in PLAYLIST})
m.sync(c)
# positions 0..3 map to July 1..4, so v4 is newest
assert media() == ["v3.m4a", "v4.m4a"], media()
assert items() == ["Video v4", "Video v3"], items()
print("PASS disk budget keeps the newest episodes that fit")

# 11. the churn guard: evicted episodes are not re-downloaded next sync
downloads.clear()
m.sync(c)
assert downloads == [], f"re-downloaded evicted episodes: {downloads}"
assert media() == ["v3.m4a", "v4.m4a"], media()
print("PASS evicted episodes are not re-downloaded on the next sync")

# 12. evictions are recorded in the state file and survive a reload
assert state().evicted == {"v1", "v2"}, state().evicted
print("PASS evictions persist in state.json")

# 13. an evicted video that leaves the playlist stops being tracked
PLAYLIST.remove("v1")
m.sync(c)
assert state().evicted == {"v2"}, state().evicted
print("PASS evictions are forgotten once the video leaves the playlist")

# 14. turning the budget off backfills what was evicted
c_off = reset(["v1", "v2", "v3", "v4"], MAX_TOTAL_MB="2")
SIZES.update({v: MIB for v in PLAYLIST})
m.sync(c_off)
assert len(media()) == 2, media()
os.environ["MAX_TOTAL_MB"] = "0"
c_off = m.load_config(None)
downloads.clear()
m.sync(c_off)
assert sorted(media()) == ["v1.m4a", "v2.m4a", "v3.m4a", "v4.m4a"], media()
assert sorted(downloads) == ["v1", "v2"], downloads
assert state().evicted == set(), state().evicted
print("PASS clearing MAX_TOTAL_MB backfills previously evicted episodes")

# 15. a single episode larger than the whole budget is still kept
c_big = reset(["big"], MAX_TOTAL_MB="1")
SIZES["big"] = 3 * MIB
m.sync(c_big)
assert media() == ["big.m4a"], media()
assert len(items()) == 1, items()
print("PASS an oversized newest episode is kept rather than leaving an empty feed")

# --- failed downloads (retry backoff) ---------------------------------------

# 16. a repeatedly failing video starts backing off instead of retrying hourly
c_fail = reset(["f1"])
FAIL.add("f1")
m.sync(c_fail)  # first failure still retries on the next run (test 4's contract)
assert downloads == ["f1"], downloads
downloads.clear()
m.sync(c_fail)  # second failure starts the backoff
assert downloads == ["f1"], downloads
assert state().failures["f1"]["count"] == 2, state().failures
downloads.clear()
m.sync(c_fail)
assert downloads == [], f"retried inside the backoff window: {downloads}"
print("PASS a repeatedly failing download backs off instead of retrying every sync")

# 17. past the backoff window it retries, and success clears the failure record
stale = state()
stale.failures["f1"]["last"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
stale.save(c_fail.state_path)
FAIL.discard("f1")
downloads.clear()
m.sync(c_fail)
assert downloads == ["f1"], downloads
assert state().failures == {}, state().failures
assert media() == ["f1.m4a"], media()
print("PASS a recovered video retries after the backoff and clears its failure record")

# 18. the delay doubles per failure and is capped, so nothing is dropped forever
assert [m.retry_delay_hours(n) for n in (1, 2, 3, 5, 99)] == [0, 1, 2, 8, 12]
print("PASS retry delay backs off exponentially up to the cap")

# --- cookies ----------------------------------------------------------------

# 19. a missing COOKIE_FILE warns and carries on rather than exiting at boot
c_cookie = reset(["c1"])
os.environ["COOKIE_FILE"] = str(ROOT / "nope.txt")
opts = m.ydl_common_opts(m.load_config(None))
assert "cookiefile" not in opts, opts
print("PASS a missing COOKIE_FILE is skipped instead of killing the process")

# 20. a cookie file that exists is handed to yt-dlp
(ROOT / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
os.environ["COOKIE_FILE"] = str(ROOT / "cookies.txt")
opts = m.ydl_common_opts(m.load_config(None))
assert opts["cookiefile"] == str(ROOT / "cookies.txt"), opts
del os.environ["COOKIE_FILE"]
print("PASS an existing COOKIE_FILE is passed through to yt-dlp")

# 21. asking for both cookie sources is still a hard error
os.environ.update(COOKIE_FILE=str(ROOT / "cookies.txt"), COOKIES_FROM_BROWSER="firefox")
try:
    m.load_config(None)
except SystemExit:
    pass
else:
    raise AssertionError("expected COOKIE_FILE + COOKIES_FROM_BROWSER to be rejected")
del os.environ["COOKIE_FILE"], os.environ["COOKIES_FROM_BROWSER"]
print("PASS setting both cookie sources is still rejected")

# --- pot provider -----------------------------------------------------------

# 22. the bgutil POT provider registers with yt-dlp; an empty registry means a
# datacenter IP gets bot-blocked and sync silently stops gaining episodes
problem = m.check_pot_provider()
assert problem is None, problem
print("PASS the bgutil POT provider registers with yt-dlp")

# 23. a web player_client is forced, so yt-dlp fetches a POT instead of falling
# back to the default client that gets bot-blocked without ever calling the
# provider — and it doesn't clobber the provider's own base_url arg
c_pot = reset(["p1"], POT_PROVIDER_URL="http://pot:4416")
ea = m.ydl_common_opts(c_pot)["extractor_args"]
assert ea["youtube"]["player_client"] == ["web_safari"], ea
assert ea["youtubepot-bgutilhttp"]["base_url"] == ["http://pot:4416"], ea
print("PASS a web player_client is forced alongside the POT provider arg")

# 24. the player client list is configurable for when YouTube shifts again
c_pc = reset(["p1"], YT_PLAYER_CLIENT="web_safari, web")
assert m.ydl_common_opts(c_pc)["extractor_args"]["youtube"]["player_client"] == ["web_safari", "web"]
del os.environ["YT_PLAYER_CLIENT"]
print("PASS YT_PLAYER_CLIENT overrides the player client list")

shutil.rmtree(ROOT, ignore_errors=True)
print("\nALL SYNC TESTS PASSED")
