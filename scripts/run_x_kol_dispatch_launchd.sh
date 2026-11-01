#!/bin/zsh

set -eu

WORKSPACE="${X_KOL_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
if [[ -f "$WORKSPACE/.env" ]]; then
  set -a
  source "$WORKSPACE/.env"
  set +a
fi

DB_SCRIPT="$WORKSPACE/scripts/run_x_kol_db.py"
PYTHON_BIN="${X_KOL_PYTHON:-$WORKSPACE/.venv/bin/python}"

export PATH="${X_KOL_PATH:-$PATH}"
export PYTHONUNBUFFERED=1

mkdir -p "$WORKSPACE/logs"
exec "$PYTHON_BIN" "$DB_SCRIPT" dispatch
