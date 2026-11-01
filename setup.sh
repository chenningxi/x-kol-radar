#!/bin/sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

mkdir -p "$ROOT/data" "$ROOT/logs"
if [ ! -f "$ROOT/data/x_profiles.md" ]; then
  cp "$ROOT/data/x_profiles.example.md" "$ROOT/data/x_profiles.md"
  echo "Created data/x_profiles.md from the example watchlist."
fi

command -v python3 >/dev/null 2>&1 || {
  echo "Missing dependency: python3" >&2
  exit 1
}

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/python" -m py_compile "$ROOT"/src/x_kol_sync/*.py "$ROOT"/scripts/*.py

if command -v opencli >/dev/null 2>&1; then
  echo "opencli: found"
else
  echo "opencli: not found (install it before running X sync)"
fi

if command -v lark-cli >/dev/null 2>&1; then
  echo "lark-cli: found"
else
  echo "lark-cli: not found (install/configure it before Feishu dispatch)"
fi

echo "Setup complete. Configure .env, then run scripts/run_x_kol_db.py init and profiles."
