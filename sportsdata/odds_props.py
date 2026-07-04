"""Player-prop odds collector — FREE public DFS endpoints.

PrizePicks: api.prizepicks.com/projections?league_id={id}  (per league)
Underdog:   api.underdogfantasy.com/beta/v5/over_under_lines (all sports, one call)

Append-only snapshots into odds_prop_snapshot. Run several times/day to capture
open->close movement (CLV). Outcomes are graded later from the box-score tables.
"""
import json, time, random
import db
from http_util import get_json, to_num

PP_LEAGUE = {"nba": 7, "mlb": 2, "nhl": 8, "nfl": 9, "wnba": 3}
UD_SPORT = {"NBA": "nba", "MLB": "mlb", "NHL": "nhl", "NFL": "nfl", "WNBA": "wnba"}
PP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch_prizepicks(sport):
    lid = PP_LEAGUE[sport]
    d = get_json(f"https://api.prizepicks.com/projections?league_id={lid}&per_page=1000",
                 headers=PP_HEADERS, retries=6, backoff=4.0)
    inc = {(i["type"], i["id"]): i for i in d.get("included", [])}
    out = []
    for p in d.get("data", []):
        a = p["attributes"]; rel = p.get("relationships", {})
        npref = rel.get("new_player", {}).get("data")
        np = inc.get(("new_player", npref["id"])) if npref else None
        npa = np["attributes"] if np else {}
        out.append(dict(sport=sport, source="prizepicks",
                        source_player_id=npref["id"] if npref else None,
                        player_name=npa.get("name"), team=npa.get("team"),
                        opp_team=a.get("description"), stat_type=a.get("stat_type"),
                        line=to_num(a.get("line_score")), line_type=a.get("odds_type"),
                        over_mult=None, under_mult=None, start_ts=a.get("start_time"),
                        game_ref=(rel.get("game", {}).get("data") or {}).get("id"),
                        raw=json.dumps(a)))
    return out


def fetch_underdog():
    d = get_json("https://api.underdogfantasy.com/beta/v5/over_under_lines",
                 headers=PP_HEADERS)
    players = {p["id"]: p for p in d.get("players", [])}
    apps = {a["id"]: a for a in d.get("appearances", [])}
    games = {g["id"]: g for g in d.get("games", [])}
    out = []
    for o in d.get("over_under_lines", []):
        ou = o.get("over_under", {})
        astat = ou.get("appearance_stat", {})
        app = apps.get(astat.get("appearance_id"))
        if not app:
            continue
        pl = players.get(app.get("player_id"))
        if not pl:
            continue
        sport = UD_SPORT.get(pl.get("sport_id"))
        if not sport:
            continue
        opts = {opt.get("choice"): to_num(opt.get("payout_multiplier")) for opt in o.get("options", [])}
        gm = games.get(app.get("match_id"), {})
        out.append(dict(sport=sport, source="underdog",
                        source_player_id=app.get("player_id"),
                        player_name=f"{pl.get('first_name','')} {pl.get('last_name','')}".strip(),
                        team=pl.get("team_id"), opp_team=None,
                        stat_type=astat.get("display_stat"), line=to_num(o.get("stat_value")),
                        line_type=o.get("line_type") or "standard",
                        over_mult=opts.get("higher"), under_mult=opts.get("lower"),
                        start_ts=gm.get("scheduled_at"), game_ref=app.get("match_id"),
                        raw=json.dumps({"line_id": o.get("id"), "stat": astat.get("stat")})))
    return out


COLS = ["sport", "source", "source_player_id", "player_name", "team", "opp_team",
        "stat_type", "line", "line_type", "over_mult", "under_mult", "start_ts", "game_ref", "raw"]


def collect_props(con):
    total = 0
    rows_all = []
    # PrizePicks per active sport
    for sport in ("nba", "mlb", "nhl", "nfl", "wnba"):
        try:
            rows = fetch_prizepicks(sport)
            rows_all += rows
            print(f"  prizepicks {sport}: {len(rows)} props")
        except Exception as e:
            print(f"  prizepicks {sport}: ERR {repr(e)[:70]}")
        time.sleep(5 + random.random() * 3)
    # Underdog all sports
    try:
        ud = fetch_underdog()
        rows_all += ud
        bysport = {}
        for r in ud:
            bysport[r["sport"]] = bysport.get(r["sport"], 0) + 1
        print(f"  underdog: {len(ud)} props {bysport}")
    except Exception as e:
        print(f"  underdog: ERR {repr(e)[:70]}")

    # insert (append-only)
    if rows_all:
        import psycopg2.extras
        vals = [tuple(r.get(c) for c in COLS) for r in rows_all]
        sql = f"INSERT INTO odds_prop_snapshot ({','.join(COLS)}) VALUES %s"
        with con.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, vals, page_size=1000)
        con.commit()
        total = len(rows_all)
    return total


if __name__ == "__main__":
    con = db.connect()
    n = collect_props(con)
    db.log_run(con, "odds_props", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL prop snapshots inserted: {n}")
    con.close()
