"""Reporting on daily_picks.

Shows:
  - Overall ROI, win rate, profit, n bets
  - By week
  - By moneyline bucket
  - Sample of recent picks

Usage:
  python report.py [--days 30]
"""
import os
import argparse
import psycopg2
import pandas as pd
from datetime import date, timedelta

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
STAKE = 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    args = ap.parse_args()
    cutoff = date.today() - timedelta(days=args.days)

    pg = psycopg2.connect(**PG)
    df = pd.read_sql(f"""
        SELECT game_date, home_team, away_team, pick, ml_home, ml_away,
               model_p_home, p_home_fair, edge_home, edge_away,
               settled_y, settled_pl, scored_at
        FROM daily_picks
        WHERE game_date >= '{cutoff}'
        ORDER BY game_date, game_pk
    """, pg)
    pg.close()

    print(f"=== daily_picks since {cutoff} ===")
    print(f"  total games scored: {len(df)}")
    print(f"  games with pick:    {df['pick'].notna().sum()}")
    print(f"  games settled:      {df['settled_y'].notna().sum()}")
    print()

    bets = df[df["pick"].notna() & df["settled_pl"].notna()].copy()
    print(f"=== Settled bets: {len(bets)} ===")
    if len(bets) == 0:
        print("  (no settled bets yet)")
        return

    bets["won"] = bets["settled_pl"] > 0
    bets["ml"] = bets.apply(
        lambda r: r["ml_home"] if r["pick"] == "HOME" else r["ml_away"], axis=1)
    bets["risk"] = STAKE
    bets["edge_pct"] = bets.apply(
        lambda r: r["edge_home"] if r["pick"] == "HOME" else r["edge_away"], axis=1)

    profit = bets["settled_pl"].sum()
    risked = bets["risk"].sum()
    roi = 100 * profit / risked
    print(f"  win rate: {100*bets['won'].mean():.1f}% ({bets['won'].sum()}/{len(bets)})")
    print(f"  avg ML:   {bets['ml'].mean():+.1f}")
    print(f"  avg edge: {100*bets['edge_pct'].mean():+.2f} pp")
    print(f"  risked:   ${risked:,.0f}")
    print(f"  profit:   ${profit:+,.0f}")
    print(f"  ROI:      {roi:+.2f}%")

    # By week
    bets["week"] = pd.to_datetime(bets["game_date"]).dt.to_period("W").astype(str)
    by_week = bets.groupby("week").agg(
        n=("pick", "size"),
        wins=("won", "sum"),
        profit=("settled_pl", "sum"),
    ).reset_index()
    by_week["roi"] = 100 * by_week["profit"] / (by_week["n"] * STAKE)
    print("\nBy week:")
    print(by_week.to_string(index=False))

    # By edge bucket
    bets["edge_bucket"] = pd.cut(bets["edge_pct"],
        bins=[-1, 0.08, 0.10, 0.15, 1],
        labels=["thr (0.08-0.10)", "0.10-0.15", "0.15+", "huge"])
    by_edge = bets.groupby("edge_bucket", observed=True).agg(
        n=("pick", "size"),
        wins=("won", "sum"),
        profit=("settled_pl", "sum"),
    ).reset_index()
    by_edge["roi"] = 100 * by_edge["profit"] / (by_edge["n"] * STAKE)
    print("\nBy edge bucket:")
    print(by_edge.to_string(index=False))

    # By side
    by_side = bets.groupby("pick").agg(
        n=("pick", "size"),
        wins=("won", "sum"),
        profit=("settled_pl", "sum"),
    ).reset_index()
    by_side["roi"] = 100 * by_side["profit"] / (by_side["n"] * STAKE)
    print("\nBy side:")
    print(by_side.to_string(index=False))

    # Recent picks
    print("\nLast 10 settled bets:")
    cols = ["game_date", "home_team", "away_team", "pick", "ml", "edge_pct", "settled_pl"]
    print(bets[cols].tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
