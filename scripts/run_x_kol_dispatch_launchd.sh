#!/bin/zsh

set -u

WORKSPACE="${X_KOL_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
DISPATCH_SCRIPT="$WORKSPACE/scripts/run_x_kol_dispatch.py"
PYTHON_BIN="${X_KOL_PYTHON:-python3}"

export PATH="${X_KOL_PATH:-$PATH}"
export PYTHONUNBUFFERED=1

mkdir -p "$WORKSPACE/logs"
exec "$PYTHON_BIN" "$DISPATCH_SCRIPT" dispatch
