FROM python:3.12-slim

# ffmpeg does the audio extraction; tini reaps the ffmpeg children yt-dlp spawns
# so a `docker stop` mid-download doesn't leave zombies behind.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

# Pinned rather than :latest so image builds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY yt_listen_later.py ./

# Install the script's inline (PEP 723) dependencies at build time, then link the
# resulting interpreter to a stable path. Two things fall out of this:
#   - the dependency list still lives in exactly one place, the script header;
#   - nothing invokes uv at runtime, so starting a container needs no network,
#     no dependency resolution, and no write access to the uv cache.
RUN uv sync --script yt_listen_later.py \
	&& ln -s "$(uv python find --script yt_listen_later.py)" /usr/local/bin/app-python \
	&& chmod -R a+rX /opt/uv-cache \
	&& app-python -c "import yt_dlp, dotenv"

COPY test_yt_listen_later.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Unprivileged, with a fixed uid so a bind-mounted ./data can be chowned to match.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
ENV HOME=/home/app \
    PUBLIC_DIR=/data/public \
    HOST=0.0.0.0 \
    PORT=8000
USER app

VOLUME /data
EXPOSE 8000

# The feed being fetchable is the thing that matters, so that's what we check.
# Uses the base interpreter: urllib is all this needs.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
	CMD ["python", "-c", "import os,sys,urllib.request; url=f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/{os.environ.get('FEED_NAME','feed.xml')}\"; sys.exit(0 if urllib.request.urlopen(url, timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
# `run` syncs once, then serves while re-syncing on REFRESH_MINUTES — a single
# foreground process, which is what a container wants.
CMD ["run"]
