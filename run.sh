#!/usr/bin/env bash
# Start the Warden hub on http://127.0.0.1:8787
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
python -m warden.server
