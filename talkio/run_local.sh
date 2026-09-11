#!/usr/bin/env bash
# Talkio launcher — Mac / Linux. Run from the project root: ./run_local.sh
set -e

cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Edit it if you need real Google/Anthropic keys."
  cp .env.example .env
fi

# Load .env into the shell so main.py/auth.py/bot.py can read them via os.environ
set -a
source .env
set +a

cd backend
echo "Starting Talkio on 0.0.0.0:8000 (reachable from other devices on this WiFi)..."
uvicorn main:app --host 0.0.0.0 --port 8000
