"""V2 feature builder: adds bullpen, season-to-date pitcher, Pythagorean,
multiple rolling windows, line movement, day-after-night, series game.

Output: table public.mlb_features_v2 (drops + recreates).
"""
import os
import psycopg2

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
YEARS = [2021, 2022, 2023, 2024, 2025]


def main():
    pg = psycopg2.connect(**PG)
    pg.autocommit = False

    print("Building consolidated views (games + all pitching apps)...")
    with pg.cursor() as c:
        c.execute("DROP TABLE IF EXISTS _games2")
        c.execute("DROP TABLE IF EXISTS _pitch_all")
        union_games = " UNION ALL ".join(
            f"""SELECT game_pk::int, game_date, home_team_id, home_team_name,
                       away_team_id, away_team_name, home_score, away_score,
                       home_is_winner, venue_id, venue_name, day_night,
                       series_game_number
                FROM mlb_games_{y}
                WHERE detailed_state ILIKE 'Final%' AND game_type='R'
                  AND home_score IS NOT NULL AND away_score IS NOT NULL"""
            for y in YEARS
        )
        c.execute(f"CREATE TEMP TABLE _games2 AS {union_games}")
        c.execute("CREATE INDEX ON _games2 (game_date)")
        c.execute("CREATE INDEX ON _games2 (home_team_id, game_date)")
        c.execute("CREATE INDEX ON _games2 (away_team_id, game_date)")
        c.execute("CREATE INDEX ON _games2 (venue_id, game_date)")

        union_pit = " UNION ALL ".join(
            f"""SELECT p.game_pk, p.team_side, p.team_id, p.person_id,
                       COALESCE(p.games_started,0) AS gs,
                       p.innings_pitched::numeric AS ip,
                       COALESCE(p.earned_runs,0) AS er,
                       COALESCE(p.strike_outs,0) AS k,
                       COALESCE(p.base_on_balls,0) AS bb,
                       COALESCE(p.home_runs,0) AS hr,
                       COALESCE(p.batters_faced,0) AS bf,
                       g.game_date,
                       g.home_team_id, g.away_team_id,
                       (p.team_side='home') AS pitched_home
                FROM player_game_stats_pitching_{y} p
                JOIN mlb_games_{y} g ON g.game_pk::int = p.game_pk
                WHERE p.innings_pitched IS NOT NULL
                  AND p.innings_pitched > 0
                  AND g.detailed_state ILIKE 'Final%' AND g.game_type='R'"""
            for y in YEARS
        )
        c.execute(f"CREATE TEMP TABLE _pitch_all AS {union_pit}")
        c.execute("CREATE INDEX ON _pitch_all (person_id, game_date)")
        c.execute("CREATE INDEX ON _pitch_all (team_id, game_date, gs)")
        c.execute("CREATE INDEX ON _pitch_all (game_pk, team_side)")

        c.execute("SELECT COUNT(*) FROM _games2"); print(f"  games: {c.fetchone()[0]}")
        c.execute("SELECT COUNT(*) FROM _pitch_all"); print(f"  pitch appearances (IP>0): {c.fetchone()[0]}")

    print("Computing features v2...")
    sql = """
    DROP TABLE IF EXISTS mlb_features_v2;
    CREATE TABLE mlb_features_v2 AS
    WITH
    starters AS (
        SELECT game_pk, team_side, team_id, person_id, game_date
        FROM _pitch_all WHERE gs = 1
    ),
    hs AS (SELECT game_pk, person_id AS h_pid FROM starters WHERE team_side='home'),
    aws AS (SELECT game_pk, person_id AS a_pid FROM starters WHERE team_side='away'),

    base AS (
        SELECT g.*, h.h_pid, a.a_pid,
               EXTRACT(YEAR FROM g.game_date)::int AS season
        FROM _games2 g
        JOIN hs h ON h.game_pk = g.game_pk
        JOIN aws a ON a.game_pk = g.game_pk
    ),

    team_games AS (
        SELECT home_team_id AS team_id, game_date,
               home_score AS rs, away_score AS ra,
               (home_is_winner=true)::int AS won,
               TRUE AS at_home
        FROM _games2
        UNION ALL
        SELECT away_team_id, game_date, away_score, home_score,
               (home_is_winner=false)::int, FALSE
        FROM _games2
    )

    SELECT b.game_pk, b.game_date, b.season, b.venue_id,
           b.home_team_id, b.away_team_id, b.home_team_name, b.away_team_name,
           b.home_score, b.away_score, b.home_is_winner::int AS y,
           (b.day_night='night') AS is_night, b.series_game_number,
           b.h_pid, b.a_pid,

           -- ============ HOME TEAM FEATURES ============
           -- Form last 7d, 14d, 30d (run differential + win pct)
           (SELECT AVG(won::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '7 days') AS h_wpct_7,
           (SELECT AVG((rs-ra)::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '14 days') AS h_rdiff_14,
           (SELECT AVG((rs-ra)::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_rdiff_30,
           (SELECT AVG(rs::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_rs_30,
           (SELECT AVG(ra::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS h_ra_30,

           -- Pythagorean season-to-date
           (SELECT
              CASE WHEN SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83)) > 0
                THEN SUM(POWER(rs,1.83)) / (SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83)))
                ELSE 0.5 END
            FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND EXTRACT(YEAR FROM tg.game_date)=b.season) AS h_pyth,

           -- Home/away split season-to-date wpct (HOME team at HOME)
           (SELECT AVG(won::float) FROM team_games tg
              WHERE tg.team_id=b.home_team_id
                AND tg.game_date < b.game_date
                AND EXTRACT(YEAR FROM tg.game_date)=b.season
                AND tg.at_home) AS h_wpct_home,

           -- ============ AWAY TEAM FEATURES ============
           (SELECT AVG(won::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '7 days') AS a_wpct_7,
           (SELECT AVG((rs-ra)::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '14 days') AS a_rdiff_14,
           (SELECT AVG((rs-ra)::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_rdiff_30,
           (SELECT AVG(rs::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_rs_30,
           (SELECT AVG(ra::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND tg.game_date >= b.game_date - INTERVAL '30 days') AS a_ra_30,

           (SELECT
              CASE WHEN SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83)) > 0
                THEN SUM(POWER(rs,1.83)) / (SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83)))
                ELSE 0.5 END
            FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND EXTRACT(YEAR FROM tg.game_date)=b.season) AS a_pyth,

           (SELECT AVG(won::float) FROM team_games tg
              WHERE tg.team_id=b.away_team_id
                AND tg.game_date < b.game_date
                AND EXTRACT(YEAR FROM tg.game_date)=b.season
                AND NOT tg.at_home) AS a_wpct_away,

           -- ============ PARK ============
           (SELECT AVG((home_score+away_score)::float) FROM _games2 v
              WHERE v.venue_id=b.venue_id
                AND v.game_date < b.game_date
                AND v.game_date >= b.game_date - INTERVAL '90 days') AS park_rpg,

           -- ============ DAY AFTER NIGHT GAME ============
           (SELECT MAX((g2.day_night='night')::int) FROM _games2 g2
              WHERE (g2.home_team_id=b.home_team_id OR g2.away_team_id=b.home_team_id)
                AND g2.game_date = b.game_date - INTERVAL '1 day') AS h_dayafternight,
           (SELECT MAX((g2.day_night='night')::int) FROM _games2 g2
              WHERE (g2.home_team_id=b.away_team_id OR g2.away_team_id=b.away_team_id)
                AND g2.game_date = b.game_date - INTERVAL '1 day') AS a_dayafternight

    FROM base b;
    """
    with pg.cursor() as c:
        c.execute(sql)
    pg.commit()
    print("  Base features built.")

    # Pitcher features (lateral) - separate step for sanity
    print("Adding pitcher features (rolling + season-to-date)...")
    with pg.cursor() as c:
        c.execute("""
            ALTER TABLE mlb_features_v2
              ADD COLUMN h_p_ip_10  float, ADD COLUMN h_p_er_10  int,
              ADD COLUMN h_p_k_10  int,   ADD COLUMN h_p_bb_10  int,
              ADD COLUMN h_p_hr_10 int,   ADD COLUMN h_p_starts_10 int,
              ADD COLUMN h_p_rest int,
              ADD COLUMN h_p_ip_std  float,
              ADD COLUMN h_p_ip_sd float, ADD COLUMN h_p_er_sd int,
              ADD COLUMN h_p_k_sd int,    ADD COLUMN h_p_bb_sd int,
              ADD COLUMN h_p_hr_sd int,   ADD COLUMN h_p_starts_sd int,
              ADD COLUMN a_p_ip_10  float, ADD COLUMN a_p_er_10  int,
              ADD COLUMN a_p_k_10  int,   ADD COLUMN a_p_bb_10  int,
              ADD COLUMN a_p_hr_10 int,   ADD COLUMN a_p_starts_10 int,
              ADD COLUMN a_p_rest int,
              ADD COLUMN a_p_ip_sd float, ADD COLUMN a_p_er_sd int,
              ADD COLUMN a_p_k_sd int,    ADD COLUMN a_p_bb_sd int,
              ADD COLUMN a_p_hr_sd int,   ADD COLUMN a_p_starts_sd int;
        """)
        # Use server-side cursor to update in chunks via UPDATE FROM subquery
        c.execute("""
            UPDATE mlb_features_v2 f
            SET h_p_ip_10 = hs.ip10, h_p_er_10 = hs.er10, h_p_k_10 = hs.k10,
                h_p_bb_10 = hs.bb10, h_p_hr_10 = hs.hr10, h_p_starts_10 = hs.gs10,
                h_p_rest = hs.rest,
                h_p_ip_sd = hs.ipsd, h_p_er_sd = hs.ersd, h_p_k_sd = hs.ksd,
                h_p_bb_sd = hs.bbsd, h_p_hr_sd = hs.hrsd, h_p_starts_sd = hs.gssd
            FROM (
              SELECT f.game_pk,
                (SELECT SUM(ip)::float FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS ip10,
                (SELECT SUM(er)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS er10,
                (SELECT SUM(k)::int    FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS k10,
                (SELECT SUM(bb)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS bb10,
                (SELECT SUM(hr)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS hr10,
                (SELECT COUNT(*)::int  FROM (SELECT * FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS gs10,
                (SELECT (f.game_date - MAX(game_date))::int FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date) AS rest,
                (SELECT SUM(ip)::float FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ipsd,
                (SELECT SUM(er)::int   FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ersd,
                (SELECT SUM(k)::int    FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ksd,
                (SELECT SUM(bb)::int   FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS bbsd,
                (SELECT SUM(hr)::int   FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS hrsd,
                (SELECT COUNT(*)::int  FROM _pitch_all WHERE person_id=f.h_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS gssd
              FROM mlb_features_v2 f
            ) hs
            WHERE hs.game_pk = f.game_pk;
        """)
        print("  home pitcher features done")

        c.execute("""
            UPDATE mlb_features_v2 f
            SET a_p_ip_10 = aps.ip10, a_p_er_10 = aps.er10, a_p_k_10 = aps.k10,
                a_p_bb_10 = aps.bb10, a_p_hr_10 = aps.hr10, a_p_starts_10 = aps.gs10,
                a_p_rest = aps.rest,
                a_p_ip_sd = aps.ipsd, a_p_er_sd = aps.ersd, a_p_k_sd = aps.ksd,
                a_p_bb_sd = aps.bbsd, a_p_hr_sd = aps.hrsd, a_p_starts_sd = aps.gssd
            FROM (
              SELECT f.game_pk,
                (SELECT SUM(ip)::float FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS ip10,
                (SELECT SUM(er)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS er10,
                (SELECT SUM(k)::int    FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS k10,
                (SELECT SUM(bb)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS bb10,
                (SELECT SUM(hr)::int   FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS hr10,
                (SELECT COUNT(*)::int  FROM (SELECT * FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 ORDER BY game_date DESC LIMIT 10) s) AS gs10,
                (SELECT (f.game_date - MAX(game_date))::int FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date) AS rest,
                (SELECT SUM(ip)::float FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ipsd,
                (SELECT SUM(er)::int   FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ersd,
                (SELECT SUM(k)::int    FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS ksd,
                (SELECT SUM(bb)::int   FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS bbsd,
                (SELECT SUM(hr)::int   FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS hrsd,
                (SELECT COUNT(*)::int  FROM _pitch_all WHERE person_id=f.a_pid AND game_date<f.game_date AND gs=1 AND EXTRACT(YEAR FROM game_date)=f.season) AS gssd
              FROM mlb_features_v2 f
            ) aps
            WHERE aps.game_pk = f.game_pk;
        """)
        print("  away pitcher features done")
    pg.commit()

    # Bullpen features: team-level relief pitching last 14 days (and last 3 days for fatigue)
    print("Adding bullpen features...")
    with pg.cursor() as c:
        c.execute("""
            ALTER TABLE mlb_features_v2
              ADD COLUMN h_bp_ip_14  float, ADD COLUMN h_bp_er_14  int,
              ADD COLUMN h_bp_k_14   int,   ADD COLUMN h_bp_bb_14  int,
              ADD COLUMN h_bp_ip_3   float,
              ADD COLUMN a_bp_ip_14  float, ADD COLUMN a_bp_er_14  int,
              ADD COLUMN a_bp_k_14   int,   ADD COLUMN a_bp_bb_14  int,
              ADD COLUMN a_bp_ip_3   float;
        """)
        c.execute("""
            UPDATE mlb_features_v2 f SET
              h_bp_ip_14 = (SELECT SUM(ip)::float FROM _pitch_all p
                            WHERE p.team_id=f.home_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              h_bp_er_14 = (SELECT SUM(er)::int   FROM _pitch_all p
                            WHERE p.team_id=f.home_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              h_bp_k_14  = (SELECT SUM(k)::int    FROM _pitch_all p
                            WHERE p.team_id=f.home_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              h_bp_bb_14 = (SELECT SUM(bb)::int   FROM _pitch_all p
                            WHERE p.team_id=f.home_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              h_bp_ip_3  = (SELECT SUM(ip)::float FROM _pitch_all p
                            WHERE p.team_id=f.home_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '3 days'),
              a_bp_ip_14 = (SELECT SUM(ip)::float FROM _pitch_all p
                            WHERE p.team_id=f.away_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              a_bp_er_14 = (SELECT SUM(er)::int   FROM _pitch_all p
                            WHERE p.team_id=f.away_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              a_bp_k_14  = (SELECT SUM(k)::int    FROM _pitch_all p
                            WHERE p.team_id=f.away_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              a_bp_bb_14 = (SELECT SUM(bb)::int   FROM _pitch_all p
                            WHERE p.team_id=f.away_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '14 days'),
              a_bp_ip_3  = (SELECT SUM(ip)::float FROM _pitch_all p
                            WHERE p.team_id=f.away_team_id
                              AND p.gs=0 AND p.game_date<f.game_date
                              AND p.game_date >= f.game_date - INTERVAL '3 days');
        """)
    pg.commit()
    print("  bullpen features done")

    # Odds (open + close)
    print("Joining odds (open + close)...")
    with pg.cursor() as c:
        c.execute("""
            ALTER TABLE mlb_features_v2
              ADD COLUMN ml_home_close int, ADD COLUMN ml_away_close int,
              ADD COLUMN ml_home_open  int, ADD COLUMN ml_away_open  int;
            UPDATE mlb_features_v2 f
              SET ml_home_close = o.ml_home_close,
                  ml_away_close = o.ml_away_close,
                  ml_home_open  = o.ml_home_open,
                  ml_away_open  = o.ml_away_open
              FROM historical_mlb_odds o
              WHERE o.game_date = f.game_date AND o.home_team = f.home_team_name;
        """)
        c.execute("SELECT COUNT(*), COUNT(ml_home_close) FROM mlb_features_v2")
        print(f"  rows: {c.fetchone()}")
    pg.commit()

    # Stats
    with pg.cursor() as c:
        c.execute("""
            SELECT EXTRACT(YEAR FROM game_date)::int AS yr, COUNT(*) n,
                   COUNT(ml_home_close) odds,
                   COUNT(*) FILTER (WHERE h_p_ip_10 IS NOT NULL AND a_p_ip_10 IS NOT NULL
                                      AND h_rdiff_30 IS NOT NULL AND a_rdiff_30 IS NOT NULL
                                      AND h_bp_ip_14 IS NOT NULL AND a_bp_ip_14 IS NOT NULL) full_feats
            FROM mlb_features_v2 GROUP BY 1 ORDER BY 1
        """)
        for r in c.fetchall():
            print(f"  {r[0]}: n={r[1]} odds={r[2]} full={r[3]}")
    pg.close()


if __name__ == "__main__":
    main()
