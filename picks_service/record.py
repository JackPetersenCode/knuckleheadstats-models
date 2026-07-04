"""Public record dashboard. Print/export your honest cumulative W-L-Profit.

Usage:
  python record.py                        # full history
  python record.py --days 30              # last 30 days
  python record.py --export record.csv    # export CSV

Use this output in your bio/website as the "verified record" link.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--export", help="optional CSV output path")
    ap.add_argument("--tier", default=None, help="filter to 'free' or 'vip'")
    args = ap.parse_args()

    pg = psycopg2.connect(**PG)
    where = ["settled_y IS NOT NULL"]
    if args.days:
        cutoff = date.today() - timedelta(days=args.days)
        where.append(f"game_date >= '{cutoff}'")
    if args.tier:
        where.append(f"tier = '{args.tier}'")

    df = pd.read_sql(
        f"""SELECT game_date, sport, pick_side, home_team, away_team,
                   ml_price, stake_units, tier, source, settled_y, settled_pl
            FROM picks_published WHERE {' AND '.join(where)}
            ORDER BY game_date""", pg)
    pg.close()

    if df.empty:
        print("No settled picks yet.")
        return

    df["won"] = df["settled_y"] == 1
    wins = df["won"].sum(); n = len(df)
    profit_units = df["settled_pl"].astype(float).sum()
    risked_units = df["stake_units"].astype(float).sum()
    roi = 100 * profit_units / max(risked_units, 0.01)

    print(f"=== Public record ({date.today() - timedelta(days=args.days or 9999)} -> today) ===")
    if args.tier: print(f"  Tier: {args.tier}")
    print(f"  Picks:    {n}")
    print(f"  W-L:      {wins}-{n-wins}  ({100*wins/n:.1f}% win rate)")
    print(f"  Units:    risked {risked_units:.1f}u, profit {profit_units:+.2f}u")
    print(f"  ROI:      {roi:+.2f}%")
    print()

    # Per sport
    print("By sport:")
    print(df.groupby("sport").agg(
        n=("won", "size"), wins=("won", "sum"),
        units=("settled_pl", lambda x: float(x.astype(float).sum())),
    ).to_string())
    print()

    # Per source
    print("By source:")
    print(df.groupby("source").agg(
        n=("won", "size"), wins=("won", "sum"),
        units=("settled_pl", lambda x: float(x.astype(float).sum())),
    ).to_string())

    if args.export:
        df.to_csv(args.export, index=False)
        print(f"\nExported to {args.export}")


if __name__ == "__main__":
    main()
