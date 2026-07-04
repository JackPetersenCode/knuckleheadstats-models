"""Join matchup_results with historical_nba_odds and compute per-season P&L.

Prediction logic: side with higher *_expected is the predicted winner.
Stake: $100 flat per game. Moneyline payout = American odds convention.
"""
import os
import psycopg2
from psycopg2.extras import DictCursor

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))

SEASONS = ["2016_17", "2017_18", "2018_19", "2019_20", "2020_21",
           "2021_22", "2022_23", "2023_24", "2024_25"]

STAKE = 100.0


def ml_payout(ml: int, won: bool) -> float:
    """Profit/loss on a $STAKE bet at American moneyline `ml`."""
    if not won:
        return -STAKE
    if ml > 0:
        return STAKE * (ml / 100.0)
    return STAKE * (100.0 / abs(ml))


def analyze():
    pg = psycopg2.connect(**PG)
    print(f"{'Season':<10}{'Bets':>7}{'W':>6}{'L':>6}{'Win%':>8}"
          f"{'Avg ML':>10}{'Risked':>12}{'Profit':>12}{'ROI%':>9}")
    print("-" * 80)

    totals = dict(bets=0, w=0, l=0, risked=0.0, profit=0.0, ml_sum=0.0)

    for season in SEASONS:
        season_label = season.replace("_", "-")
        sql = f"""
            SELECT m.home_team, m.visitor_team, m.home_expected, m.visitor_expected,
                   m.green_red, o.ml_home, o.ml_away
            FROM matchup_results_{season} m
            JOIN historical_nba_odds o
              ON o.game_date = m.game_date::date
             AND o.home_team = m.home_team
            WHERE m.green_red IN ('green','red')
              AND m.home_expected IS NOT NULL
              AND m.visitor_expected IS NOT NULL
              AND m.home_expected <> m.visitor_expected
        """
        with pg.cursor(cursor_factory=DictCursor) as c:
            c.execute(sql)
            rows = c.fetchall()

        bets = w = l = 0
        risked = profit = ml_sum = 0.0
        for r in rows:
            picked_home = r["home_expected"] > r["visitor_expected"]
            ml = r["ml_home"] if picked_home else r["ml_away"]
            if ml is None:
                continue
            won = r["green_red"] == "green"
            p = ml_payout(int(ml), won)
            bets += 1
            risked += STAKE
            profit += p
            ml_sum += int(ml)
            if won:
                w += 1
            else:
                l += 1

        if bets == 0:
            print(f"{season_label:<10}  no matches")
            continue
        win_pct = 100.0 * w / bets
        roi = 100.0 * profit / risked
        avg_ml = ml_sum / bets
        print(f"{season_label:<10}{bets:>7}{w:>6}{l:>6}{win_pct:>7.1f}%"
              f"{avg_ml:>10.1f}{risked:>12,.0f}{profit:>+12,.0f}{roi:>+8.2f}%")

        totals["bets"] += bets
        totals["w"] += w
        totals["l"] += l
        totals["risked"] += risked
        totals["profit"] += profit
        totals["ml_sum"] += ml_sum

    print("-" * 80)
    if totals["bets"]:
        win_pct = 100.0 * totals["w"] / totals["bets"]
        roi = 100.0 * totals["profit"] / totals["risked"]
        avg_ml = totals["ml_sum"] / totals["bets"]
        print(f"{'TOTAL':<10}{totals['bets']:>7}{totals['w']:>6}{totals['l']:>6}"
              f"{win_pct:>7.1f}%{avg_ml:>10.1f}{totals['risked']:>12,.0f}"
              f"{totals['profit']:>+12,.0f}{roi:>+8.2f}%")

    # diagnostic: unmatched / missing-odds counts per season
    print("\nDiagnostics (rows lost in join / NULL ml):")
    for season in SEASONS:
        with pg.cursor() as c:
            c.execute(f"""
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE o.game_date IS NULL) AS no_odds_match,
                  COUNT(*) FILTER (WHERE m.home_expected = m.visitor_expected) AS ties,
                  COUNT(*) FILTER (WHERE o.ml_home IS NULL OR o.ml_away IS NULL) AS null_ml
                FROM matchup_results_{season} m
                LEFT JOIN historical_nba_odds o
                  ON o.game_date = m.game_date::date
                 AND o.home_team = m.home_team
                WHERE m.green_red IN ('green','red')
            """)
            row = c.fetchone()
            print(f"  {season.replace('_','-')}: total={row[0]} "
                  f"unmatched={row[1]} ties={row[2]} null_ml={row[3]}")

    pg.close()


if __name__ == "__main__":
    analyze()
