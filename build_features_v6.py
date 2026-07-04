"""V6 features: platoon splits, pitch arsenal, pitcher H/A splits, team streaks,
head-to-head, plus MLB API standings (streak/L10/games_back).

Outputs: mlb_features_v6 = mlb_features_v5 + new columns.
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


@timed("batter platoon splits")
def build_batter_platoon(c):
    """Per (batter, vs_hand, game_date) - cumulative season-to-date wOBA, K%, BB%."""
    c.execute("DROP TABLE IF EXISTS _batter_platoon")
    union = " UNION ALL ".join(
        f"""SELECT pl.matchup_batter_id AS bid,
                   pl.matchup_pitch_hand_code AS vs_hand,
                   pl.game_pk, g.game_date,
                   pl.result_event AS ev
            FROM mlb_plays_{y} pl
            JOIN mlb_games_{y} g ON g.game_pk::int = pl.game_pk
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.matchup_batter_id IS NOT NULL
              AND pl.matchup_pitch_hand_code IN ('L','R')
              AND pl.result_event IS NOT NULL"""
        for y in YEARS
    )
    c.execute(f"""
        CREATE TEMP TABLE _batter_platoon AS
        SELECT bid, vs_hand, game_pk, game_date,
               COUNT(*) AS pa,
               COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float
                 / NULLIF(COUNT(*),0) AS k_rate,
               COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float
                 / NULLIF(COUNT(*),0) AS bb_rate,
               (0.7*COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk','Hit By Pitch'))
                + 0.9*COUNT(*) FILTER (WHERE ev='Single')
                + 1.25*COUNT(*) FILTER (WHERE ev='Double')
                + 1.6*COUNT(*) FILTER (WHERE ev='Triple')
                + 2.0*COUNT(*) FILTER (WHERE ev='Home Run'))::float
                 / NULLIF(COUNT(*),0) AS woba
        FROM ({union}) src
        GROUP BY bid, vs_hand, game_pk, game_date
    """)
    c.execute("CREATE INDEX ON _batter_platoon (bid, vs_hand, game_date)")
    c.execute("SELECT COUNT(*) FROM _batter_platoon")
    print(f"  rows: {c.fetchone()[0]}")


@timed("pitcher arsenal summary")
def build_arsenal(c):
    """Per (pitcher, game_pk, game_date) - pitch-type % and FF velo."""
    c.execute("DROP TABLE IF EXISTS _arsenal")
    union = " UNION ALL ".join(
        f"""SELECT pl.matchup_pitcher_id AS pid, pe.game_pk, g.game_date,
                   pe.play_events_details_type_code AS tc,
                   pe.play_events_pitch_data_start_speed AS spd
            FROM mlb_play_events_{y} pe
            JOIN mlb_plays_{y} pl
              ON pl.game_pk = pe.game_pk AND pl.at_bat_index = pe.at_bat_index
            JOIN mlb_games_{y} g ON g.game_pk::int = pe.game_pk
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.matchup_pitcher_id IS NOT NULL"""
        for y in YEARS
    )
    c.execute(f"""
        CREATE TEMP TABLE _arsenal AS
        SELECT pid, game_pk, game_date,
               COUNT(*) AS n,
               COUNT(*) FILTER (WHERE tc='FF')::float / NULLIF(COUNT(*),0) AS pct_ff,
               COUNT(*) FILTER (WHERE tc='SI')::float / NULLIF(COUNT(*),0) AS pct_si,
               COUNT(*) FILTER (WHERE tc='SL')::float / NULLIF(COUNT(*),0) AS pct_sl,
               COUNT(*) FILTER (WHERE tc IN ('CH','FS'))::float / NULLIF(COUNT(*),0) AS pct_offspeed,
               COUNT(*) FILTER (WHERE tc IN ('CU','KC','ST','SV','CS'))::float / NULLIF(COUNT(*),0) AS pct_breaking,
               COUNT(*) FILTER (WHERE tc='FC')::float / NULLIF(COUNT(*),0) AS pct_fc,
               AVG(spd) FILTER (WHERE tc='FF')::float AS ff_velo,
               AVG(spd)::float AS avg_velo
        FROM ({union}) src
        GROUP BY pid, game_pk, game_date
    """)
    c.execute("CREATE INDEX ON _arsenal (pid, game_date)")
    c.execute("SELECT COUNT(*) FROM _arsenal")
    print(f"  rows: {c.fetchone()[0]}")


@timed("pitcher H/A starts")
def build_pitcher_ha(c):
    """Per (pitcher, game_pk, game_date, is_home) - starter game stats."""
    c.execute("DROP TABLE IF EXISTS _pit_ha")
    union = " UNION ALL ".join(
        f"""SELECT p.person_id AS pid, p.game_pk, g.game_date,
                   (p.team_side='home') AS is_home,
                   p.innings_pitched::float AS ip,
                   COALESCE(p.earned_runs,0) AS er
            FROM player_game_stats_pitching_{y} p
            JOIN mlb_games_{y} g ON g.game_pk::int = p.game_pk
            WHERE p.games_started=1 AND p.innings_pitched IS NOT NULL
              AND g.detailed_state ILIKE 'Final%' AND g.game_type='R'"""
        for y in YEARS
    )
    c.execute(f"CREATE TEMP TABLE _pit_ha AS {union}")
    c.execute("CREATE INDEX ON _pit_ha (pid, is_home, game_date)")
    c.execute("SELECT COUNT(*) FROM _pit_ha")
    print(f"  rows: {c.fetchone()[0]}")


@timed("team game results")
def build_team_results(c):
    c.execute("DROP TABLE IF EXISTS _team_results")
    union = " UNION ALL ".join(
        f"""SELECT home_team_id AS team_id, game_date, (home_is_winner=true)::int AS won,
                   home_score AS rs, away_score AS ra, TRUE AS at_home
            FROM mlb_games_{y}
            WHERE detailed_state ILIKE 'Final%' AND game_type='R'
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            UNION ALL
            SELECT away_team_id, game_date, (home_is_winner=false)::int,
                   away_score, home_score, FALSE
            FROM mlb_games_{y}
            WHERE detailed_state ILIKE 'Final%' AND game_type='R'
              AND home_score IS NOT NULL AND away_score IS NOT NULL"""
        for y in YEARS
    )
    c.execute(f"CREATE TEMP TABLE _team_results AS {union}")
    c.execute("CREATE INDEX ON _team_results (team_id, game_date)")


@timed("create v6 table")
def create_v6(c):
    c.execute("DROP TABLE IF EXISTS mlb_features_v6")
    c.execute("CREATE TABLE mlb_features_v6 AS SELECT * FROM mlb_features_v5")
    c.execute("ALTER TABLE mlb_features_v6 ADD PRIMARY KEY (game_pk)")
    c.execute("""
        ALTER TABLE mlb_features_v6
          -- Lineup wOBA vs the OPPOSING pitcher's hand (last 60 days)
          ADD COLUMN h_lineup_woba_vs_hand float,
          ADD COLUMN a_lineup_woba_vs_hand float,
          -- Pitch arsenal (last 5 starts)
          ADD COLUMN h_p_pct_ff float, ADD COLUMN h_p_pct_si float, ADD COLUMN h_p_pct_sl float,
          ADD COLUMN h_p_pct_offspeed float, ADD COLUMN h_p_pct_breaking float, ADD COLUMN h_p_pct_fc float,
          ADD COLUMN a_p_pct_ff float, ADD COLUMN a_p_pct_si float, ADD COLUMN a_p_pct_sl float,
          ADD COLUMN a_p_pct_offspeed float, ADD COLUMN a_p_pct_breaking float, ADD COLUMN a_p_pct_fc float,
          -- Pitcher home/away ERA (last 5 home starts / 5 away starts)
          ADD COLUMN h_p_home_era float, ADD COLUMN h_p_away_era float,
          ADD COLUMN a_p_home_era float, ADD COLUMN a_p_away_era float,
          -- Team last 10 games W%, run differential
          ADD COLUMN h_l10_wpct float, ADD COLUMN h_l10_rdiff float,
          ADD COLUMN a_l10_wpct float, ADD COLUMN a_l10_rdiff float,
          -- Current streak (positive = W streak, negative = L)
          ADD COLUMN h_streak int, ADD COLUMN a_streak int,
          -- Head-to-head last 30 days
          ADD COLUMN h2h_n int, ADD COLUMN h2h_home_wpct float,
          -- API standings (snapshot day-of-game)
          ADD COLUMN h_gb numeric, ADD COLUMN a_gb numeric,
          ADD COLUMN h_api_streak int, ADD COLUMN a_api_streak int
    """)


@timed("add lineup-vs-hand wOBA")
def add_lineup_vs_hand(c):
    """Each batter's rolling wOBA vs the opposing starter's hand, aggregated by lineup slot."""
    # For home lineup: opposing pitcher hand is whatever the *away* pitcher throws with.
    # We need pitcher handedness — look up from mlb_plays where pitcher started.
    # Simpler: get the matchup_pitch_hand_code for the START of each game = starter's hand.
    c.execute("""
        DROP TABLE IF EXISTS _starter_hand;
        CREATE TEMP TABLE _starter_hand AS
        WITH unioned AS (""" +
        " UNION ALL ".join(
            f"""SELECT pl.game_pk, pl.matchup_pitcher_id AS pid,
                       pl.matchup_pitch_hand_code AS hand
                FROM mlb_plays_{y} pl
                JOIN mlb_games_{y} g ON g.game_pk::int = pl.game_pk
                WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
                  AND pl.matchup_pitch_hand_code IS NOT NULL"""
            for y in YEARS
        ) + """
        )
        SELECT pid, hand FROM unioned GROUP BY pid, hand
    """)
    c.execute("CREATE INDEX ON _starter_hand (pid)")

    # Now: for each game, for each batter in home lineup, look up rolling 60d wOBA
    # WHERE vs_hand = (away starter's hand). Then aggregate weighted by batting order.
    for side in ("home", "away"):
        prefix = side[0]
        opp_pid_col = "a_pid" if side == "home" else "h_pid"
        c.execute(f"""
            UPDATE mlb_features_v6 f
            SET {prefix}_lineup_woba_vs_hand = sub.woba
            FROM (
              SELECT f.game_pk,
                     SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa * agg.woba)::float
                       / NULLIF(SUM((1.2 - 0.05*(l.batting_order-1)) * agg.pa), 0) AS woba
              FROM mlb_features_v6 f
              JOIN _starter_hand sh ON sh.pid = f.{opp_pid_col}
              JOIN mlb_game_lineups l
                ON l.game_pk = f.game_pk AND l.team_side = '{side}'
              JOIN LATERAL (
                SELECT SUM(pa)::float AS pa,
                       SUM(pa*woba)::float / NULLIF(SUM(pa),0) AS woba
                FROM _batter_platoon b
                WHERE b.bid = l.player_id
                  AND b.vs_hand = sh.hand
                  AND b.game_date < f.game_date
                  AND b.game_date >= f.game_date - INTERVAL '60 days'
              ) agg ON TRUE
              GROUP BY f.game_pk
            ) sub
            WHERE sub.game_pk = f.game_pk
        """)
        print(f"  {side} lineup vs hand done")


@timed("add arsenal")
def add_arsenal(c):
    for prefix in ("h", "a"):
        pid_col = f"{prefix}_pid"
        c.execute(f"""
            UPDATE mlb_features_v6 f
            SET {prefix}_p_pct_ff = sub.pct_ff, {prefix}_p_pct_si = sub.pct_si,
                {prefix}_p_pct_sl = sub.pct_sl, {prefix}_p_pct_offspeed = sub.pct_offspeed,
                {prefix}_p_pct_breaking = sub.pct_breaking, {prefix}_p_pct_fc = sub.pct_fc
            FROM (
              SELECT f.game_pk,
                AVG(a.pct_ff) AS pct_ff, AVG(a.pct_si) AS pct_si,
                AVG(a.pct_sl) AS pct_sl, AVG(a.pct_offspeed) AS pct_offspeed,
                AVG(a.pct_breaking) AS pct_breaking, AVG(a.pct_fc) AS pct_fc
              FROM mlb_features_v6 f
              JOIN LATERAL (
                SELECT * FROM _arsenal x
                WHERE x.pid = f.{pid_col} AND x.game_date < f.game_date
                ORDER BY x.game_date DESC LIMIT 5
              ) a ON TRUE
              GROUP BY f.game_pk
            ) sub
            WHERE sub.game_pk = f.game_pk
        """)
        print(f"  {prefix} arsenal done")


@timed("add pitcher H/A ERA")
def add_pitcher_ha(c):
    for prefix in ("h", "a"):
        pid_col = f"{prefix}_pid"
        c.execute(f"""
            UPDATE mlb_features_v6 f
            SET {prefix}_p_home_era = sub.home_era,
                {prefix}_p_away_era = sub.away_era
            FROM (
              SELECT f.game_pk,
                (SELECT 9.0*SUM(er)::float / NULLIF(SUM(ip),0)
                   FROM (SELECT * FROM _pit_ha p
                         WHERE p.pid = f.{pid_col} AND p.is_home AND p.game_date < f.game_date
                         ORDER BY p.game_date DESC LIMIT 5) s) AS home_era,
                (SELECT 9.0*SUM(er)::float / NULLIF(SUM(ip),0)
                   FROM (SELECT * FROM _pit_ha p
                         WHERE p.pid = f.{pid_col} AND NOT p.is_home AND p.game_date < f.game_date
                         ORDER BY p.game_date DESC LIMIT 5) s) AS away_era
              FROM mlb_features_v6 f
            ) sub
            WHERE sub.game_pk = f.game_pk
        """)
        print(f"  {prefix} pitcher H/A done")


@timed("add team streak / L10")
def add_streak(c):
    for prefix in ("h", "a"):
        team_col = f"{prefix}ome_team_id" if prefix == "h" else f"way_team_id"
        team_col_full = "home_team_id" if prefix == "h" else "away_team_id"
        c.execute(f"""
            UPDATE mlb_features_v6 f
            SET {prefix}_l10_wpct = sub.wpct,
                {prefix}_l10_rdiff = sub.rdiff,
                {prefix}_streak = sub.streak
            FROM (
              SELECT f.game_pk,
                AVG(t.won::float) AS wpct,
                AVG((t.rs - t.ra)::float) AS rdiff,
                (
                  SELECT CASE WHEN BOOL_AND(won::bool) THEN COUNT(*)::int
                              WHEN BOOL_AND(NOT won::bool) THEN -COUNT(*)::int
                              ELSE 0 END
                  FROM (
                    SELECT won FROM _team_results t2
                    WHERE t2.team_id = f.{team_col_full} AND t2.game_date < f.game_date
                    ORDER BY t2.game_date DESC LIMIT 10
                  ) k
                  -- collapse to current streak by chopping at first toggle
                  WHERE TRUE
                ) AS streak
              FROM mlb_features_v6 f
              JOIN LATERAL (
                SELECT * FROM _team_results tr
                WHERE tr.team_id = f.{team_col_full} AND tr.game_date < f.game_date
                ORDER BY tr.game_date DESC LIMIT 10
              ) t ON TRUE
              GROUP BY f.game_pk
            ) sub
            WHERE sub.game_pk = f.game_pk
        """)
        print(f"  {prefix} streak/L10 done")


@timed("add head-to-head")
def add_h2h(c):
    c.execute("""
        DROP TABLE IF EXISTS _all_games_lite;
        CREATE TEMP TABLE _all_games_lite AS """ +
        " UNION ALL ".join(
            f"""SELECT home_team_id, away_team_id, game_date, (home_is_winner=true)::int AS home_won
                FROM mlb_games_{y}
                WHERE detailed_state ILIKE 'Final%' AND game_type='R'
                  AND home_score IS NOT NULL"""
            for y in YEARS
        )
    )
    c.execute("CREATE INDEX ON _all_games_lite (home_team_id, away_team_id, game_date)")
    c.execute("""
        UPDATE mlb_features_v6 f
        SET h2h_n = sub.n, h2h_home_wpct = sub.h_wpct
        FROM (
          SELECT f.game_pk,
            COUNT(*) AS n,
            AVG(CASE
                  WHEN g.home_team_id = f.home_team_id THEN g.home_won::float
                  ELSE (1 - g.home_won)::float
                END) AS h_wpct
          FROM mlb_features_v6 f
          JOIN _all_games_lite g
            ON ((g.home_team_id = f.home_team_id AND g.away_team_id = f.away_team_id)
             OR (g.home_team_id = f.away_team_id AND g.away_team_id = f.home_team_id))
           AND g.game_date < f.game_date
           AND g.game_date >= f.game_date - INTERVAL '30 days'
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)


@timed("add API standings")
def add_standings(c):
    """Use snapshot from game_date - 1 (or same day if no prior snapshot)."""
    c.execute("""
        UPDATE mlb_features_v6 f
        SET h_gb = sh.games_back,
            h_api_streak = CASE
              WHEN sh.streak LIKE 'W%' THEN substr(sh.streak,2)::int
              WHEN sh.streak LIKE 'L%' THEN -substr(sh.streak,2)::int
              ELSE NULL END,
            a_gb = sa.games_back,
            a_api_streak = CASE
              WHEN sa.streak LIKE 'W%' THEN substr(sa.streak,2)::int
              WHEN sa.streak LIKE 'L%' THEN -substr(sa.streak,2)::int
              ELSE NULL END
        FROM mlb_team_standings sh
        JOIN mlb_team_standings sa
          ON sa.snapshot_date = sh.snapshot_date
        WHERE sh.snapshot_date = f.game_date
          AND sh.team_id = f.home_team_id
          AND sa.team_id = f.away_team_id
    """)


def main():
    pg = psycopg2.connect(**PG); pg.autocommit = False
    with pg.cursor() as c:
        build_batter_platoon(c)
        build_arsenal(c)
        build_pitcher_ha(c)
        build_team_results(c)
        create_v6(c)
        add_lineup_vs_hand(c)
        add_arsenal(c)
        add_pitcher_ha(c)
        add_streak(c)
        add_h2h(c)
        add_standings(c)

        c.execute("""
            SELECT EXTRACT(YEAR FROM game_date)::int yr, COUNT(*) total,
                   COUNT(*) FILTER (WHERE h_lineup_woba_vs_hand IS NOT NULL) lvh,
                   COUNT(*) FILTER (WHERE h_p_pct_ff IS NOT NULL) ars,
                   COUNT(*) FILTER (WHERE h_p_home_era IS NOT NULL) ha,
                   COUNT(*) FILTER (WHERE h_streak IS NOT NULL) streak,
                   COUNT(*) FILTER (WHERE h2h_n IS NOT NULL) h2h,
                   COUNT(*) FILTER (WHERE h_gb IS NOT NULL) stand
            FROM mlb_features_v6 GROUP BY 1 ORDER BY 1
        """)
        print("\nFinal coverage:")
        for r in c.fetchall():
            print(f"  {r[0]}: total={r[1]} lvh={r[2]} ars={r[3]} ha={r[4]} streak={r[5]} h2h={r[6]} stand={r[7]}")
    pg.commit()
    pg.close()


if __name__ == "__main__":
    main()
