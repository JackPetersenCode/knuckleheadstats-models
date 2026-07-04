"""Score MLB games scheduled for a given date using the saved v5 model.

Computes the full v5 feature row by querying Postgres rolling aggregates as of
game_date - 1, joined with today's MLB API data (lineups, weather, umpire,
probable pitchers).

Usage:
  python daily_picker.py [YYYY-MM-DD]  [--odds odds.csv]  [--thr 0.08]

odds.csv schema:  game_pk, ml_home, ml_away
"""
import os
import argparse
import pickle
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd
import psycopg2
import requests
from psycopg2.extras import RealDictCursor, execute_values

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
# v5_seedavg_model.pkl is the production model. It averages predictions across
# 3 random seeds (1, 42, 99) of the LR+XGB+LGB ensemble to reduce seed-dependent
# variance. The original v5_model.pkl's isotonic calibrator collapsed
# probabilities to 5 buckets and was OOF-worse than market; don't use it.
MODEL_PATH = Path(r"c:\Users\jackp\Desktop\new_game\v5_seedavg_model.pkl")
SCHED_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={}&hydrate=probablePitcher,venue"
FEED_URL  = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
STAKE = 100.0
YEARS = [2021, 2022, 2023, 2024, 2025]
# Recommended strategy from OOF backtest:
#   - Bet HOME side only
#   - Only when ml_home_close > 0 (home is the underdog)
#   - Only when (raw_p_home - market_p_home_fair) > 0.06
# OOF performance: +5.15% ROI on 537 bets, P(true ROI > 0) = 0.83, 3 of 4
# years positive. Not 5%-significant, but the strongest signal across 6 rounds.
DEFAULT_THR = 0.06
HOME_DOG_ONLY = True


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def ensure_table(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_picks (
                game_pk      integer NOT NULL,
                game_date    date    NOT NULL,
                home_team    varchar(50),
                away_team    varchar(50),
                home_starter varchar(80),
                away_starter varchar(80),
                model_p_home numeric,
                ml_home      integer,
                ml_away      integer,
                p_home_fair  numeric,
                edge_home    numeric,
                edge_away    numeric,
                pick         varchar(8),
                stake        numeric,
                scored_at    timestamptz default now(),
                settled_y    integer,
                settled_pl   numeric,
                model_version varchar(40),
                PRIMARY KEY (game_pk)
            )
        """)
    pg.commit()


# --- MLB API -----------------------------------------------------------

def fetch_schedule(date_str):
    r = requests.get(SCHED_URL.format(date_str), timeout=30); r.raise_for_status()
    games = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            if g.get("gameType") != "R":
                continue
            home_pp = g.get("teams", {}).get("home", {}).get("probablePitcher", {})
            away_pp = g.get("teams", {}).get("away", {}).get("probablePitcher", {})
            games.append(dict(
                game_pk=g["gamePk"],
                game_date=d["date"],
                home_team=g["teams"]["home"]["team"]["name"],
                home_team_id=g["teams"]["home"]["team"]["id"],
                away_team=g["teams"]["away"]["team"]["name"],
                away_team_id=g["teams"]["away"]["team"]["id"],
                venue_id=g.get("venue", {}).get("id"),
                day_night=g.get("dayNight"),
                series_game_number=g.get("seriesGameNumber"),
                home_probable=home_pp.get("id"),
                home_probable_name=home_pp.get("fullName"),
                away_probable=away_pp.get("id"),
                away_probable_name=away_pp.get("fullName"),
            ))
    return games


def fetch_feed(game_pk):
    r = requests.get(FEED_URL.format(game_pk), timeout=30); r.raise_for_status()
    d = r.json(); gd = d.get("gameData", {}); ld = d.get("liveData", {})
    w = gd.get("weather", {}) or {}
    try: temp_f = int(w.get("temp"))
    except Exception: temp_f = None
    wind = w.get("wind", ""); wind_mph = None; wind_dir = None
    if wind:
        try:
            wind_mph = int(wind.split()[0])
            wind_dir = wind.split(",", 1)[1].strip() if "," in wind else None
        except Exception: pass
    plate_id = None
    for o in ld.get("boxscore", {}).get("officials", []) or []:
        if o.get("officialType") == "Home Plate":
            plate_id = o.get("official", {}).get("id"); break
    lineups = {}
    for side in ("home", "away"):
        t = ld.get("boxscore", {}).get("teams", {}).get(side, {}) or {}
        lineups[side] = list(t.get("battingOrder") or [])
    # also pull probable pitcher hands
    home_pid = gd.get("probablePitchers", {}).get("home", {}).get("id")
    away_pid = gd.get("probablePitchers", {}).get("away", {}).get("id")
    return dict(
        condition=w.get("condition"), temp_f=temp_f, wind_mph=wind_mph, wind_dir=wind_dir,
        plate_umpire_id=plate_id,
        home_lineup=lineups["home"], away_lineup=lineups["away"],
        feed_home_pid=home_pid, feed_away_pid=away_pid,
    )


# --- SQL: comprehensive single-game feature query ----------------------

GAMES_UNION = " UNION ALL ".join(
    f"""SELECT game_pk::int, game_date, home_team_id, away_team_id,
               home_score, away_score, home_is_winner, venue_id, day_night
        FROM mlb_games_{y}
        WHERE detailed_state ILIKE 'Final%%' AND game_type='R'
          AND home_score IS NOT NULL"""
    for y in YEARS
)
PITCH_UNION = " UNION ALL ".join(
    f"""SELECT p.person_id pid, g.game_date,
               COALESCE(p.games_started,0) gs,
               p.innings_pitched::numeric ip,
               COALESCE(p.earned_runs,0) er,
               COALESCE(p.strike_outs,0) k,
               COALESCE(p.base_on_balls,0) bb,
               COALESCE(p.home_runs,0) hr,
               p.team_id, (p.team_side='home') is_home
        FROM player_game_stats_pitching_{y} p
        JOIN mlb_games_{y} g ON g.game_pk::int = p.game_pk
        WHERE p.innings_pitched IS NOT NULL AND p.innings_pitched > 0
          AND g.detailed_state ILIKE 'Final%%' AND g.game_type='R'"""
    for y in YEARS
)


def _team_form(c, team_id, gd):
    sql = f"""
    WITH tg AS (
      SELECT home_team_id team_id, game_date,
             (home_is_winner=true)::int won,
             home_score rs, away_score ra
      FROM ({GAMES_UNION}) gg
      UNION ALL
      SELECT away_team_id, game_date,
             (home_is_winner=false)::int, away_score, home_score
      FROM ({GAMES_UNION}) gg
    )
    SELECT
      (SELECT AVG(won::float) FROM tg WHERE team_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '7 days') wpct_7,
      (SELECT AVG((rs-ra)::float) FROM tg WHERE team_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '14 days') rdiff_14,
      (SELECT AVG((rs-ra)::float) FROM tg WHERE team_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '30 days') rdiff_30,
      (SELECT AVG(rs::float) FROM tg WHERE team_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '30 days') rs_30,
      (SELECT AVG(ra::float) FROM tg WHERE team_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '30 days') ra_30,
      (SELECT CASE WHEN SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83))>0
                   THEN SUM(POWER(rs,1.83))/(SUM(POWER(rs,1.83))+SUM(POWER(ra,1.83)))
                   ELSE 0.5 END
        FROM tg WHERE team_id=%s AND game_date<%s AND EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) pyth
    """
    args = (team_id, gd, gd) * 6
    c.execute(sql, args)
    return c.fetchone()


def _pitcher(c, pid, gd):
    """Rolling 10-start + season-to-date stats for a starter."""
    sql = f"""
    WITH starts AS (
      SELECT * FROM ({PITCH_UNION}) pa WHERE gs=1 AND pid=%s AND game_date<%s
    )
    SELECT
      (SELECT SUM(ip)::float FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) ip_10,
      (SELECT SUM(er)::int FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) er_10,
      (SELECT SUM(k)::int FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) k_10,
      (SELECT SUM(bb)::int FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) bb_10,
      (SELECT SUM(hr)::int FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) hr_10,
      (SELECT COUNT(*)::int FROM (SELECT * FROM starts ORDER BY game_date DESC LIMIT 10) s) gs_10,
      (SELECT (%s::date - MAX(game_date))::int FROM starts) rest,
      (SELECT SUM(ip)::float FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) ip_sd,
      (SELECT SUM(er)::int FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) er_sd,
      (SELECT SUM(k)::int FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) k_sd,
      (SELECT SUM(bb)::int FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) bb_sd,
      (SELECT SUM(hr)::int FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) hr_sd,
      (SELECT COUNT(*)::int FROM starts WHERE EXTRACT(YEAR FROM game_date)=EXTRACT(YEAR FROM %s::date)) starts_sd
    """
    c.execute(sql, (pid, gd, gd, gd, gd, gd, gd, gd, gd))
    return c.fetchone()


def _bullpen(c, team_id, gd):
    sql = f"""
    WITH rp AS (
      SELECT * FROM ({PITCH_UNION}) pa WHERE gs=0 AND team_id=%s AND game_date<%s
    )
    SELECT
      (SELECT SUM(ip)::float FROM rp WHERE game_date>=%s::date - INTERVAL '14 days') ip_14,
      (SELECT SUM(er)::int FROM rp WHERE game_date>=%s::date - INTERVAL '14 days') er_14,
      (SELECT SUM(k)::int FROM rp WHERE game_date>=%s::date - INTERVAL '14 days') k_14,
      (SELECT SUM(bb)::int FROM rp WHERE game_date>=%s::date - INTERVAL '14 days') bb_14,
      (SELECT SUM(ip)::float FROM rp WHERE game_date>=%s::date - INTERVAL '3 days') ip_3
    """
    c.execute(sql, (team_id, gd, gd, gd, gd, gd, gd))
    return c.fetchone()


def _pitch_statcast(c, pid, gd):
    """Last-5-starts statcast averages."""
    pe_union = " UNION ALL ".join(
        f"SELECT game_pk, at_bat_index, play_events_details_is_strike, "
        f"play_events_details_call_code, play_events_pitch_data_start_speed, "
        f"play_events_pitch_data_breaks_spin_rate "
        f"FROM mlb_play_events_{y}" for y in YEARS)
    pl_union = " UNION ALL ".join(
        f"SELECT game_pk, at_bat_index, matchup_pitcher_id FROM mlb_plays_{y}"
        for y in YEARS)
    sql = (
        "WITH ps AS ("
        "  SELECT pl.matchup_pitcher_id pid, pe.game_pk, g.game_date,"
        "         pe.play_events_details_is_strike is_strike,"
        "         pe.play_events_details_call_code cc,"
        "         pe.play_events_pitch_data_start_speed spd,"
        "         pe.play_events_pitch_data_breaks_spin_rate spin"
        "  FROM (" + pe_union + ") pe"
        "  JOIN (" + pl_union + ") pl"
        "    ON pl.game_pk = pe.game_pk AND pl.at_bat_index = pe.at_bat_index"
        "  JOIN (" + GAMES_UNION + ") g ON g.game_pk = pe.game_pk"
        "  WHERE pl.matchup_pitcher_id = %s AND g.game_date < %s"
        "),"
        " per_game AS ("
        "  SELECT game_pk, game_date,"
        "         AVG(spd)::float avg_spd, AVG(spin)::float avg_spin,"
        "         AVG(is_strike::int)::float strike_rate,"
        "         (COUNT(*) FILTER (WHERE cc IN ('S','W')))::float/NULLIF(COUNT(*),0) whiff_rate"
        "  FROM ps GROUP BY game_pk, game_date"
        ")"
        " SELECT AVG(avg_spd)::float, AVG(avg_spin)::float,"
        "        AVG(strike_rate)::float, AVG(whiff_rate)::float"
        " FROM (SELECT * FROM per_game ORDER BY game_date DESC LIMIT 5) s"
    )
    c.execute(sql, (pid, gd))
    return c.fetchone()


def _team_bat(c, team_id, gd):
    plays_union = " UNION ALL ".join(
        f"SELECT game_pk, about_is_top_inning, result_event FROM mlb_plays_{y}"
        for y in YEARS)
    sql = (
        "WITH bs AS ("
        "  SELECT CASE WHEN pl.about_is_top_inning THEN g.away_team_id"
        "              ELSE g.home_team_id END team_id,"
        "         pl.game_pk, g.game_date, pl.result_event ev"
        "  FROM (" + plays_union + ") pl"
        "  JOIN (" + GAMES_UNION + ") g ON g.game_pk = pl.game_pk"
        "  WHERE pl.result_event IS NOT NULL"
        ")"
        " SELECT"
        "  COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float / NULLIF(COUNT(*),0),"
        "  COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float / NULLIF(COUNT(*),0),"
        "  COUNT(*) FILTER (WHERE ev='Home Run')::float / NULLIF(COUNT(*),0),"
        "  (COUNT(*) FILTER (WHERE ev='Double')"
        "   + 2*COUNT(*) FILTER (WHERE ev='Triple')"
        "   + 3*COUNT(*) FILTER (WHERE ev='Home Run'))::float / NULLIF(COUNT(*),0),"
        "  (0.7*COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk','Hit By Pitch'))"
        "   + 0.9*COUNT(*) FILTER (WHERE ev='Single')"
        "   + 1.25*COUNT(*) FILTER (WHERE ev='Double')"
        "   + 1.6*COUNT(*) FILTER (WHERE ev='Triple')"
        "   + 2.0*COUNT(*) FILTER (WHERE ev='Home Run'))::float / NULLIF(COUNT(*),0)"
        " FROM bs"
        " WHERE team_id=%s AND game_date < %s AND game_date >= %s::date - INTERVAL '14 days'"
    )
    c.execute(sql, (team_id, gd, gd))
    return c.fetchone()


def _lineup_woba(c, lineup_ids, gd):
    if not lineup_ids: return None
    pid_list = ",".join(str(int(x)) for x in lineup_ids)
    plays_union = " UNION ALL ".join(
        f"SELECT game_pk, matchup_batter_id, result_event FROM mlb_plays_{y}"
        for y in YEARS)
    sql = (
        "WITH bp AS ("
        "  SELECT pl.matchup_batter_id bid, g.game_date, pl.result_event ev"
        "  FROM (" + plays_union + ") pl"
        "  JOIN (" + GAMES_UNION + ") g ON g.game_pk = pl.game_pk"
        "  WHERE pl.matchup_batter_id IN (" + pid_list + ") AND pl.result_event IS NOT NULL"
        "    AND g.game_date < %s AND g.game_date >= %s::date - INTERVAL '21 days'"
        "),"
        " per_b AS ("
        "  SELECT bid,"
        "    COUNT(*) pa,"
        "    COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float/COUNT(*) k_rate,"
        "    COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float/COUNT(*) bb_rate,"
        "    (COUNT(*) FILTER (WHERE ev='Double')"
        "     + 2*COUNT(*) FILTER (WHERE ev='Triple')"
        "     + 3*COUNT(*) FILTER (WHERE ev='Home Run'))::float/COUNT(*) iso,"
        "    (0.7*COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk','Hit By Pitch'))"
        "     + 0.9*COUNT(*) FILTER (WHERE ev='Single')"
        "     + 1.25*COUNT(*) FILTER (WHERE ev='Double')"
        "     + 1.6*COUNT(*) FILTER (WHERE ev='Triple')"
        "     + 2.0*COUNT(*) FILTER (WHERE ev='Home Run'))::float/COUNT(*) woba"
        "  FROM bp GROUP BY bid"
        ")"
        " SELECT bid, pa, k_rate, bb_rate, iso, woba FROM per_b"
    )
    c.execute(sql, (gd, gd))
    rows = {r[0]: dict(pa=r[1], k_rate=r[2], bb_rate=r[3], iso=r[4], woba=r[5])
            for r in c.fetchall()}
    # order-weight aggregate
    num = dict(woba=0.0, iso=0.0, k=0.0, bb=0.0)
    den = 0.0; nb = 0
    for slot, bid in enumerate(lineup_ids, start=1):
        rec = rows.get(int(bid))
        if not rec or not rec["pa"]:
            continue
        w = (1.2 - 0.05*(slot-1)) * rec["pa"]
        if rec["woba"] is not None: num["woba"] += w * rec["woba"]
        if rec["iso"] is not None: num["iso"] += w * rec["iso"]
        if rec["k_rate"] is not None: num["k"] += w * rec["k_rate"]
        if rec["bb_rate"] is not None: num["bb"] += w * rec["bb_rate"]
        den += w; nb += 1
    if den == 0: return None
    return dict(
        woba=num["woba"]/den, iso=num["iso"]/den, k=num["k"]/den, bb=num["bb"]/den, n=nb)


def _park(c, venue_id, gd):
    sql = "SELECT AVG((home_score+away_score)::float) FROM (" + GAMES_UNION + \
          ") g WHERE venue_id=%s AND game_date<%s AND game_date>=%s::date - INTERVAL '90 days'"
    c.execute(sql, (venue_id, gd, gd))
    return c.fetchone()[0]


def _ump_60d(c, ump_id, gd):
    plays_union = " UNION ALL ".join(
        f"SELECT game_pk, result_event FROM mlb_plays_{y}" for y in YEARS)
    sql = (
        "WITH us AS ("
        "  SELECT u.plate_umpire_id ump_id, g.game_date, pl.result_event ev,"
        "         g.home_score+g.away_score total_runs"
        "  FROM (" + plays_union + ") pl"
        "  JOIN (" + GAMES_UNION + ") g ON g.game_pk = pl.game_pk"
        "  JOIN mlb_game_umpires u ON u.game_pk = pl.game_pk"
        "  WHERE pl.result_event IS NOT NULL AND u.plate_umpire_id = %s"
        "    AND g.game_date < %s AND g.game_date >= %s::date - INTERVAL '60 days'"
        ")"
        " SELECT"
        "  COUNT(*) FILTER (WHERE ev IN ('Strikeout','Strikeout Double Play'))::float/NULLIF(COUNT(*),0),"
        "  COUNT(*) FILTER (WHERE ev IN ('Walk','Intent Walk'))::float/NULLIF(COUNT(*),0),"
        "  AVG(total_runs)::float,"
        "  COUNT(DISTINCT us.game_date)::int"
        " FROM us"
    )
    c.execute(sql, (ump_id, gd, gd))
    return c.fetchone()


def build_features_row(pg, g, feed):
    row = {}
    with pg.cursor() as c:
        # Team form + Pythagorean
        for side, prefix, team_id in [("home", "h", g["home_team_id"]),
                                       ("away", "a", g["away_team_id"])]:
            r = _team_form(c, team_id, g["game_date"])
            row[f"{prefix}_wpct_7"], row[f"{prefix}_rdiff_14"], row[f"{prefix}_rdiff_30"], \
            row[f"{prefix}_rs_30"], row[f"{prefix}_ra_30"], row[f"{prefix}_pyth"] = r

        # Home/away win pct (placeholder zero)
        row["h_wpct_home"] = row["h_wpct_7"]  # placeholder
        row["a_wpct_away"] = row["a_wpct_7"]

        # Pitcher
        for side, prefix, pid in [
            ("home", "h_p", feed.get("feed_home_pid") or g.get("home_probable")),
            ("away", "a_p", feed.get("feed_away_pid") or g.get("away_probable")),
        ]:
            if pid:
                r = _pitcher(c, pid, g["game_date"])
                (row[f"{prefix}_ip_10"], row[f"{prefix}_er_10"], row[f"{prefix}_k_10"],
                 row[f"{prefix}_bb_10"], row[f"{prefix}_hr_10"], row[f"{prefix}_starts_10"],
                 row[f"{prefix}_rest"],
                 ip_sd, er_sd, k_sd, bb_sd, hr_sd, starts_sd) = r
                # compute rate stats
                def per9(num, ip):
                    return num*9.0/ip if (ip and ip > 0) else np.nan
                row[f"{prefix}_era_10"] = per9(row[f"{prefix}_er_10"], row[f"{prefix}_ip_10"])
                row[f"{prefix}_k9_10"]  = per9(row[f"{prefix}_k_10"], row[f"{prefix}_ip_10"])
                row[f"{prefix}_bb9_10"] = per9(row[f"{prefix}_bb_10"], row[f"{prefix}_ip_10"])
                row[f"{prefix}_hr9_10"] = per9(row[f"{prefix}_hr_10"], row[f"{prefix}_ip_10"])
                row[f"{prefix}_ipgs_10"] = (row[f"{prefix}_ip_10"] / row[f"{prefix}_starts_10"]
                                            if row[f"{prefix}_starts_10"] else np.nan)
                row[f"{prefix}_era_sd"] = per9(er_sd, ip_sd)
                row[f"{prefix}_k9_sd"]  = per9(k_sd, ip_sd)
                row[f"{prefix}_bb9_sd"] = per9(bb_sd, ip_sd)
                row[f"{prefix}_hr9_sd"] = per9(hr_sd, ip_sd)
                row[f"{prefix}_starts_sd"] = starts_sd

                # Statcast (last 5 starts averages)
                sc_r = _pitch_statcast(c, pid, g["game_date"])
                if sc_r is not None:
                    row[f"{prefix}_velo"], row[f"{prefix}_spin"], \
                    row[f"{prefix}_strike_rate"], row[f"{prefix}_whiff_rate"] = sc_r
                row[f"{prefix}_ff_velo"] = row.get(f"{prefix}_velo")  # approx
            else:
                # Set all to NaN
                for k in ("ip_10","er_10","k_10","bb_10","hr_10","starts_10","rest",
                          "era_10","k9_10","bb9_10","hr9_10","ipgs_10",
                          "era_sd","k9_sd","bb9_sd","hr9_sd","starts_sd",
                          "velo","spin","strike_rate","whiff_rate","ff_velo"):
                    row[f"{prefix}_{k}"] = np.nan

        # Bullpen
        for side, prefix, team_id in [("home","h_bp", g["home_team_id"]),
                                       ("away","a_bp", g["away_team_id"])]:
            r = _bullpen(c, team_id, g["game_date"])
            ip_14, er_14, k_14, bb_14, ip_3 = r
            row[f"{prefix}_ip_14"] = ip_14; row[f"{prefix}_er_14"] = er_14
            row[f"{prefix}_k_14"]  = k_14;  row[f"{prefix}_bb_14"] = bb_14
            row[f"{prefix}_ip_3"]  = ip_3
            def per9(num, ip):
                return num*9.0/ip if (ip and ip > 0) else np.nan
            row[f"{prefix}_era_14"] = per9(er_14, ip_14)
            row[f"{prefix}_k9_14"]  = per9(k_14, ip_14)
            row[f"{prefix}_bb9_14"] = per9(bb_14, ip_14)
            row[f"{prefix}_fatigue"] = (ip_3 / ip_14) if (ip_3 and ip_14) else np.nan

        # Team batting (last 14d)
        for side, prefix, team_id in [("home","h_bat", g["home_team_id"]),
                                       ("away","a_bat", g["away_team_id"])]:
            r = _team_bat(c, team_id, g["game_date"])
            row[f"{prefix}_k"], row[f"{prefix}_bb"], row[f"{prefix}_hr"], \
            row[f"{prefix}_iso"], row[f"{prefix}_woba"] = r

        # Lineup
        for side, prefix in [("home","h_lineup"), ("away","a_lineup")]:
            lu = _lineup_woba(c, feed.get(f"{side}_lineup") or [], g["game_date"])
            if lu:
                row[f"{prefix}_woba"] = lu["woba"]; row[f"{prefix}_iso"] = lu["iso"]
                row[f"{prefix}_k_rate"] = lu["k"]; row[f"{prefix}_bb_rate"] = lu["bb"]
                row[f"{prefix}_n_batters"] = lu["n"]
            else:
                for k in ("woba","iso","k_rate","bb_rate","n_batters"):
                    row[f"{prefix}_{k}"] = np.nan

        # Park
        row["park_rpg"] = _park(c, g["venue_id"], g["game_date"]) if g.get("venue_id") else np.nan

        # Umpire
        if feed.get("plate_umpire_id"):
            r = _ump_60d(c, feed["plate_umpire_id"], g["game_date"])
            row["ump_k_rate"], row["ump_bb_rate"], row["ump_runs_pg"], row["ump_n_games"] = r
        else:
            row["ump_k_rate"] = row["ump_bb_rate"] = row["ump_runs_pg"] = row["ump_n_games"] = np.nan

    # Weather + flags
    row["temp_f"] = feed.get("temp_f"); row["wind_mph"] = feed.get("wind_mph")
    row["is_dome"] = 1 if feed.get("condition") in ("Dome","Roof Closed") else 0
    wd = feed.get("wind_dir") or ""
    row["wind_helps_hitter"] = 1 if (feed.get("wind_mph") or 0) >= 5 and wd.startswith("Out To") else 0
    row["wind_helps_pitcher"] = 1 if (feed.get("wind_mph") or 0) >= 5 and wd.startswith("In From") else 0
    row["weather_clear"] = 1 if feed.get("condition") in ("Clear","Sunny","Partly Cloudy") else 0
    row["wind_x_helps_hitter"] = row["wind_helps_hitter"] * (row["wind_mph"] or 0)
    row["wind_x_helps_pitcher"] = row["wind_helps_pitcher"] * (row["wind_mph"] or 0)
    row["cold_temp"] = 1 if (row["temp_f"] or 70) < 50 else 0
    row["hot_temp"]  = 1 if (row["temp_f"] or 70) > 85 else 0
    row["is_night"] = 1 if (g.get("day_night") == "night") else 0
    row["series_game_number"] = g.get("series_game_number") or 1
    row["h_dayafternight"] = 0; row["a_dayafternight"] = 0

    # Coerce all numeric values to float so downstream math works (some come back
    # as Decimal from Postgres NUMERIC).
    for k, v in list(row.items()):
        if v is None:
            continue
        try:
            row[k] = float(v)
        except (TypeError, ValueError):
            pass

    # Differentials
    def safe(a, b):
        if a is None or b is None: return np.nan
        try:
            if pd.isna(a) or pd.isna(b): return np.nan
        except Exception:
            pass
        return float(a) - float(b)
    row["d_pyth"] = safe(row.get("h_pyth"), row.get("a_pyth"))
    row["d_wpct_7"] = safe(row.get("h_wpct_7"), row.get("a_wpct_7"))
    row["d_rdiff_30"] = safe(row.get("h_rdiff_30"), row.get("a_rdiff_30"))
    row["d_starter_era_sd"] = safe(row.get("a_p_era_sd"), row.get("h_p_era_sd"))
    row["d_starter_k9_sd"] = safe(row.get("h_p_k9_sd"), row.get("a_p_k9_sd"))
    row["d_starter_bb9_sd"] = safe(row.get("a_p_bb9_sd"), row.get("h_p_bb9_sd"))
    row["d_bp_era_14"] = safe(row.get("a_bp_era_14"), row.get("h_bp_era_14"))
    row["d_bp_fatigue"] = safe(row.get("a_bp_fatigue"), row.get("h_bp_fatigue"))
    row["d_wpct_home_away"] = safe(row.get("h_wpct_home"), row.get("a_wpct_away"))
    row["d_p_ff_velo"] = safe(row.get("h_p_ff_velo"), row.get("a_p_ff_velo"))
    row["d_p_velo"] = safe(row.get("h_p_velo"), row.get("a_p_velo"))
    row["d_p_spin"] = safe(row.get("h_p_spin"), row.get("a_p_spin"))
    row["d_p_whiff"] = safe(row.get("h_p_whiff_rate"), row.get("a_p_whiff_rate"))
    row["d_p_strike"] = safe(row.get("h_p_strike_rate"), row.get("a_p_strike_rate"))
    row["d_bat_woba"] = safe(row.get("h_bat_woba"), row.get("a_bat_woba"))
    row["d_bat_iso"] = safe(row.get("h_bat_iso"), row.get("a_bat_iso"))
    row["d_bat_k"] = safe(row.get("a_bat_k"), row.get("h_bat_k"))
    row["d_bat_bb"] = safe(row.get("h_bat_bb"), row.get("a_bat_bb"))
    row["d_bat_hr"] = safe(row.get("h_bat_hr"), row.get("a_bat_hr"))
    row["d_lineup_woba"] = safe(row.get("h_lineup_woba"), row.get("a_lineup_woba"))
    row["d_lineup_iso"] = safe(row.get("h_lineup_iso"), row.get("a_lineup_iso"))
    row["d_lineup_k"] = safe(row.get("a_lineup_k_rate"), row.get("h_lineup_k_rate"))
    row["d_lineup_bb"] = safe(row.get("h_lineup_bb_rate"), row.get("a_lineup_bb_rate"))
    row["mq_h_pitch_vs_a_bat"] = safe(row.get("h_p_whiff_rate"), row.get("a_bat_woba"))
    row["mq_a_pitch_vs_h_bat"] = safe(row.get("a_p_whiff_rate"), row.get("h_bat_woba"))
    row["mq_h_p_vs_a_lineup"] = safe(row.get("h_p_whiff_rate"), row.get("a_lineup_woba"))
    row["mq_a_p_vs_h_lineup"] = safe(row.get("a_p_whiff_rate"), row.get("h_lineup_woba"))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    ap.add_argument("--odds", help="optional CSV: game_pk,ml_home,ml_away")
    ap.add_argument("--thr", type=float, default=DEFAULT_THR)
    args = ap.parse_args()

    print(f"Loading model from {MODEL_PATH}...")
    with open(MODEL_PATH, "rb") as f:
        m = pickle.load(f)
    feats = m["features"]
    imp, sc = m["imputer"], m["scaler"]
    seed_models = m["seed_models"]  # list of (seed, lr, xgb, lgb)
    print(f"  trained through {m['trained_through']}, seeds = {m['seeds']}")
    print(f"  val log-loss: market={m['val_market_log_loss']:.4f}  model={m['val_model_log_loss']:.4f}")
    rs = m.get("recommended_strategy", {})
    if rs:
        print(f"  strategy: {rs.get('description')}")
        print(f"  seed-robust expected ROI: {rs.get('expected_seed_robust_roi_pct')}% "
              f"(std {rs.get('seed_std_roi_pct')}%)")

    pg = psycopg2.connect(**PG); ensure_table(pg)

    print(f"\nFetching schedule for {args.date}...")
    games = fetch_schedule(args.date)
    print(f"  {len(games)} regular-season games")

    odds_df = pd.read_csv(args.odds).set_index("game_pk") if args.odds else None
    rows = []

    for g in games:
        print(f"\n  {g['game_pk']}: {g['away_team']} ({g.get('away_probable_name','?')}) @ "
              f"{g['home_team']} ({g.get('home_probable_name','?')})")
        try:
            feed = fetch_feed(g["game_pk"])
        except Exception as e:
            print(f"    feed error: {e}"); continue
        try:
            feat_row = build_features_row(pg, g, feed)
            pg.commit()
        except Exception as e:
            pg.rollback()
            print(f"    feature error: {e}"); continue

        rec = {f: feat_row.get(f, np.nan) for f in feats}
        if odds_df is not None and g["game_pk"] in odds_df.index:
            mh = int(odds_df.loc[g["game_pk"], "ml_home"])
            ma = int(odds_df.loc[g["game_pk"], "ml_away"])
            ph_raw = american_to_prob(mh); pa_raw = american_to_prob(ma)
            ovr = ph_raw + pa_raw; ph_fair = ph_raw / ovr
            rec["mkt_logit"]  = float(np.log(ph_fair / (1 - ph_fair)))
            rec["open_logit"] = rec["mkt_logit"]
            rec["line_move"]  = 0.0
        else:
            mh = ma = None; ph_fair = None
            rec["mkt_logit"] = rec["open_logit"] = rec["line_move"] = np.nan

        X = pd.DataFrame([rec])[feats].values.astype(float)
        n_nan = int(np.isnan(X).sum())
        X_i = imp.transform(X); X_s = sc.transform(X_i)
        # Seed-averaged ensemble: average probabilities across 3 seeds of the
        # 3-model ensemble (LR + XGB + LGB).
        all_probs = []
        for seed, lr, xgbm, lgbm in seed_models:
            p_lr = lr.predict_proba(X_s)[:,1]
            p_xgb = xgbm.predict_proba(X_i)[:,1]
            p_lgb = lgbm.predict_proba(X_i)[:,1]
            all_probs.append((p_lr + p_xgb + p_lgb) / 3)
        p_cal = float(np.mean(all_probs))

        edge_h = edge_a = pick = None
        if mh is not None:
            edge_h = p_cal - ph_fair; edge_a = (1 - p_cal) - (1 - ph_fair)
            # Strategy from OOF backtest: home underdog only, with model edge > thr
            if HOME_DOG_ONLY:
                if edge_h > args.thr and mh > 0:
                    pick = "HOME"
            else:
                if edge_h > args.thr: pick = "HOME"
                elif edge_a > args.thr: pick = "AWAY"

        print(f"    p(home)={p_cal:.3f}  nan_features={n_nan}/{len(feats)}", end="")
        if mh is not None:
            print(f"  market_fair={ph_fair:.3f}  edge_h={edge_h:+.3f}  edge_a={edge_a:+.3f}", end="")
            if pick: print(f"  PICK: {pick} @ {(mh if pick=='HOME' else ma):+}")
            else: print(f"  (no edge >{args.thr:.2f})")
        else:
            print()

        rows.append((
            g["game_pk"], g["game_date"], g["home_team"], g["away_team"],
            g.get("home_probable_name"), g.get("away_probable_name"),
            p_cal, mh, ma,
            float(ph_fair) if ph_fair is not None else None,
            float(edge_h) if edge_h is not None else None,
            float(edge_a) if edge_a is not None else None,
            pick,
            STAKE if pick else None,
            "v5_seedavg_homedog",  # model_version
        ))

    if rows:
        with pg.cursor() as c:
            execute_values(c,
                "INSERT INTO daily_picks "
                "(game_pk, game_date, home_team, away_team, home_starter, away_starter, "
                "model_p_home, ml_home, ml_away, p_home_fair, edge_home, edge_away, pick, stake, "
                "model_version) "
                "VALUES %s ON CONFLICT (game_pk) DO UPDATE SET "
                "model_p_home = EXCLUDED.model_p_home, ml_home = EXCLUDED.ml_home, "
                "ml_away = EXCLUDED.ml_away, p_home_fair = EXCLUDED.p_home_fair, "
                "edge_home = EXCLUDED.edge_home, edge_away = EXCLUDED.edge_away, "
                "pick = EXCLUDED.pick, scored_at = now(), "
                "model_version = EXCLUDED.model_version", rows)
        pg.commit()

    n_picks = sum(1 for r in rows if r[12])
    print(f"\nWrote {len(rows)} games; {n_picks} picks above thr={args.thr}")
    pg.close()


if __name__ == "__main__":
    main()
