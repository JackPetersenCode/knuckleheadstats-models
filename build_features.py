"""Build the per-game feature table for MLB prediction.

For each game in mlb_games_{year} that has matching closing odds,
compute features using ONLY data available BEFORE game_date:

  * home/away team form (rolling 30 days): wpct, runs_for/g, runs_against/g
  * home/away starting pitcher (rolling 10 starts): era, k/9, bb/9, hr/9, ip/start
  * starter days rest
  * venue park factor (rolling 60 games): total runs/game
  * day/night flag, home advantage flag
  * target: home_is_winner (1 if home wins, 0 if away wins; ties dropped)
  * closing moneylines for ROI tests later

Output: table public.mlb_features
"""
import os
import psycopg2
from psycopg2.extras import execute_values
from datetime import date, timedelta

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
YEARS = [2021, 2022, 2023, 2024, 2025]


def main():
    pg = psycopg2.connect(**PG)
    pg.autocommit = False

    # 1) Materialize a single mlb_games_all view + pitcher_starts_all view
    print("Building game + pitcher consolidated views...")
    with pg.cursor() as c:
        c.execute("DROP TABLE IF EXISTS _all_games")
        c.execute("DROP TABLE IF EXISTS _all_starts")
        union_games = " UNION ALL ".join(
            f"""SELECT game_pk::int, game_date, home_team_id, home_team_name,
                       away_team_id, away_team_name, home_score, away_score,
                       home_is_winner, venue_id, venue_name, day_night
                FROM mlb_games_{y}
                WHERE detailed_state ILIKE 'Final%' AND game_type='R'
                  AND home_score IS NOT NULL AND away_score IS NOT NULL"""
            for y in YEARS
        )
        c.execute(f"CREATE TEMP TABLE _all_games AS {union_games}")
        c.execute("CREATE INDEX ON _all_games (game_date)")
        c.execute("CREATE INDEX ON _all_games (home_team_id, game_date)")
        c.execute("CREATE INDEX ON _all_games (away_team_id, game_date)")
        c.execute("CREATE INDEX ON _all_games (venue_id, game_date)")

        union_pit = " UNION ALL ".join(
            f"""SELECT p.game_pk, p.team_side, p.team_id, p.person_id,
                       p.innings_pitched::numeric AS ip, p.earned_runs, p.strike_outs,
                       p.base_on_balls, p.home_runs, p.batters_faced, p.pitches_thrown,
                       g.game_date
                FROM player_game_stats_pitching_{y} p
                JOIN mlb_games_{y} g ON g.game_pk::int = p.game_pk
                WHERE p.games_started=1
                  AND g.detailed_state ILIKE 'Final%' AND g.game_type='R'"""
            for y in YEARS
        )
        c.execute(f"CREATE TEMP TABLE _all_starts AS {union_pit}")
        c.execute("CREATE INDEX ON _all_starts (person_id, game_date)")
        c.execute("CREATE INDEX ON _all_starts (game_pk)")

        c.execute("SELECT COUNT(*) FROM _all_games"); print(f"  games: {c.fetchone()[0]}")
        c.execute("SELECT COUNT(*) FROM _all_starts"); print(f"  starts: {c.fetchone()[0]}")

    # 2) For each game, compute features. Big SQL with windowing.
    print("Computing per-game features (this may take a minute)...")
    sql = """
    DROP TABLE IF EXISTS mlb_features;
    CREATE TABLE mlb_features AS
    WITH
    -- Identify starters per game (joined back to games)
    starters AS (
        SELECT s.game_pk, s.team_side, s.person_id, s.team_id, s.game_date
        FROM _all_starts s
    ),
    home_starters AS (SELECT game_pk, person_id AS h_pid FROM starters WHERE team_side='home'),
    away_starters AS (SELECT game_pk, person_id AS a_pid FROM starters WHERE team_side='away'),

    -- Base game rows with starter ids
    base AS (
        SELECT g.*, h.h_pid, a.a_pid
        FROM _all_games g
        JOIN home_starters h ON h.game_pk = g.game_pk
        JOIN away_starters a ON a.game_pk = g.game_pk
    ),

    -- Team-game runs: produce one row per (team_id, game_date) with rs/ra
    team_games AS (
        SELECT home_team_id AS team_id, game_date,
               home_score AS rs, away_score AS ra,
               (home_is_winner=true)::int AS won
        FROM _all_games
        UNION ALL
        SELECT away_team_id, game_date,
               away_score, home_score,
               (home_is_winner=false)::int
        FROM _all_games
    ),

    -- Team 30-day rolling: join only games strictly before this date and within 30 days
    -- We'll compute as a correlated lateral subquery per row (slower but clear)
    feats AS (
        SELECT b.game_pk, b.game_date, b.home_team_id, b.away_team_id,
               b.home_team_name, b.away_team_name, b.home_score, b.away_score,
               b.home_is_winner::int AS y, b.venue_id, b.day_night,
               b.h_pid, b.a_pid,

               -- Home team last-30-day stats
               (SELECT AVG(won::float) FROM team_games tg
                  WHERE tg.team_id = b.home_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_wpct,
               (SELECT AVG(rs::float) FROM team_games tg
                  WHERE tg.team_id = b.home_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_rs,
               (SELECT AVG(ra::float) FROM team_games tg
                  WHERE tg.team_id = b.home_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_ra,

               -- Away team last-30-day stats
               (SELECT AVG(won::float) FROM team_games tg
                  WHERE tg.team_id = b.away_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_wpct,
               (SELECT AVG(rs::float) FROM team_games tg
                  WHERE tg.team_id = b.away_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_rs,
               (SELECT AVG(ra::float) FROM team_games tg
                  WHERE tg.team_id = b.away_team_id
                    AND tg.game_date < b.game_date
                    AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_ra,

               -- Park factor (last 60 games at venue, total runs/game)
               (SELECT AVG((home_score + away_score)::float) FROM _all_games v
                  WHERE v.venue_id = b.venue_id
                    AND v.game_date < b.game_date
                    AND v.game_date >= b.game_date - INTERVAL '90 days') AS park_rpg,

               b.day_night = 'night' AS is_night
        FROM base b
    )
    SELECT f.*,
           -- Home starter (prior 10 starts)
           hs.ip AS h_p_ip,  hs.er AS h_p_er,  hs.k AS h_p_k,  hs.bb AS h_p_bb,
           hs.hr AS h_p_hr,  hs.gs AS h_p_starts,  hs.days_rest AS h_p_rest,
           -- Away starter (prior 10 starts)
           aps.ip AS a_p_ip, aps.er AS a_p_er, aps.k AS a_p_k, aps.bb AS a_p_bb,
           aps.hr AS a_p_hr, aps.gs AS a_p_starts, aps.days_rest AS a_p_rest
    FROM feats f
    LEFT JOIN LATERAL (
      SELECT SUM(ip)::float AS ip, SUM(earned_runs)::int AS er,
             SUM(strike_outs)::int AS k, SUM(base_on_balls)::int AS bb,
             SUM(home_runs)::int AS hr, COUNT(*)::int AS gs,
             (f.game_date - MAX(game_date))::int AS days_rest
      FROM (
        SELECT * FROM _all_starts s
        WHERE s.person_id = f.h_pid AND s.game_date < f.game_date
        ORDER BY s.game_date DESC LIMIT 10
      ) sub
    ) hs ON TRUE
    LEFT JOIN LATERAL (
      SELECT SUM(ip)::float AS ip, SUM(earned_runs)::int AS er,
             SUM(strike_outs)::int AS k, SUM(base_on_balls)::int AS bb,
             SUM(home_runs)::int AS hr, COUNT(*)::int AS gs,
             (f.game_date - MAX(game_date))::int AS days_rest
      FROM (
        SELECT * FROM _all_starts s
        WHERE s.person_id = f.a_pid AND s.game_date < f.game_date
        ORDER BY s.game_date DESC LIMIT 10
      ) sub
    ) aps ON TRUE;
    """
    with pg.cursor() as c:
        c.execute(sql)
    pg.commit()

    # 3) Join odds and finalize
    print("Joining closing odds...")
    with pg.cursor() as c:
        c.execute("""
            ALTER TABLE mlb_features ADD COLUMN ml_home_close int;
            ALTER TABLE mlb_features ADD COLUMN ml_away_close int;
            UPDATE mlb_features f
            SET ml_home_close = o.ml_home_close,
                ml_away_close = o.ml_away_close
            FROM historical_mlb_odds o
            WHERE o.game_date = f.game_date AND o.home_team = f.home_team_name;
        """)
        c.execute("SELECT COUNT(*), COUNT(ml_home_close) FROM mlb_features")
        total, with_odds = c.fetchone()
        print(f"  features rows: {total}, with closing odds: {with_odds}")
    pg.commit()

    # Final coverage stats
    with pg.cursor() as c:
        c.execute("""
            SELECT EXTRACT(YEAR FROM game_date)::int AS yr, COUNT(*) AS n,
                   COUNT(ml_home_close) AS with_odds,
                   COUNT(*) FILTER (WHERE h_p_ip IS NOT NULL AND a_p_ip IS NOT NULL
                                      AND h_wpct IS NOT NULL AND a_wpct IS NOT NULL) AS with_features
            FROM mlb_features GROUP BY 1 ORDER BY 1
        """)
        print("\nFinal coverage:")
        for r in c.fetchall():
            print(f"  {r[0]}: total={r[1]}  with_odds={r[2]}  with_full_features={r[3]}")
    pg.close()
    print("\nDone. Table mlb_features ready.")


if __name__ == "__main__":
    main()
