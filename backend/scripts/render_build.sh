#!/usr/bin/env bash
set -euo pipefail

# Run from Render's backend root; credentials are supplied only by the platform.
python -m pip install -r requirements.txt
if [ "${VOICE_INPUT_ENABLED:-0}" = "1" ]; then
  python -m pip install -r requirements-voice.txt
  python scripts/setup_voice_model.py
  python scripts/check_voice_model.py
fi
