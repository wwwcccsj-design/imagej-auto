#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLED_PY="/Users/chaojisaiyaren/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

if [ -x "$BUNDLED_PY" ]; then
  PYTHON="$BUNDLED_PY"
else
  PYTHON="$(command -v python3)"
fi

cd "$DIR"
"$PYTHON" run_app.py
