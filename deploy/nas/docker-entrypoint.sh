#!/bin/sh
set -eu

# These paths are bind mounts in Compose. Creating them here makes a fresh
# deployment self-initialising while keeping browser accounts and job state
# persistent on the NAS host.
mkdir -p /app/tmp /app/output /nas/videos
exec "$@"
