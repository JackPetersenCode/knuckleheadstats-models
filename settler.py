"""Settle picks in `daily_picks` once games complete.

For each row where settled_y IS NULL, fetches the game result from the MLB
Stats API live feed.  Computes won/lost relative to the bet and updates:
  - settled_y    (1 if home won, 0 if away won, NULL if not final yet)
  - settled_pl   ($ P&L for the recorded pick; NULL if no pick or game pending)

Usage:
  python settler.py [--days 14]    # settle anything within the last 14 days
"""
import os
import argparse
import psycopg2
import requests
from datetime import date, timedelta

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
STAKE = 100.0


def fetch_result(game_pk):
    """Returns (status, home_score, away_score) or (None, None, None) if missing."""
    try:
        r = requests.get(FEED_URL.format(game_pk), timeout=20)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return None, None, None, f"fetch error: {e}"
    gd = d.get("gameData", {}) or {}
    status = (gd.get("status", {}) or {}).get("abstractGameState")
    ld = d.get("liveData", {}) or {}
    linescore = ld.get("linescore", {}) or {}
    teams_ls = linescore.get("teams", {}) or {}
    home_score = (teams_ls.get("home", {}) or {}).get("runs")
    away_score = (teams_ls.get("away", {}) or {}).get("runs")
    return status, home_score, away_score, None


def pl_for_pick(pick, ml_home, ml_away, home_won):
    """Compute P&L for the recorded pick at flat $100 stake."""
    if pick is None:
        return None
    if pick == "HOME":
        ml = ml_home; won = home_won
    elif pick == "AWAY":
        ml = ml_away; won = not home_won
    else:
        return None
    if ml is None:
        return None
    if not won:
        return -STAKE
    return STAKE * (ml / 100.0) if ml > 0 else STAKE * (100.0 / abs(ml))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14,
                    help="settle picks dated within the last N days")
    args = ap.parse_args()
    cutoff = date.today() - timedelta(days=args.days)

    pg = psycopg2.connect(**PG)
    with pg.cursor() as c:
        c.execute("""
            SELECT game_pk, game_date, home_team, away_team,
                   pick, ml_home, ml_away
            FROM daily_picks
            WHERE game_date >= %s AND settled_y IS NULL
            ORDER BY game_date, game_pk
        """, (cutoff,))
        targets = c.fetchall()

    print(f"Targets to settle: {len(targets)} (game_date >= {cutoff})")
    updates = []
    n_final = n_pending = n_err = 0

    for game_pk, gd, home, away, pick, mh, ma in targets:
        status, hs, asc, err = fetch_result(game_pk)
        if err:
            print(f"  {game_pk}: ERROR {err}")
            n_err += 1; continue
        if status != "Final":
            print(f"  {game_pk} {away} @ {home}: status={status}  (not settling)")
            n_pending += 1; continue
        if hs is None or asc is None:
            print(f"  {game_pk}: Final but missing scores")
            n_err += 1; continue
        home_won = hs > asc
        pl = pl_for_pick(pick, mh, ma, home_won) if pick else None
        updates.append((1 if home_won else 0, pl, game_pk))
        pick_str = f"{pick} @ {mh if pick=='HOME' else ma:+}" if pick else "(no pick)"
        result_str = f"{home} {hs} - {asc} {away}  [{'HOME' if home_won else 'AWAY'} won]"
        pl_str = f"  P/L: {pl:+.0f}" if pl is not None else ""
        print(f"  {result_str}  pick={pick_str}{pl_str}")
        n_final += 1

    if updates:
        with pg.cursor() as c:
            c.executemany(
                "UPDATE daily_picks SET settled_y=%s, settled_pl=%s WHERE game_pk=%s",
                updates)
        pg.commit()

    print(f"\nSettled: {n_final}  pending: {n_pending}  errors: {n_err}")
    pg.close()


if __name__ == "__main__":
    main()
