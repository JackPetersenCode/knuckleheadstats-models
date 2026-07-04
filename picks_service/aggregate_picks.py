"""Daily multi-source pick aggregator.

Pulls picks from:
  1. v5 MLB home-underdog model (uses daily_picks table populated by daily_picker.py)
  2. Reverse-line-movement (RLM) signal from current vs opening odds
  3. (Future) NHL/NFL open-source models when in-season

Ranks picks by "edge score", separates VIP from free, writes to:
  - picks_published   (one row per published pick with confidence tier)
"""
import argparse
from datetime import date
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import execute_values, RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG, FREE_EDGE_THRESHOLD, VIP_EDGE_THRESHOLD, FREE_PICKS_PER_DAY, VIP_PICKS_PER_DAY


def ensure_table(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS picks_published (
                pick_id      bigserial PRIMARY KEY,
                game_pk      integer,
                game_date    date,
                sport        varchar(10),
                home_team    varchar(60),
                away_team    varchar(60),
                pick_side    varchar(20),   -- "HOME" / "AWAY" / "OVER" / "UNDER" / etc
                market       varchar(20),   -- "moneyline" / "spread" / "total" / "prop"
                ml_price     integer,
                edge_pct     numeric,
                source       varchar(60),   -- "v5_homedog" / "rlm" / "open_source_nhl"
                tier         varchar(8),    -- "free" / "vip"
                stake_units  numeric,       -- recommended stake in units
                rationale    text,          -- one-line explanation for the post
                created_at   timestamptz default now(),
                settled_y    integer,       -- 1 won, 0 lost, NULL pending
                settled_pl   numeric
            );
            CREATE INDEX IF NOT EXISTS idx_pp_date_tier ON picks_published (game_date, tier);
        """)
    pg.commit()


def pull_v5_picks(pg, game_date):
    """v5 daily_picks rows from today; respect home-dog filter."""
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM daily_picks
            WHERE game_date = %s AND pick IS NOT NULL
              AND model_version = 'v5_seedavg_homedog'
            ORDER BY edge_home DESC NULLS LAST
        """, (game_date,))
        rows = c.fetchall()
    out = []
    for r in rows:
        edge = float(r["edge_home"]) if r["pick"] == "HOME" else float(r["edge_away"])
        ml   = int(r["ml_home"])    if r["pick"] == "HOME" else int(r["ml_away"])
        team = r["home_team"]       if r["pick"] == "HOME" else r["away_team"]
        out.append(dict(
            game_pk=r["game_pk"], game_date=r["game_date"], sport="MLB",
            home_team=r["home_team"], away_team=r["away_team"],
            pick_side=r["pick"], market="moneyline", ml_price=ml,
            edge_pct=edge, source="v5_homedog",
            rationale=f"{team} +{ml}: model edge {edge*100:+.1f}pp; "
                      f"home underdog with our strongest validated signal."))
    return out


def pull_rlm_signals(pg, game_date):
    """Reverse line movement: where current closing line is BETTER for the
    underdog than the opening line. That signals sharp money on the dog.
    Uses historical_mlb_odds which has both open + close.
    """
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM historical_mlb_odds
            WHERE game_date = %s
              AND ml_home_open IS NOT NULL AND ml_away_open IS NOT NULL
              AND ml_home_close IS NOT NULL AND ml_away_close IS NOT NULL
        """, (game_date,))
        rows = c.fetchall()
    out = []
    for r in rows:
        # If ml_home_close - ml_home_open > 0 it means the home line got worse
        # for the home team (e.g., -150 -> -130 or +120 -> +140).  i.e., money
        # moved AWAY from home -> we'd bet AWAY.  And vice versa.
        # We want significant moves: > 15 cents.
        h_move = (r["ml_home_close"] or 0) - (r["ml_home_open"] or 0)
        a_move = (r["ml_away_close"] or 0) - (r["ml_away_open"] or 0)
        # Significant sharp move = one side moved by 15+ cents AGAINST the
        # market's normal direction
        if h_move >= 15 and r["ml_away_close"] is not None:
            out.append(dict(
                game_pk=None, game_date=r["game_date"], sport="MLB",
                home_team=r["home_team"], away_team=r["away_team"],
                pick_side="AWAY", market="moneyline", ml_price=int(r["ml_away_close"]),
                edge_pct=0.04 + (h_move / 200.0),  # rough edge from move size
                source="rlm",
                rationale=f"{r['away_team']}: sharp money moved line {h_move} cents "
                          f"toward {r['away_team']} since open."))
        elif a_move >= 15 and r["ml_home_close"] is not None:
            out.append(dict(
                game_pk=None, game_date=r["game_date"], sport="MLB",
                home_team=r["home_team"], away_team=r["away_team"],
                pick_side="HOME", market="moneyline", ml_price=int(r["ml_home_close"]),
                edge_pct=0.04 + (a_move / 200.0),
                source="rlm",
                rationale=f"{r['home_team']}: sharp money moved line {a_move} cents "
                          f"toward {r['home_team']} since open."))
    return out


def dedupe_and_rank(picks):
    """Don't publish two picks for the same game. Prefer v5 over RLM if both fire
    on same side; if they conflict, drop both."""
    by_game = {}
    for p in picks:
        key = (p["game_date"], p["home_team"], p["away_team"])
        if key not in by_game:
            by_game[key] = p
            continue
        other = by_game[key]
        if other["pick_side"] != p["pick_side"]:
            by_game[key] = None  # conflict — drop both
            continue
        # both same side: boost edge, prefer v5 source label
        if p["source"] == "v5_homedog":
            by_game[key] = p
        by_game[key]["edge_pct"] = max(other["edge_pct"], p["edge_pct"])
    return [p for p in by_game.values() if p is not None]


def assign_tier(picks):
    """Top N highest-edge -> VIP. A subset of the top edge ones also go to free."""
    picks_sorted = sorted(picks, key=lambda x: -x["edge_pct"])
    vip = [p for p in picks_sorted if p["edge_pct"] >= VIP_EDGE_THRESHOLD][:VIP_PICKS_PER_DAY]
    free_candidates = [p for p in vip if p["edge_pct"] >= FREE_EDGE_THRESHOLD][:FREE_PICKS_PER_DAY]
    free_set = {(p["game_date"], p["home_team"], p["away_team"]) for p in free_candidates}
    for p in vip:
        p["tier"] = "free" if (p["game_date"], p["home_team"], p["away_team"]) in free_set else "vip"
        # recommend 1 unit stake; could refine with Kelly later
        p["stake_units"] = 1.0 if p["tier"] == "free" else (
            1.5 if p["edge_pct"] >= 0.10 else 1.0)
    return vip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    args = ap.parse_args()

    pg = psycopg2.connect(**PG)
    ensure_table(pg)

    picks = []
    picks.extend(pull_v5_picks(pg, args.date))
    picks.extend(pull_rlm_signals(pg, args.date))
    picks = dedupe_and_rank(picks)
    picks = assign_tier(picks)

    if not picks:
        print(f"No picks meet thresholds for {args.date}.")
        return

    rows = [(p.get("game_pk"), p["game_date"], p["sport"],
             p["home_team"], p["away_team"], p["pick_side"], p["market"],
             p.get("ml_price"), p["edge_pct"], p["source"], p["tier"],
             p["stake_units"], p["rationale"]) for p in picks]
    with pg.cursor() as c:
        execute_values(c,
            "INSERT INTO picks_published "
            "(game_pk, game_date, sport, home_team, away_team, pick_side, market, "
            "ml_price, edge_pct, source, tier, stake_units, rationale) VALUES %s",
            rows)
    pg.commit()

    print(f"Published {len(picks)} picks for {args.date}:")
    for p in picks:
        print(f"  [{p['tier'].upper():>4}] {p['pick_side']:>4} "
              f"{p['home_team'] if p['pick_side']=='HOME' else p['away_team']:<22} "
              f"@ {p['ml_price']:+}  edge={p['edge_pct']*100:+.1f}pp  "
              f"({p['source']})")
    pg.close()


if __name__ == "__main__":
    main()
