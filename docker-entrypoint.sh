#!/usr/bin/env bash
# Check the things that fail confusingly inside a container, then hand over to
# the script. Accepts either a bare command (sync/serve/run/feed) or a full
# command line to exec instead.
set -euo pipefail

DATA_DIR=$(dirname "${PUBLIC_DIR:-/data/public}")

if [[ ! -d $DATA_DIR ]]; then
	echo "entrypoint: $DATA_DIR does not exist — mount a volume at /data" >&2
	exit 1
fi

if [[ ! -w $DATA_DIR ]]; then
	cat >&2 <<-EOF
		entrypoint: $DATA_DIR is not writable by uid $(id -u).

		A bind-mounted host directory keeps its host ownership, so fix it there:
		    sudo chown -R 1000:1000 ./data
		Or run the container as the owning user:
		    docker run --user "\$(id -u):\$(id -g)" ...
	EOF
	exit 1
fi

mkdir -p "${PUBLIC_DIR:-/data/public}"

if [[ -z ${YOUTUBE_PLAYLIST_URL:-} && -z ${YOUTUBE_PLAYLIST_ID:-} && ! -f /app/.env ]]; then
	cat >&2 <<-EOF
		entrypoint: no playlist configured.

		Pass one in the environment:
		    docker run -e YOUTUBE_PLAYLIST_URL=... -e BASE_URL=... ...
		or point compose at your .env file (see docker-compose.yml).
	EOF
	exit 1
fi

# Match the Dockerfile's CMD when invoked with no arguments at all, so an
# overridden entrypoint still starts the server rather than an argparse error.
if [[ $# -eq 0 ]]; then
	set -- run
fi

# Bare subcommands are the common case; anything else runs verbatim so that
# `docker run ... bash` and `docker run ... ./test_yt_listen_later.py` work.
case $1 in
sync | serve | run | feed)
	# app-python is the build-time environment, so no uv, no network, no
	# dependency resolution happens here.
	exec app-python /app/yt_listen_later.py "$@"
	;;
*)
	exec "$@"
	;;
esac
