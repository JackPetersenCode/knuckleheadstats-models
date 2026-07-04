"""Settle published picks once games complete. Fetches results from MLB Stats
API and updates picks_published.settled_y and settled_pl in UNITS.

Usage:
  python settler.py [--days 7]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG

FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"


def ml_to_units(ml, won, stake_units):
    """Return P&L in units (relative to 1 unit = $100 stake)."""
    if not won:
        return -float(stake_units)
    return float(stake_units) * (ml / 100.0 if ml > 0 else 100.0 / abs(ml))


def fetch_result(game_pk):
    try:
        r = requests.get(FEED_URL.format(game_pk), timeout=20)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None, None, None
    gd = d.get("gameData", {}) or {}
    status = (gd.get("status", {}) or {}).get("abstractGameState")
    ld = d.get("liveData", {}) or {}
    ls = ld.get("linescore", {}) or {}
    teams_ls = ls.get("teams", {}) or {}
    hs = (teams_ls.get("home", {}) or {}).get("runs")
    asc = (teams_ls.get("away", {}) or {}).get("runs")
    return status, hs, asc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    cutoff = date.today() - timedelta(days=args.days)

    pg = psycopg2.connect(**PG)
    with pg.cursor() as c:
        c.execute("""
            SELECT pick_id, game_pk, pick_side, ml_price, stake_units, home_team, away_team
            FROM picks_published
            WHERE game_date >= %s AND settled_y IS NULL AND game_pk IS NOT NULL
        """, (cutoff,))
        targets = c.fetchall()
    print(f"Targets: {len(targets)}")

    updates = []
    for pick_id, gp, pick_side, ml, units, home, away in targets:
        status, hs, asc = fetch_result(gp)
        if status != "Final" or hs is None or asc is None:
            print(f"  pick_id={pick_id} ({away}@{home}): {status} — skip")
            continue
        home_won = hs > asc
        won = (pick_side == "HOME" and home_won) or (pick_side == "AWAY" and not home_won)
        pl = ml_to_units(int(ml), won, float(units))
        updates.append((1 if home_won else 0, pl, pick_id))
        print(f"  {away} {asc}-{hs} {home}  pick={pick_side}@{ml:+}  "
              f"{'WIN' if won else 'LOSS'}  P/L: {pl:+.2f}u")

    if updates:
        with pg.cursor() as c:
            c.executemany(
                "UPDATE picks_published SET settled_y=%s, settled_pl=%s WHERE pick_id=%s",
                updates)
        pg.commit()
    print(f"Settled {len(updates)} picks.")
    pg.close()


if __name__ == "__main__":
    main()
