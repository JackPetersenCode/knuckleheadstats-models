"""V3 builder: extend mlb_features_v2 with Statcast-style pitcher + team batting.

Approach:
  1) Build temp _pitch_summary_by_game: per (pitcher_id, game_pk, game_date) ->
     avg pitch velo, avg FF velo, avg spin, strike%, whiff%
  2) Build temp _bat_summary_by_game: per (batting_team_id, game_pk, game_date,
     opp_pitch_hand) -> K%, BB%, HR%, ISO_proxy, wOBA_proxy
  3) For each game in mlb_features_v2, compute rolling-window summaries before
     game_date and add as columns.

Outputs additional columns to mlb_features_v2.
"""
import os
import psycopg2
import time

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
YEARS = [2021, 2022, 2023, 2024, 2025]


def timed(label):
    def deco(fn):
        def wrap(*args, **kwargs):
            t0 = time.time()
            print(f"[{label}] start")
            r = fn(*args, **kwargs)
            print(f"[{label}] done in {time.time()-t0:.1f}s")
            return r
        return wrap
    return deco


@timed("pitch summary")
def build_pitch_summary(cur):
    cur.execute("DROP TABLE IF EXISTS _pitch_sum")
    # Join pitch events to plays to get pitcher_id, then aggregate per game per pitcher.
    union_sql = " UNION ALL ".join(
        f"""SELECT pl.matchup_pitcher_id AS pid, pe.game_pk,
                   g.game_date,
                   pe.play_events_details_is_strike AS is_strike,
                   pe.play_events_details_call_code AS cc,
                   pe.play_events_details_type_code AS tc,
                   pe.play_events_pitch_data_start_speed AS spd,
                   pe.play_events_pitch_data_breaks_spin_rate AS spin
            FROM mlb_play_events_{y} pe
            JOIN mlb_plays_{y} pl
              ON pl.game_pk = pe.game_pk AND pl.at_bat_index = pe.at_bat_index
            JOIN mlb_games_{y} g ON g.game_pk::int = pe.game_pk
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.matchup_pitcher_id IS NOT NULL"""
        for y in YEARS
    )
    cur.execute(f"""
        CREATE TEMP TABLE _pitch_sum AS
        SELECT pid, game_pk, game_date,
               COUNT(*) AS n_pitch,
               AVG(spd)::float AS avg_spd,
               AVG(spd) FILTER (WHERE tc='FF')::float AS avg_ff_spd,
               AVG(spin)::float AS avg_spin,
               AVG(is_strike::int)::float AS strike_rate,
               (COUNT(*) FILTER (WHERE cc IN ('S','W')))::float / NULLIF(COUNT(*),0) AS whiff_rate,
               (COUNT(*) FILTER (WHERE cc='X'))::float / NULLIF(COUNT(*) FILTER (WHERE cc IN ('X','D','E')),0) AS inplay_out_rate
        FROM ({union_sql}) src
        GROUP BY pid, game_pk, game_date
    """)
    cur.execute("CREATE INDEX ON _pitch_sum (pid, game_date)")
    cur.execute("SELECT COUNT(*) FROM _pitch_sum")
    print(f"  pitch summary rows: {cur.fetchone()[0]}")


@timed("bat summary")
def build_bat_summary(cur):
    cur.execute("DROP TABLE IF EXISTS _bat_sum")
    # Identify batting team via about_is_top_inning (true = away batting at top of inning)
    union_sql = " UNION ALL ".join(
        f"""SELECT CASE WHEN pl.about_is_top_inning THEN g.away_team_id ELSE g.home_team_id END AS team_id,
                   pl.game_pk, g.game_date,
                   pl.matchup_pitch_hand_code AS opp_hand,
                   pl.result_event AS ev
            FROM mlb_plays_{y} pl
            JOIN mlb_games_{y} g ON g.game_pk::int = pl.game_pk
            WHERE g.detailed_state ILIKE 'Final%' AND g.game_type='R'
              AND pl.result_event IS NOT NULL"""
        for y in YEARS
    )
    cur.execute(f"""
        CREATE TEMP TABLE _bat_sum AS
        SELECT team_id, game_pk, game_date, opp_hand,
               COUNT(*) AS pa,
               COUNT(*) FILTER (WHERE ev='Strikeout' OR ev='Strikeout Double Play')::float
                  / NULLIF(COUNT(*),0) AS k_rate,
               COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float
                  / NULLIF(COUNT(*),0) AS bb_rate,
               COUNT(*) FILTER (WHERE ev='Home Run')::float
                  / NULLIF(COUNT(*),0) AS hr_rate,
               (COUNT(*) FILTER (WHERE ev='Double')
                + 2*COUNT(*) FILTER (WHERE ev='Triple')
                + 3*COUNT(*) FILTER (WHERE ev='Home Run'))::float
                  / NULLIF(COUNT(*),0) AS iso_proxy,
               (0.7*COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk','Hit By Pitch'))
                + 0.9*COUNT(*) FILTER (WHERE ev='Single')
                + 1.25*COUNT(*) FILTER (WHERE ev='Double')
                + 1.6*COUNT(*) FILTER (WHERE ev='Triple')
                + 2.0*COUNT(*) FILTER (WHERE ev='Home Run'))::float
                  / NULLIF(COUNT(*),0) AS woba_proxy
        FROM ({union_sql}) src
        GROUP BY team_id, game_pk, game_date, opp_hand
    """)
    cur.execute("CREATE INDEX ON _bat_sum (team_id, game_date)")
    cur.execute("SELECT COUNT(*) FROM _bat_sum")
    print(f"  bat summary rows: {cur.fetchone()[0]}")


@timed("add pitcher rolling cols")
def add_pitcher_rolling(cur):
    cur.execute("""
        ALTER TABLE mlb_features_v2
          ADD COLUMN IF NOT EXISTS h_p_ff_velo float,
          ADD COLUMN IF NOT EXISTS h_p_velo float,
          ADD COLUMN IF NOT EXISTS h_p_spin float,
          ADD COLUMN IF NOT EXISTS h_p_strike_rate float,
          ADD COLUMN IF NOT EXISTS h_p_whiff_rate float,
          ADD COLUMN IF NOT EXISTS a_p_ff_velo float,
          ADD COLUMN IF NOT EXISTS a_p_velo float,
          ADD COLUMN IF NOT EXISTS a_p_spin float,
          ADD COLUMN IF NOT EXISTS a_p_strike_rate float,
          ADD COLUMN IF NOT EXISTS a_p_whiff_rate float
    """)
    cur.execute("""
        UPDATE mlb_features_v2 f
        SET h_p_ff_velo = sub.avg_ff_spd,
            h_p_velo    = sub.avg_spd,
            h_p_spin    = sub.avg_spin,
            h_p_strike_rate = sub.strike_rate,
            h_p_whiff_rate  = sub.whiff_rate
        FROM (
          SELECT f.game_pk,
            AVG(ps.avg_ff_spd) AS avg_ff_spd,
            AVG(ps.avg_spd) AS avg_spd,
            AVG(ps.avg_spin) AS avg_spin,
            AVG(ps.strike_rate) AS strike_rate,
            AVG(ps.whiff_rate) AS whiff_rate
          FROM mlb_features_v2 f
          JOIN LATERAL (
            SELECT * FROM _pitch_sum p
            WHERE p.pid = f.h_pid AND p.game_date < f.game_date
            ORDER BY p.game_date DESC LIMIT 5
          ) ps ON TRUE
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)
    print("  home pitcher Statcast done")

    cur.execute("""
        UPDATE mlb_features_v2 f
        SET a_p_ff_velo = sub.avg_ff_spd,
            a_p_velo    = sub.avg_spd,
            a_p_spin    = sub.avg_spin,
            a_p_strike_rate = sub.strike_rate,
            a_p_whiff_rate  = sub.whiff_rate
        FROM (
          SELECT f.game_pk,
            AVG(ps.avg_ff_spd) AS avg_ff_spd,
            AVG(ps.avg_spd) AS avg_spd,
            AVG(ps.avg_spin) AS avg_spin,
            AVG(ps.strike_rate) AS strike_rate,
            AVG(ps.whiff_rate) AS whiff_rate
          FROM mlb_features_v2 f
          JOIN LATERAL (
            SELECT * FROM _pitch_sum p
            WHERE p.pid = f.a_pid AND p.game_date < f.game_date
            ORDER BY p.game_date DESC LIMIT 5
          ) ps ON TRUE
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)
    print("  away pitcher Statcast done")


@timed("add team batting rolling cols")
def add_batting_rolling(cur):
    cur.execute("""
        ALTER TABLE mlb_features_v2
          ADD COLUMN IF NOT EXISTS h_bat_k float,
          ADD COLUMN IF NOT EXISTS h_bat_bb float,
          ADD COLUMN IF NOT EXISTS h_bat_hr float,
          ADD COLUMN IF NOT EXISTS h_bat_iso float,
          ADD COLUMN IF NOT EXISTS h_bat_woba float,
          ADD COLUMN IF NOT EXISTS a_bat_k float,
          ADD COLUMN IF NOT EXISTS a_bat_bb float,
          ADD COLUMN IF NOT EXISTS a_bat_hr float,
          ADD COLUMN IF NOT EXISTS a_bat_iso float,
          ADD COLUMN IF NOT EXISTS a_bat_woba float
    """)
    # Rolling 14 days, weighted by PA
    cur.execute("""
        UPDATE mlb_features_v2 f
        SET h_bat_k = sub.k_rate, h_bat_bb = sub.bb_rate, h_bat_hr = sub.hr_rate,
            h_bat_iso = sub.iso, h_bat_woba = sub.woba
        FROM (
          SELECT f.game_pk,
            SUM(bs.pa*bs.k_rate)::float / NULLIF(SUM(bs.pa),0) AS k_rate,
            SUM(bs.pa*bs.bb_rate)::float / NULLIF(SUM(bs.pa),0) AS bb_rate,
            SUM(bs.pa*bs.hr_rate)::float / NULLIF(SUM(bs.pa),0) AS hr_rate,
            SUM(bs.pa*bs.iso_proxy)::float / NULLIF(SUM(bs.pa),0) AS iso,
            SUM(bs.pa*bs.woba_proxy)::float / NULLIF(SUM(bs.pa),0) AS woba
          FROM mlb_features_v2 f
          JOIN _bat_sum bs ON bs.team_id = f.home_team_id
                          AND bs.game_date < f.game_date
                          AND bs.game_date >= f.game_date - INTERVAL '14 days'
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)
    print("  home batting done")

    cur.execute("""
        UPDATE mlb_features_v2 f
        SET a_bat_k = sub.k_rate, a_bat_bb = sub.bb_rate, a_bat_hr = sub.hr_rate,
            a_bat_iso = sub.iso, a_bat_woba = sub.woba
        FROM (
          SELECT f.game_pk,
            SUM(bs.pa*bs.k_rate)::float / NULLIF(SUM(bs.pa),0) AS k_rate,
            SUM(bs.pa*bs.bb_rate)::float / NULLIF(SUM(bs.pa),0) AS bb_rate,
            SUM(bs.pa*bs.hr_rate)::float / NULLIF(SUM(bs.pa),0) AS hr_rate,
            SUM(bs.pa*bs.iso_proxy)::float / NULLIF(SUM(bs.pa),0) AS iso,
            SUM(bs.pa*bs.woba_proxy)::float / NULLIF(SUM(bs.pa),0) AS woba
          FROM mlb_features_v2 f
          JOIN _bat_sum bs ON bs.team_id = f.away_team_id
                          AND bs.game_date < f.game_date
                          AND bs.game_date >= f.game_date - INTERVAL '14 days'
          GROUP BY f.game_pk
        ) sub
        WHERE sub.game_pk = f.game_pk
    """)
    print("  away batting done")


def main():
    pg = psycopg2.connect(**PG)
    pg.autocommit = False
    with pg.cursor() as c:
        build_pitch_summary(c)
        build_bat_summary(c)
        add_pitcher_rolling(c)
        add_batting_rolling(c)
        c.execute("""
            SELECT EXTRACT(YEAR FROM game_date)::int AS yr,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE h_p_velo IS NOT NULL AND a_p_velo IS NOT NULL
                                      AND h_bat_woba IS NOT NULL AND a_bat_woba IS NOT NULL
                                      AND h_p_ip_10 IS NOT NULL AND a_p_ip_10 IS NOT NULL
                                      AND h_bp_ip_14 IS NOT NULL AND a_bp_ip_14 IS NOT NULL
                                      AND ml_home_close IS NOT NULL) AS full_feats
            FROM mlb_features_v2 GROUP BY 1 ORDER BY 1
        """)
        print("\nFinal coverage:")
        for r in c.fetchall():
            print(f"  {r[0]}: total={r[1]}  full_feats={r[2]}")
    pg.commit()
    pg.close()


if __name__ == "__main__":
    main()
