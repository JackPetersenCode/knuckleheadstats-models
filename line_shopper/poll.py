"""Poll The Odds API for current lines across configured books and markets.

Stores each (sport, event_id, market, book, side) line in `line_snapshots`
with a timestamp. Detection runs against this table.

Usage:
  python poll.py            # one-shot poll of all sports
  python poll.py --loop     # continuous poll every POLL_INTERVAL_SECONDS
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (ODDS_API_KEY, ODDS_BASE, SPORTS, BOOKMAKERS, MARKETS,
                    PG, POLL_INTERVAL_SECONDS)


def ensure_schema(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS line_snapshots (
                snap_id     bigserial PRIMARY KEY,
                snap_time   timestamptz NOT NULL,
                sport       varchar(40) NOT NULL,
                event_id    varchar(64) NOT NULL,
                commence    timestamptz,
                home_team   varchar(80),
                away_team   varchar(80),
                book        varchar(20) NOT NULL,
                market      varchar(20) NOT NULL,
                side        varchar(40) NOT NULL,   -- team name or "OVER"/"UNDER" + point
                price_usd   integer NOT NULL,        -- American odds
                point       numeric                  -- spread or total line, if applicable
            );
            CREATE INDEX IF NOT EXISTS idx_ls_event_market
                ON line_snapshots (event_id, market, snap_time DESC);
            CREATE INDEX IF NOT EXISTS idx_ls_recent
                ON line_snapshots (snap_time DESC);
        """)
    pg.commit()


def poll_one_sport(sport):
    """Return list of snapshot rows from Odds API for this sport."""
    if not ODDS_API_KEY:
        print(f"  [skip {sport}] ODDS_API_KEY not set")
        return []
    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = dict(
        apiKey=ODDS_API_KEY,
        regions="us,eu",
        markets=",".join(MARKETS),
        bookmakers=",".join(BOOKMAKERS),
        oddsFormat="american",
        dateFormat="iso",
    )
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [error {sport}] {e}")
        return []

    now = datetime.now(timezone.utc)
    rows = []
    for ev in data:
        event_id = ev["id"]
        commence = ev["commence_time"]
        home = ev.get("home_team")
        away = ev.get("away_team")
        for bk in ev.get("bookmakers", []):
            book = bk["key"]
            for mk in bk.get("markets", []):
                market = mk["key"]
                for o in mk.get("outcomes", []):
                    side = o["name"]
                    price = int(o["price"])
                    point = o.get("point")
                    rows.append((now, sport, event_id, commence, home, away,
                                 book, market, side, price, point))
    print(f"  [{sport}] {len(rows)} rows  ({len(data)} events)")
    return rows


def insert(pg, rows):
    if not rows: return
    with pg.cursor() as c:
        execute_values(c,
            "INSERT INTO line_snapshots "
            "(snap_time, sport, event_id, commence, home_team, away_team, "
            "book, market, side, price_usd, point) VALUES %s",
            rows)
    pg.commit()


def poll_once():
    pg = psycopg2.connect(**PG)
    ensure_schema(pg)
    print(f"\nPoll at {datetime.now().isoformat(timespec='seconds')}")
    all_rows = []
    for sport in SPORTS:
        all_rows.extend(poll_one_sport(sport))
    insert(pg, all_rows)
    print(f"  inserted: {len(all_rows)} rows")
    pg.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    if args.loop:
        while True:
            try:
                poll_once()
            except Exception as e:
                print(f"poll error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
    else:
        poll_once()


if __name__ == "__main__":
    main()
