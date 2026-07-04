@echo off
REM Odds snapshot (props + game lines) — run several times/day for CLV (open->close)
cd /d "%~dp0"
"C:\Users\jackp\AppData\Local\Programs\Python\Python312\python.exe" collect_odds.py >> "%~dp0logs\odds_%date:~-4%%date:~4,2%%date:~7,2%.log" 2>&1
