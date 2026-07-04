"""V5: add weather, umpire, and lineup-aware batting features.

Builds:
  - _batter_sum: per (batter_id, game_pk, game_date) -> PA, K%, BB%, HR%, ISO, wOBA proxy
  - _ump_sum:    per (plate_umpire_id, game_pk, game_date) -> K% / BB% / runs/g of all PAs in that ump's game
  - mlb_features_v5: full feature row = mlb_features_v2 columns + new ones

New feature columns (added to mlb_features_v5):
  Weather:
    temp_f, wind_mph, is_dome, wind_helps_hitter, wind_helps_pitcher, weather_clear
  Umpire (rolling 60d):
    ump_k_rate, ump_bb_rate, ump_runs_pg
  Lineup-aware batting (rolling 14d, weighted by batting-order slot):
    h_lineup_woba, h_lineup_iso, h_lineup_k_rate, h_lineup_bb_rate, h_lineup_n_batters
    a_lineup_woba, a_lineup_iso, a_lineup_k_rate, a_lineup_bb_rate, a_lineup_n_batters
"""
import os
import time
import psycopg2

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
YEARS = [2021, 2022, 2023, 2024, 2025]


def timed(label):
    def deco(fn):
        def wrap(*a, **k):
            t = time.time(); print(f"[{label}] start")
            r = fn(*a, **k); print(f"[{label}] done in {time.time()-t:.1f}s")
            return r
        return wrap
    return deco


@timed("batter summary")
def build_batter_summary(c):
    c.execute("DROP TABLE IF EXISTS _batter_sum")
    union = " UNION ALL ".join(
        f"""SELECT pl.matchup_batter_id AS bid,
                   pl.game_pk, g.game_date,
                   pl.matchup_pitch_hand_code AS opp_hand,
                   pl.result_event AS ev
            FROM mlb_plays_{y} pl
            JOIN mlb_games_{y} g ON g.game_pk::int = pl.game_pk
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.matchup_batter_id IS NOT NULL
              AND pl.result_event IS NOT NULL"""
        for y in YEARS
    )
    c.execute(f"""
        CREATE TEMP TABLE _batter_sum AS
        SELECT bid, game_pk, game_date,
               COUNT(*) AS pa,
               COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float
                  / NULLIF(COUNT(*),0) AS k_rate,
               COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float
                  / NULLIF(COUNT(*),0) AS bb_rate,
               COUNT(*) FILTER (WHERE ev='Home Run')::float
                  / NULLIF(COUNT(*),0) AS hr_rate,
               (COUNT(*) FILTER (WHERE ev='Double')
                + 2*COUNT(*) FILTER (WHERE ev='Triple')
                + 3*COUNT(*) FILTER (WHERE ev='Home Run'))::float
                  / NULLIF(COUNT(*),0) AS iso,
               (0.7*COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk','Hit By Pitch'))
                + 0.9*COUNT(*) FILTER (WHERE ev='Single')
                + 1.25*COUNT(*) FILTER (WHERE ev='Double')
                + 1.6*COUNT(*) FILTER (WHERE ev='Triple')
                + 2.0*COUNT(*) FILTER (WHERE ev='Home Run'))::float
                  / NULLIF(COUNT(*),0) AS woba
        FROM ({union}) src
        GROUP BY bid, game_pk, game_date
    """)
    c.execute("CREATE INDEX ON _batter_sum (bid, game_date)")
    c.execute("SELECT COUNT(*) FROM _batter_sum")
    print(f"  batter-game rows: {c.fetchone()[0]}")


@timed("ump summary")
def build_ump_summary(c):
    c.execute("DROP TABLE IF EXISTS _ump_sum")
    # ump_sum per (ump, game) -> game-level k/bb/runs aggregates
    union_pa = " UNION ALL ".join(
        f"""SELECT pl.game_pk, g.game_date,
                   u.plate_umpire_id AS ump_id,
                   pl.result_event AS ev,
                   g.home_score + g.away_score AS total_runs
            FROM mlb_plays_{y} pl
            JOIN mlb_games_{y} g ON g.game_pk::int = pl.game_pk
            JOIN mlb_game_umpires u ON u.game_pk = g.game_pk::int
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.result_event IS NOT NULL
              AND u.plate_umpire_id IS NOT NULL"""
        for y in YEARS
    )
    c.execute(f"""
        CREATE TEMP TABLE _ump_sum AS
        SELECT ump_id, game_pk, game_date,
               COUNT(*) AS pa,
               COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float / COUNT(*) AS k_rate,
               COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float / COUNT(*) AS bb_rate,
               MAX(total_runs)::float AS runs
        FROM ({union_pa}) s
        GROUP BY ump_id, game_pk, game_date
    """)
    c.execute("CREATE INDEX ON _ump_sum (ump_id, game_date)")
    c.execute("SELECT COUNT(*) FROM _ump_sum")
    print(f"  ump-game rows: {c.fetchone()[0]}")


@timed("create v5 table")
def create_v5(c):
    c.execute("DROP TABLE IF EXISTS mlb_features_v5")
    c.execute("""
        CREATE TABLE mlb_features_v5 AS
        SELECT * FROM mlb_features_v2
        WHERE ml_home_close IS NOT NULL
    """)
    c.execute("ALTER TABLE mlb_features_v5 ADD PRIMARY KEY (game_pk)")
    c.execute("""
        ALTER TABLE mlb_features_v5
          -- weather
          ADD COLUMN temp_f int, ADD COLUMN wind_mph int,
          ADD COLUMN is_dome int, ADD COLUMN wind_helps_hitter int,
          ADD COLUMN wind_helps_pitcher int, ADD COLUMN weather_clear int,
          -- umpire rolling 60d
          ADD COLUMN ump_k_rate float, ADD COLUMN ump_bb_rate float,
          ADD COLUMN ump_runs_pg float, ADD COLUMN ump_n_games int,
          -- lineup-aware (home)
          ADD COLUMN h_lineup_woba float, ADD COLUMN h_lineup_iso float,
          ADD COLUMN h_lineup_k_rate float, ADD COLUMN h_lineup_bb_rate float,
          ADD COLUMN h_lineup_n_batters int,
          -- lineup-aware (away)
          ADD COLUMN a_lineup_woba float, ADD COLUMN a_lineup_iso float,
          ADD COLUMN a_lineup_k_rate float, ADD COLUMN a_lineup_bb_rate float,
          ADD COLUMN a_lineup_n_batters int
    """)


@timed("add weather")
def add_weather(c):
    c.execute("""
        UPDATE mlb_features_v5 f
        SET temp_f = w.temp_f,
            wind_mph = w.wind_mph,
            is_dome = CASE WHEN w.condition IN ('Dome','Roof Closed') THEN 1 ELSE 0 END,
            wind_helps_hitter = CASE
              WHEN w.wind_mph >= 5 AND w.wind_dir IS NOT NULL
                   AND w.wind_dir LIKE 'Out To%' THEN 1 ELSE 0 END,
            wind_helps_pitcher = CASE
              WHEN w.wind_mph >= 5 AND w.wind_dir IS NOT NULL
                   AND w.wind_dir LIKE 'In From%' THEN 1 ELSE 0 END,
            weather_clear = CASE WHEN w.condition IN ('Clear','Sunny','Partly Cloudy') THEN 1 ELSE 0 END
        FROM mlb_game_weather w
        WHERE w.game_pk = f.game_pk
    """)


@timed("add umpire rolling 60d")
def add_umpire(c):
    c.execute("""
        UPDATE mlb_features_v5 f
        SET ump_k_rate = sub.k_rate,
            ump_bb_rate = sub.bb_rate,
            ump_runs_pg = sub.runs_pg,
            ump_n_games = sub.n_games
        FROM (
          SELECT f.game_pk,
                 SUM(us.pa*us.k_rate)::float / NULLIF(SUM(us.pa),0) AS k_rate,
                 SUM(us.pa*us.bb_rate)::float / NULLIF(SUM(us.pa),0) AS bb_rate,
                 AVG(us.runs)::float AS runs_pg,
                 COUNT(*)::int AS n_games
          FROM mlb_features_v5 f
          JOIN mlb_game_umpires u  ON u.game_pk = f.game_pk
          JOIN _ump_sum         us ON us.ump_id = u.plate_umpire_id
                                  AND us.game_date < f.game_date
                                  AND us.game_date >= f.game_date - INTERVAL '60 days'
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)


@timed("add lineup-aware batting")
def add_lineup(c):
    """For each starting batter, look up their rolling 14-day (PA-weighted) stats
    and average across the lineup, with order weighting (top of order = more PA)."""
    # Order weight: 1st = 1.20, 2nd = 1.12, 3rd = 1.10, ... 9th = 0.85
    # Approximate using 1.2 - 0.05*(slot-1)
    for side in ("home", "away"):
        prefix = side[0]  # 'h' or 'a'
        c.execute(f"""
            UPDATE mlb_features_v5 f
            SET {prefix}_lineup_woba    = sub.woba,
                {prefix}_lineup_iso     = sub.iso,
                {prefix}_lineup_k_rate  = sub.k_rate,
                {prefix}_lineup_bb_rate = sub.bb_rate,
                {prefix}_lineup_n_batters = sub.n_b
            FROM (
              SELECT f.game_pk,
                     SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa * agg.woba)::float
                       / NULLIF(SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa), 0) AS woba,
                     SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa * agg.iso)::float
                       / NULLIF(SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa), 0) AS iso,
                     SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa * agg.k_rate)::float
                       / NULLIF(SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa), 0) AS k_rate,
                     SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa * agg.bb_rate)::float
                       / NULLIF(SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa), 0) AS bb_rate,
                     COUNT(DISTINCT l.player_id)::int AS n_b
              FROM mlb_features_v5 f
              JOIN mlb_game_lineups l
                ON l.game_pk = f.game_pk AND l.team_side = '{side}'
              JOIN LATERAL (
                  SELECT
                    SUM(pa)::float AS pa,
                    SUM(pa*woba)::float / NULLIF(SUM(pa),0)   AS woba,
                    SUM(pa*iso)::float  / NULLIF(SUM(pa),0)   AS iso,
                    SUM(pa*k_rate)::float / NULLIF(SUM(pa),0) AS k_rate,
                    SUM(pa*bb_rate)::float / NULLIF(SUM(pa),0) AS bb_rate
                  FROM _batter_sum b
                  WHERE b.bid = l.player_id
                    AND b.game_date < f.game_date
                    AND b.game_date >= f.game_date - INTERVAL '21 days'
              ) agg ON TRUE
              GROUP BY f.game_pk
            ) sub
            WHERE sub.game_pk = f.game_pk
        """)
        print(f"  {side} lineup done")


def main():
    pg = psycopg2.connect(**PG); pg.autocommit = False
    with pg.cursor() as c:
        build_batter_summary(c)
        build_ump_summary(c)
        create_v5(c)
        add_weather(c)
        add_umpire(c)
        add_lineup(c)

        c.execute("""
            SELECT EXTRACT(YEAR FROM game_date)::int AS yr,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE temp_f IS NOT NULL) AS w_filled,
                   COUNT(*) FILTER (WHERE ump_k_rate IS NOT NULL) AS u_filled,
                   COUNT(*) FILTER (WHERE h_lineup_woba IS NOT NULL AND a_lineup_woba IS NOT NULL) AS l_filled
            FROM mlb_features_v5 GROUP BY 1 ORDER BY 1
        """)
        print("\nFinal coverage:")
        for r in c.fetchall():
            print(f"  {r[0]}: total={r[1]} weather={r[2]} ump={r[3]} lineup={r[4]}")
    pg.commit()
    pg.close()


if __name__ == "__main__":
    main()
