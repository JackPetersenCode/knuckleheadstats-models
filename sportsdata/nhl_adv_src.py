"""NHL advanced metrics — MoneyPuck season-summary CSVs (no key).

  skaters.csv -> nhl_skater_advanced  (expected goals, Corsi/Fenwick %, shot attempts)
  goalies.csv -> nhl_goalie_advanced  (xGoals against, high-danger save context)

MoneyPuck `playerId` is the official NHL id (= our nhl player_id), so these join to
nhl_skater_box / nhl_goalie_box directly. Data is per season per situation
('all','5on5','4on5','5on4','other'). Backfill iterates seasons.
"""
import csv, io
import db
from http_util import get_text, to_int, to_num

URL = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/{kind}.csv"

SK_COLS = [
    ("player_id", "playerId", str), ("season", "season", to_int), ("situation", "situation", str),
    ("name", "name", str), ("team", "team", str), ("position", "position", str),
    ("games_played", "games_played", to_int), ("icetime", "icetime", to_num),
    ("gameScore", "gameScore", to_num),
    ("onIce_xGoalsPercentage", "onIce_xGoalsPercentage", to_num),
    ("onIce_corsiPercentage", "onIce_corsiPercentage", to_num),
    ("onIce_fenwickPercentage", "onIce_fenwickPercentage", to_num),
    ("I_F_xGoals", "I_F_xGoals", to_num), ("I_F_xOnGoal", "I_F_xOnGoal", to_num),
    ("I_F_shotAttempts", "I_F_shotAttempts", to_num), ("I_F_goals", "I_F_goals", to_num),
    ("I_F_points", "I_F_points", to_num), ("I_F_primaryAssists", "I_F_primaryAssists", to_num),
    ("I_F_hits", "I_F_hits", to_num), ("I_F_takeaways", "I_F_takeaways", to_num),
    ("I_F_giveaways", "I_F_giveaways", to_num),
]
G_COLS = [
    ("player_id", "playerId", str), ("season", "season", to_int), ("situation", "situation", str),
    ("name", "name", str), ("team", "team", str),
    ("games_played", "games_played", to_int), ("icetime", "icetime", to_num),
    ("xGoals", "xGoals", to_num), ("goals", "goals", to_num), ("ongoal", "ongoal", to_num),
    ("xRebounds", "xRebounds", to_num),
    ("highDangerShots", "highDangerShots", to_num),
    ("highDangerxGoals", "highDangerxGoals", to_num),
    ("highDangerGoals", "highDangerGoals", to_num),
]


def _map(rec, spec):
    out = {}
    for dbcol, csvcol, fn in spec:
        v = rec.get(csvcol)
        out[dbcol] = None if (v is None or v == "") else (v if fn is str else fn(v))
    return out


def _collect(kind, table, spec, pk, season, con):
    try:
        txt = get_text(URL.format(yr=season, kind=kind))
    except Exception as e:
        print(f"  nhl {kind} {season}: {repr(e)[:70]}"); return 0
    recs = list(csv.DictReader(io.StringIO(txt)))
    rows = [_map(r, spec) for r in recs if r.get("playerId")]
    n = db.upsert(con, table, rows, pk)
    con.commit()
    return n


def collect_season(season, con):
    s = _collect("skaters", "nhl_skater_advanced", SK_COLS, ["player_id", "season", "situation"], season, con)
    g = _collect("goalies", "nhl_goalie_advanced", G_COLS, ["player_id", "season", "situation"], season, con)
    print(f"  nhl {season}: {s} skater-adv rows, {g} goalie-adv rows")
    return s, g


if __name__ == "__main__":
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
    con = db.connect()
    collect_season(yr, con)
    con.close()
