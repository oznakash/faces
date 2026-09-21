#!/bin/sh
# Local profile — one process, no Docker, no node. http://localhost:8000
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt && ./.venv/bin/playwright install chromium; }
# FACES_STANDALONE=<collection slug> exposes only that collection at "/"; the working home moves to /admin.
exec env FACES_STANDALONE="${FACES_STANDALONE:-iia-summit-2026}" ./.venv/bin/uvicorn faces.server:app --host 127.0.0.1 --port 8000 --reload
