@echo off
REM Talkio frontend-only launcher — for a friend's PC.
REM This does NOT run the backend — it only serves the frontend files so your
REM browser can load them. Edit frontend\config.js first with the backend's address,
REM or leave it blank and you'll be asked for it the first time the page loads.

cd /d "%~dp0frontend"
echo Starting Talkio frontend at http://localhost:5500 ...
echo Press Ctrl+C to stop.
python -m http.server 5500
