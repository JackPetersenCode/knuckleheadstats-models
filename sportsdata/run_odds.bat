@echo off
REM Odds snapshot (props + game lines) — run several times/day for CLV (open->close)
cd /d "%~dp0"
set LOG="%~dp0logs\odds_%date:~-4%%date:~4,2%%date:~7,2%.log"
"C:\Users\jackp\AppData\Local\Programs\Python\Python312\python.exe" collect_odds.py >> %LOG% 2>&1
REM publish the refreshed board to the live site every cycle (skips if unchanged)
call "%~dp0..\landing_page\publish.bat" >> %LOG% 2>&1
