@echo off
REM Launch the GM Tool web app, then open http://127.0.0.1:5050
cd /d "%~dp0"
echo Starting GM Tool on http://127.0.0.1:5050  (Ctrl+C to stop)
start "" http://127.0.0.1:5050
python -m gm.app
