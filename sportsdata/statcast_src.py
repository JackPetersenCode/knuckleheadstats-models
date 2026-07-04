"""MLB pitch-level Statcast source — Baseball Savant 'details' CSV export.

One row per pitch. Statcast tracking is league-wide from 2015 onward, so that is
the earliest available season. batter/pitcher are MLBAM ids (= our mlb player_id);
game_pk = statsapi gamePk (= our mlb game_id).

Savant caps a single search at ~25k rows, so we pull ONE DAY at a time
(a full MLB slate is ~4-5k pitches). Idempotent: upsert on (game_pk, ab, pitch).
"""
import csv, io, datetime
import db
from http_util import get_text, to_int, to_num

SAVANT = ("https://baseballsavant.mlb.com/statcast_search/csv?all=true&type=details"
          "&player_type=pitcher&game_date_gt={d}&game_date_lt={d}")

# CSV column -> (db column, caster). Curated modeling subset of the 119-col feed.
_INT = to_int
_NUM = to_num
COLMAP = {
    "game_pk": ("game_pk", _INT), "at_bat_number": ("at_bat_number", _INT),
    "pitch_number": ("pitch_number", _INT), "game_date": ("game_date", str),
    "game_year": ("game_year", _INT), "game_type": ("game_type", str),
    "pitcher": ("pitcher", _INT), "batter": ("batter", _INT),
    "player_name": ("pitcher_name", str), "stand": ("stand", str), "p_throws": ("p_throws", str),
    "pitch_type": ("pitch_type", str), "pitch_name": ("pitch_name", str),
    "release_speed": ("release_speed", _NUM), "effective_speed": ("effective_speed", _NUM),
    "release_spin_rate": ("release_spin_rate", _NUM), "spin_axis": ("spin_axis", _NUM),
    "release_extension": ("release_extension", _NUM), "release_pos_x": ("release_pos_x", _NUM),
    "release_pos_y": ("release_pos_y", _NUM), "release_pos_z": ("release_pos_z", _NUM),
    "pfx_x": ("pfx_x", _NUM), "pfx_z": ("pfx_z", _NUM), "arm_angle": ("arm_angle", _NUM),
    "plate_x": ("plate_x", _NUM), "plate_z": ("plate_z", _NUM), "zone": ("zone", _INT),
    "sz_top": ("sz_top", _NUM), "sz_bot": ("sz_bot", _NUM),
    "balls": ("balls", _INT), "strikes": ("strikes", _INT), "outs_when_up": ("outs_when_up", _INT),
    "inning": ("inning", _INT), "inning_topbot": ("inning_topbot", str),
    "on_1b": ("on_1b", _INT), "on_2b": ("on_2b", _INT), "on_3b": ("on_3b", _INT),
    "n_thruorder_pitcher": ("n_thruorder_pitcher", _INT),
    "pitcher_days_since_prev_game": ("pitcher_days_since_prev_game", _INT),
    "type": ("type", str), "description": ("description", str), "events": ("events", str),
    "des": ("des", str), "bb_type": ("bb_type", str), "hit_location": ("hit_location", _INT),
    "hc_x": ("hc_x", _NUM), "hc_y": ("hc_y", _NUM),
    "launch_speed": ("launch_speed", _NUM), "launch_angle": ("launch_angle", _NUM),
    "hit_distance_sc": ("hit_distance_sc", _NUM), "bat_speed": ("bat_speed", _NUM),
    "swing_length": ("swing_length", _NUM), "launch_speed_angle": ("launch_speed_angle", _INT),
    "estimated_ba_using_speedangle": ("estimated_ba_using_speedangle", _NUM),
    "estimated_woba_using_speedangle": ("estimated_woba_using_speedangle", _NUM),
    "estimated_slg_using_speedangle": ("estimated_slg_using_speedangle", _NUM),
    "woba_value": ("woba_value", _NUM), "woba_denom": ("woba_denom", _NUM),
    "babip_value": ("babip_value", _NUM), "iso_value": ("iso_value", _NUM),
    "delta_run_exp": ("delta_run_exp", _NUM), "delta_home_win_exp": ("delta_home_win_exp", _NUM),
    "delta_pitcher_run_exp": ("delta_pitcher_run_exp", _NUM),
    "if_fielding_alignment": ("if_fielding_alignment", str),
    "of_fielding_alignment": ("of_fielding_alignment", str),
    "home_team": ("home_team", str), "away_team": ("away_team", str),
    "home_score": ("home_score", _INT), "away_score": ("away_score", _INT),
    "bat_score": ("bat_score", _INT), "fld_score": ("fld_score", _INT),
}
DBCOLS = [v[0] for v in COLMAP.values()]


def _cast(name, raw):
    col, fn = COLMAP[name]
    raw = (raw or "").strip()
    if raw == "" or raw.lower() == "null":
        return col, None
    if fn is str:
        return col, raw
    return col, fn(raw)


def fetch_day(date):
    """Return list of pitch dicts for one calendar day (empty if no games)."""
    txt = get_text(SAVANT.format(d=date.isoformat()))
    if not txt.strip():
        return []
    rows = []
    rdr = csv.DictReader(io.StringIO(txt))
    for rec in rdr:
        if not rec.get("game_pk") or not rec.get("at_bat_number") or not rec.get("pitch_number"):
            continue
        row = {}
        for name in COLMAP:
            _, val = _cast(name, rec.get(name))
            row[COLMAP[name][0]] = val
        rows.append(row)
    return rows


def collect_day(date, con):
    rows = fetch_day(date)
    if rows:
        # de-dupe defensively on PK within batch
        db.upsert(con, "mlb_statcast_pitch", rows, ["game_pk", "at_bat_number", "pitch_number"])
        con.commit()
    return len(rows)


if __name__ == "__main__":
    import sys
    d = datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.date.today() - datetime.timedelta(days=1)
    con = db.connect()
    n = collect_day(d, con)
    print(f"statcast {d}: {n} pitches")
    con.close()
