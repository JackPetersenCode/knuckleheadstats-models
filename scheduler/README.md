# Windows Task Scheduler Setup

Three scheduled tasks automate the daily workflow. Import each XML file via:

**Task Scheduler → Action → Import Task...**

Adjust the paths inside each XML if your repo lives elsewhere.

| File | Schedule | What it does |
|---|---|---|
| `morning_picks.xml` | Daily 11:05am ET | Fetch odds, score games, publish picks |
| `evening_settle.xml` | Daily 11:30pm ET | Settle yesterday's picks, refresh record |
| `weekly_retrain.xml` | Monday 6:00am ET | Retrain v5 model with latest week of data |

If your machine is set to a different timezone, edit the `<StartBoundary>`
fields in each XML.

## Pre-flight checklist

Before enabling these:

1. ☐ `v5_seedavg_model.pkl` exists
2. ☐ `ODDS_API_KEY` env var set system-wide (Control Panel → Environment Variables)
3. ☐ Discord webhook env vars set
4. ☐ All three scripts run successfully manually:
   ```powershell
   python c:\Users\jackp\Desktop\new_game\fetch_today_odds.py
   python c:\Users\jackp\Desktop\new_game\daily_picker.py --odds c:\Users\jackp\Desktop\new_game\today_odds.csv
   python c:\Users\jackp\Desktop\new_game\picks_service\aggregate_picks.py
   python c:\Users\jackp\Desktop\new_game\picks_service\discord_post.py
   ```
5. ☐ Logs directory exists: `c:\Users\jackp\Desktop\new_game\logs\`

## Logs

Each task writes stdout/stderr to `logs/{task}_{date}.log` so you can debug
failures the next morning.

## Disabling for off-season

When MLB season ends (end of October), disable `morning_picks.xml` to avoid
wasting Odds API requests on empty schedules. Re-enable in April.
