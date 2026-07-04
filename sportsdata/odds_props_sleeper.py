"""Third DFS prop book: Sleeper — FREE public endpoint, explicit over/under multipliers.

  https://api.sleeper.app/lines/available?include_preset=true   (all sports, one call)
  https://api.sleeper.app/players/{sport}                       (id -> name, cached)

Sleeper carries true-odds-style payout multipliers (often 2.0-2.6 on a 0.5 line,
i.e. ~+100 to +160), generally more generous than PrizePicks/Underdog — useful for
+EV detection and as a third book for line-shopping. Writes to odds_prop_snapshot
(source='sleeper'); xwalk + grade pick it up automatically (player_name populated).
"""
import json
import db
from http_util import get_json, to_num

LINES_URL = "https://api.sleeper.app/lines/available?include_preset=true"
PP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SPORTS = ("mlb", "nba", "nhl", "wnba")   # ones we have / may add box data for

# Sleeper wager_type -> the canonical stat string stat_map.py understands
# (unmapped types stored as underscores->spaces; they grade as ungradeable, which is fine)
STAT = {
    # mlb batting
    "hits": "hits", "total_bases": "total bases", "hits_runs_rbis": "hits+runs+rbis",
    "rbis": "rbis", "runs": "runs", "bat_walks": "batter walks", "singles": "singles",
    "doubles": "doubles", "triples": "triples", "home_runs": "home runs",
    "stolen_bases": "stolen bases",
    # mlb pitching
    "strike_outs": "pitcher strikeouts", "earned_runs": "earned runs allowed",
    "hits_allowed": "hits allowed", "walks_allowed": "walks allowed", "outs": "pitching outs",
    # nba
    "points": "points", "rebounds": "rebounds", "assists": "assists", "steals": "steals",
    "blocks": "blocked shots", "threes_made": "3-pt made", "pts_reb_ast": "pts+rebs+asts",
    "points_and_rebounds": "pts+rebs", "points_and_assists": "pts+asts",
    "rebounds_and_assists": "rebs+asts",
    # nhl
    "shots": "shots on goal", "goals": "goals", "saves": "saves",
    "goals_against": "goals against",
}


def _players(sport):
    try:
        d = get_json(f"https://api.sleeper.app/players/{sport}", headers=PP_HEADERS)
    except Exception:
        return {}
    out = {}
    for pid, p in d.items():
        nm = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if nm:
            out[str(pid)] = nm
    return out


def collect_sleeper(con):
    try:
        data = get_json(LINES_URL, headers=PP_HEADERS, retries=5, backoff=3.0)
    except Exception as e:
        print(f"  sleeper: ERR {repr(e)[:70]}")
        return 0
    namecache = {}
    rows, bysport = [], {}
    for entry in data:
        sport = entry.get("sport")
        if sport not in SPORTS:
            continue
        if entry.get("subject_type") != "player":
            continue
        opts = entry.get("options", [])
        if not opts:
            continue
        over_mult = under_mult = line = None
        for o in opts:
            if o.get("status") != "active":
                continue
            if line is None:
                line = to_num(o.get("outcome_value"))
            if o.get("outcome") == "over":
                over_mult = to_num(o.get("payout_multiplier"))
            elif o.get("outcome") == "under":
                under_mult = to_num(o.get("payout_multiplier"))
        if line is None:
            continue
        spid = str(entry.get("subject_id"))
        if sport not in namecache:
            namecache[sport] = _players(sport)
        wtype = entry.get("wager_type")
        rows.append(dict(
            sport=sport, source="sleeper", source_player_id=spid,
            player_name=namecache[sport].get(spid),
            team=entry.get("options", [{}])[0].get("metadata", {}).get("subject_team")
                 or (opts[0].get("subject_team") if opts else None),
            opp_team=None,
            stat_type=STAT.get(wtype, (wtype or "").replace("_", " ")),
            line=line, line_type=entry.get("line_type") or "normal",
            over_mult=over_mult, under_mult=under_mult,
            start_ts=None, game_ref=entry.get("game_id"),
            raw=json.dumps({"wager_type": wtype, "line_id": entry.get("line_id")}),
        ))
        bysport[sport] = bysport.get(sport, 0) + 1

    if rows:
        import psycopg2.extras
        cols = ["sport", "source", "source_player_id", "player_name", "team", "opp_team",
                "stat_type", "line", "line_type", "over_mult", "under_mult", "start_ts",
                "game_ref", "raw"]
        vals = [tuple(r.get(c) for c in cols) for r in rows]
        with con.cursor() as cur:
            psycopg2.extras.execute_values(cur,
                f"INSERT INTO odds_prop_snapshot ({','.join(cols)}) VALUES %s", vals, page_size=1000)
        con.commit()
    print(f"  sleeper: {len(rows)} props {bysport}")
    return len(rows)


if __name__ == "__main__":
    con = db.connect()
    n = collect_sleeper(con)
    db.log_run(con, "odds_props_sleeper", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL sleeper prop snapshots: {n}")
    con.close()
