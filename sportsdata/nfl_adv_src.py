"""NFL advanced data — nflverse public release CSVs (no key).

  snap_counts_{season}.csv  -> nfl_snap_counts   (snap %s: offense/defense/ST)
  player_stats_{season}.csv -> nfl_player_advanced (air yards, target share, EPA, ...)

nflverse uses its own player ids (gsis / pfr) + names — a separate id space from the
ESPN box scores, so these land in their own tables (crosswalk by name later).
Data is per season (one CSV/season), so backfill iterates seasons, not days.
"""
import csv, io
import db
from http_util import get_text, to_int, to_num

SNAPS = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{yr}.csv"
PSTATS = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{yr}.csv"


def _rows(url):
    txt = get_text(url)
    return list(csv.DictReader(io.StringIO(txt)))


# nfl_snap_counts: (db col, csv col, caster)
SNAP_COLS = [
    ("season", "season", to_int), ("week", "week", to_int), ("game_type", "game_type", str),
    ("nfl_game_id", "game_id", str), ("pfr_player_id", "pfr_player_id", str),
    ("player_name", "player", str), ("position", "position", str),
    ("team", "team", str), ("opponent", "opponent", str),
    ("offense_snaps", "offense_snaps", to_int), ("offense_pct", "offense_pct", to_num),
    ("defense_snaps", "defense_snaps", to_int), ("defense_pct", "defense_pct", to_num),
    ("st_snaps", "st_snaps", to_int), ("st_pct", "st_pct", to_num),
]

# nfl_player_advanced: (db col, csv col, caster)
ADV_COLS = [
    ("player_id", "player_id", str), ("player_name", "player_display_name", str),
    ("season", "season", to_int), ("week", "week", to_int), ("season_type", "season_type", str),
    ("team", "team", str), ("position", "position", str),
    ("completions", "completions", to_int), ("attempts", "attempts", to_int),
    ("passing_yards", "passing_yards", to_int), ("passing_tds", "passing_tds", to_int),
    ("interceptions", "interceptions", to_int), ("sacks", "sacks", to_num),
    ("passing_air_yards", "passing_air_yards", to_int),
    ("passing_yards_after_catch", "passing_yards_after_catch", to_int),
    ("passing_epa", "passing_epa", to_num), ("pacr", "pacr", to_num), ("dakota", "dakota", to_num),
    ("carries", "carries", to_int), ("rushing_yards", "rushing_yards", to_int),
    ("rushing_tds", "rushing_tds", to_int), ("rushing_epa", "rushing_epa", to_num),
    ("receptions", "receptions", to_int), ("targets", "targets", to_int),
    ("receiving_yards", "receiving_yards", to_int), ("receiving_tds", "receiving_tds", to_int),
    ("receiving_air_yards", "receiving_air_yards", to_int),
    ("receiving_yards_after_catch", "receiving_yards_after_catch", to_int),
    ("target_share", "target_share", to_num), ("air_yards_share", "air_yards_share", to_num),
    ("wopr", "wopr", to_num), ("receiving_epa", "receiving_epa", to_num),
    ("fantasy_points", "fantasy_points", to_num), ("fantasy_points_ppr", "fantasy_points_ppr", to_num),
]


def _map(rec, spec):
    out = {}
    for dbcol, csvcol, fn in spec:
        v = rec.get(csvcol)
        if v is None or v == "" or str(v).upper() in ("NA", "NULL"):
            out[dbcol] = None
        else:
            out[dbcol] = v if fn is str else fn(v)
    return out


def collect_snaps(season, con):
    try:
        recs = _rows(SNAPS.format(yr=season))
    except Exception as e:
        print(f"  nfl snaps {season}: {repr(e)[:70]}"); return 0
    rows = [_map(r, SNAP_COLS) for r in recs if r.get("pfr_player_id")]
    n = db.upsert(con, "nfl_snap_counts", rows, ["season", "week", "pfr_player_id", "team"])
    con.commit()
    return n


def collect_advanced(season, con):
    try:
        recs = _rows(PSTATS.format(yr=season))
    except Exception as e:
        print(f"  nfl adv {season}: {repr(e)[:70]}"); return 0
    rows = [_map(r, ADV_COLS) for r in recs if r.get("player_id")]
    n = db.upsert(con, "nfl_player_advanced", rows, ["player_id", "season", "week"])
    con.commit()
    return n


def collect_season(season, con):
    s = collect_snaps(season, con)
    a = collect_advanced(season, con)
    print(f"  nfl {season}: {s} snap rows, {a} advanced rows")
    return s, a


if __name__ == "__main__":
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
    con = db.connect()
    collect_season(yr, con)
    con.close()
