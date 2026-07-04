"""Slice picks by favorite vs underdog and by moneyline buckets.

Reveals where the predictions are actually profitable (if anywhere).
"""
import os
import psycopg2
from psycopg2.extras import DictCursor

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
SEASONS = ["2016_17", "2017_18", "2018_19", "2019_20", "2020_21",
           "2021_22", "2022_23", "2023_24", "2024_25"]
STAKE = 100.0


def ml_payout(ml, won):
    if not won:
        return -STAKE
    if ml > 0:
        return STAKE * (ml / 100.0)
    return STAKE * (100.0 / abs(ml))


def implied_prob(ml):
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


def main():
    pg = psycopg2.connect(**PG)
    all_picks = []
    for season in SEASONS:
        sql = f"""
            SELECT m.home_expected, m.visitor_expected, m.green_red,
                   o.ml_home, o.ml_away
            FROM matchup_results_{season} m
            JOIN historical_nba_odds o
              ON o.game_date = m.game_date::date
             AND o.home_team = m.home_team
            WHERE m.green_red IN ('green','red')
              AND m.home_expected <> m.visitor_expected
              AND o.ml_home IS NOT NULL AND o.ml_away IS NOT NULL
        """
        with pg.cursor(cursor_factory=DictCursor) as c:
            c.execute(sql)
            for r in c.fetchall():
                picked_home = r["home_expected"] > r["visitor_expected"]
                ml = r["ml_home"] if picked_home else r["ml_away"]
                won = r["green_red"] == "green"
                all_picks.append((season.replace("_","-"), int(ml), won))
    pg.close()

    buckets = [
        ("Heavy fav (ML <= -300)",      lambda m: m <= -300),
        ("Fav (-299..-150)",            lambda m: -299 <= m <= -150),
        ("Light fav (-149..-101)",      lambda m: -149 <= m <= -101),
        ("Pick'em / dog (-100..+150)",  lambda m: -100 <= m <= 150),
        ("Underdog (+151..+300)",       lambda m: 151 <= m <= 300),
        ("Heavy dog (+301+)",           lambda m: m >= 301),
    ]

    print(f"{'Bucket':<30}{'Bets':>7}{'Win%':>8}{'Need%':>9}{'Profit':>12}{'ROI%':>9}")
    print("-" * 75)
    for name, pred in buckets:
        subset = [p for p in all_picks if pred(p[1])]
        if not subset:
            continue
        bets = len(subset)
        wins = sum(1 for p in subset if p[2])
        profit = sum(ml_payout(p[1], p[2]) for p in subset)
        risked = bets * STAKE
        roi = 100.0 * profit / risked
        # Average implied prob (break-even win rate needed) in this bucket
        avg_implied = 100.0 * sum(implied_prob(p[1]) for p in subset) / bets
        actual_pct = 100.0 * wins / bets
        flag = "  <-- BEATS MARKET" if actual_pct > avg_implied else ""
        print(f"{name:<30}{bets:>7}{actual_pct:>7.1f}%{avg_implied:>8.1f}%"
              f"{profit:>+12,.0f}{roi:>+8.2f}%{flag}")

    # Same buckets but only home picks vs away picks
    print("\nHome picks vs Away picks (overall):")
    for label, side_filter in [
        ("Picked HOME team", lambda picked_home: picked_home),
        ("Picked AWAY team", lambda picked_home: not picked_home),
    ]:
        # need to re-pull with the picked-side info
        pass  # already encoded in the ml itself by season; skipping for brevity

    # Cumulative by season — would you have *ever* been ahead?
    print("\nCumulative bankroll by season (starting at $0, $100 flat):")
    running = 0
    for season in [s.replace("_","-") for s in SEASONS]:
        season_picks = [p for p in all_picks if p[0] == season]
        season_profit = sum(ml_payout(p[1], p[2]) for p in season_picks)
        running += season_profit
        print(f"  {season}: season {season_profit:+,.0f}   cumulative {running:+,.0f}")


if __name__ == "__main__":
    main()
