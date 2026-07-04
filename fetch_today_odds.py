"""Auto-fetch today's MLB closing odds; write today_odds.csv for daily_picker.

Tries two sources, in order of preference:
  1. The Odds API (the-odds-api.com) — accurate, current, requires key.
  2. Fallback: skip — `daily_picker.py` will just produce model probabilities
     without market comparison.

Usage:
  python fetch_today_odds.py [YYYY-MM-DD]

Env vars:
  ODDS_API_KEY  - your the-odds-api.com key (free tier: 500 req/mo)
"""
import argparse
import csv
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import requests

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
OUT = Path(r"c:\Users\jackp\Desktop\new_game\today_odds.csv")
SCHED_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={}"


def get_scheduled_games(date_str):
    r = requests.get(SCHED_URL.format(date_str), timeout=20)
    r.raise_for_status()
    out = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            if g.get("gameType") != "R":
                continue
            out.append(dict(
                game_pk=g["gamePk"],
                home_team=g["teams"]["home"]["team"]["name"],
                away_team=g["teams"]["away"]["team"]["name"],
            ))
    return out


def fetch_odds_api():
    """Return list of dicts: {home_team, away_team, ml_home, ml_away}.
    Averages prices across all books returned."""
    if not ODDS_API_KEY:
        return None
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    params = dict(
        apiKey=ODDS_API_KEY,
        regions="us",
        markets="h2h",
        oddsFormat="american",
        dateFormat="iso",
    )
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Odds API error: {e}")
        return None

    out = []
    for ev in data:
        home = ev.get("home_team")
        away = ev.get("away_team")
        h_prices, a_prices = [], []
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for o in mk.get("outcomes", []):
                    if o["name"] == home:
                        h_prices.append(int(o["price"]))
                    elif o["name"] == away:
                        a_prices.append(int(o["price"]))
        if h_prices and a_prices:
            out.append(dict(
                home_team=home, away_team=away,
                ml_home=int(round(sum(h_prices) / len(h_prices))),
                ml_away=int(round(sum(a_prices) / len(a_prices))),
                n_books=len(h_prices),
            ))
    return out


def match_to_game_pks(scheduled, odds):
    """odds row team names may not exactly match MLB API names. Normalize.

    Most differences are 'San Francisco Giants' vs 'SF Giants'. We do
    substring match on team names to bridge."""
    matched = []
    odds_by_team = {(o["home_team"], o["away_team"]): o for o in odds}
    # First pass: exact match
    for g in scheduled:
        key = (g["home_team"], g["away_team"])
        if key in odds_by_team:
            o = odds_by_team[key]
            matched.append(dict(game_pk=g["game_pk"], ml_home=o["ml_home"], ml_away=o["ml_away"]))
    # Second pass: substring match for anything not yet matched
    matched_pks = {m["game_pk"] for m in matched}
    for g in scheduled:
        if g["game_pk"] in matched_pks: continue
        for o in odds:
            if (g["home_team"].split()[-1] in o["home_team"]
                and g["away_team"].split()[-1] in o["away_team"]):
                matched.append(dict(game_pk=g["game_pk"], ml_home=o["ml_home"], ml_away=o["ml_away"]))
                break
    return matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    args = ap.parse_args()

    print(f"Fetching today_odds.csv for {args.date}...")
    scheduled = get_scheduled_games(args.date)
    print(f"  {len(scheduled)} MLB games scheduled")

    odds = fetch_odds_api()
    if not odds:
        print("  No odds source available (set ODDS_API_KEY).")
        print(f"  Writing empty CSV anyway so daily_picker.py runs without --odds.")
        with OUT.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["game_pk", "ml_home", "ml_away"])
        return
    print(f"  {len(odds)} games priced by Odds API")

    matched = match_to_game_pks(scheduled, odds)
    print(f"  matched {len(matched)} of {len(scheduled)}")

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game_pk", "ml_home", "ml_away"])
        for m in matched:
            w.writerow([m["game_pk"], m["ml_home"], m["ml_away"]])
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
