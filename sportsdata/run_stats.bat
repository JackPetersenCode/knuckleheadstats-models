@echo off
REM Daily refresh: stats (last 3 days) -> crosswalk -> grade props vs outcomes (+ CLV)
cd /d "%~dp0"
set PY="C:\Users\jackp\AppData\Local\Programs\Python\Python312\python.exe"
set LOG="%~dp0logs\stats_%date:~-4%%date:~4,2%%date:~7,2%.log"
%PY% -c "import db; db.init_schema_v3()" >> %LOG% 2>&1
%PY% collect.py --sport all --days 3 >> %LOG% 2>&1
%PY% collect_advanced.py --days 3 >> %LOG% 2>&1
%PY% xwalk.py >> %LOG% 2>&1
%PY% grade.py >> %LOG% 2>&1
%PY% value_grade.py >> %LOG% 2>&1
