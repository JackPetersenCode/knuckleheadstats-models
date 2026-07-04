"""Refreshes record.json and record.csv next to index.html.

Run weekly (or after each batch settles). The landing page fetches record.json
to populate the verified-record strip at the top.

Usage:
  python update_record.py
"""
import os
import csv
import json
from datetime import date, timedelta
from pathlib import Path
import psycopg2

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
OUT = Path(__file__).resolve().parent


def main():
    pg = psycopg2.connect(**PG)
    cutoff = date.today() - timedelta(days=30)

    with pg.cursor() as c:
        c.execute("""
            SELECT
              COUNT(*) FILTER (WHERE settled_y IS NOT NULL),
              COUNT(*) FILTER (WHERE settled_y = 1),
              COUNT(*) FILTER (WHERE settled_y = 0),
              COALESCE(SUM(settled_pl), 0)::float
            FROM picks_published
            WHERE game_date >= %s AND settled_y IS NOT NULL
        """, (cutoff,))
        settled, wins, losses, profit = c.fetchone()

    record = dict(
        as_of=str(date.today()),
        window_days=30,
        settled_30=int(settled),
        wins_30=int(wins),
        losses_30=int(losses),
        profit_units_30=float(profit),
    )
    (OUT / "record.json").write_text(json.dumps(record, indent=2))
    print(f"Wrote {OUT/'record.json'}:")
    print(json.dumps(record, indent=2))

    # Full audit CSV
    with pg.cursor() as c:
        c.execute("""
            SELECT game_date, sport, pick_side, home_team, away_team,
                   ml_price, stake_units, settled_y, settled_pl, source
            FROM picks_published
            WHERE settled_y IS NOT NULL
            ORDER BY game_date, pick_id
        """)
        rows = c.fetchall()
        cols = [d[0] for d in c.description]

    with (OUT / "record.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Wrote {OUT/'record.csv'}: {len(rows)} rows")
    pg.close()


if __name__ == "__main__":
    main()
