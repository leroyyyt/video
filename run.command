#!/usr/bin/env bash
# macOS / Linux launcher — double-click this file (no typing needed).
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

if [ ! -d ".venv" ]; then
  echo "First-time setup — creating a private environment…"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Checking dependencies…"
python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
pip install -q -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo ""
  echo "  !! FFmpeg was not found on your PATH. Merging needs it."
  echo "     macOS:  brew install ffmpeg"
  echo "     Linux:  sudo apt install ffmpeg"
  echo ""
fi

# Open the browser a moment after the server starts.
(
  sleep 2
  if command -v open >/dev/null 2>&1; then open "http://127.0.0.1:8000"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:8000"
  fi
) &

echo ""
echo "Starting… your browser will open at http://127.0.0.1:8000"
echo "Keep this window open while you use the app. Close it to stop."
echo ""
python main.py
