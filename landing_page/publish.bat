@echo off
REM Regenerate the picks/record data from the live value engine and publish to the
REM Fly site by committing the data files (the deploy-picks GitHub Action redeploys
REM on any landing_page change). Wired into run_stats.bat (daily) after value_grade.
cd /d "%~dp0"
set PY="C:\Users\jackp\AppData\Local\Programs\Python\Python312\python.exe"
%PY% update_record.py
cd /d "%~dp0.."
git add landing_page/picks.json landing_page/record.json landing_page/record.csv
git diff --cached --quiet && (echo publish: no data changes) || (git commit -m "picks: daily data refresh" && git push)
