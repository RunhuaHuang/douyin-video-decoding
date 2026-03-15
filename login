#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT_DIR"

uv run douyin-csv-login "$@"
