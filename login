#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_PATH="$ROOT_DIR/tmp/browser/douyin-storage-state.json"

cd "$ROOT_DIR"

rm -f "$STATE_PATH"
echo "已清除旧的抖音登录状态。"
uv run douyin-csv-login
