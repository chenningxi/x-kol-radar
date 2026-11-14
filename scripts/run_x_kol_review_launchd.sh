#!/bin/zsh

set -eu

WORKSPACE="${X_KOL_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
if [[ -f "$WORKSPACE/.env" ]]; then
  set -a
  source "$WORKSPACE/.env"
  set +a
fi

PYTHON_BIN="${X_KOL_PYTHON:-$WORKSPACE/.venv/bin/python}"
export PATH="${X_KOL_PATH:-$PATH}"
export PYTHONUNBUFFERED=1
mkdir -p "$WORKSPACE/logs"

review_status=0
"$PYTHON_BIN" "$WORKSPACE/scripts/run_x_kol_db.py" review || review_status=$?
check_status=0
"$PYTHON_BIN" "$WORKSPACE/scripts/run_x_kol_db.py" check || check_status=$?
if (( review_status != 0 )); then
  exit "$review_status"
fi
exit "$check_status"
