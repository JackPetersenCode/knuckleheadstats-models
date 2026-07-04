"""Game-line odds collector (moneyline / spread / total) — FREE via ESPN.

Per sport: scoreboard (upcoming games) -> per event core-odds endpoint ->
one row per (provider, market, outcome). Append-only into odds_game_snapshot.
Multiple providers (DraftKings, ESPN BET, etc.) captured when ESPN exposes them.
"""
import json, time, random, datetime
import db
from http_util import get_json, to_int, to_num

CORE = {"nba": ("basketball", "nba"), "nfl": ("football", "nfl"),
        "mlb": ("baseball", "mlb"), "nhl": ("hockey", "nhl")}
SITE = {"nba": "basketball/nba", "nfl": "football/nfl", "mlb": "baseball/mlb", "nhl": "hockey/nhl"}


def _events(sport, date):
    sb = get_json(f"https://site.api.espn.com/apis/site/v2/sports/{SITE[sport]}/scoreboard?dates={date.strftime('%Y%m%d')}")
    out = []
    for e in sb.get("events", []):
        if e["status"]["type"].get("completed"):
            continue
        comp = e["competitions"][0]
        home = away = None
        for c in comp.get("competitors", []):
            ab = c.get("team", {}).get("abbreviation")
            if c.get("homeAway") == "home": home = ab
            else: away = ab
        out.append((e["id"], comp["id"], e.get("date"), home, away))
    return out


def _price(v):
    """American odds: valid only if |price| >= 100. ESPN uses 0 as a missing placeholder."""
    p = to_int(v)
    return p if (p is not None and abs(p) >= 100) else None


def _rows_for_event(sport, eid, cid, commence, home, away):
    s, lg = CORE[sport]
    try:
        co = get_json(f"https://sports.core.api.espn.com/v2/sports/{s}/leagues/{lg}/events/{eid}/competitions/{cid}/odds")
    except Exception:
        return []
    # nba/nfl event id == our game_id; mlb/nhl resolved later
    gid = eid if sport in ("nba", "nfl") else None
    rows = []
    for it in co.get("items", []):
        book = (it.get("provider") or {}).get("name")
        base = dict(sport=sport, source="espn", event_ref=eid, game_id=gid, commence_ts=commence,
                    home_team=home, away_team=away, book=book)
        ho, ao = it.get("homeTeamOdds", {}), it.get("awayTeamOdds", {})
        # moneyline
        if ho.get("moneyLine") is not None:
            rows.append({**base, "market": "h2h", "outcome": "home", "line": None,
                         "price": _price(ho.get("moneyLine")), "raw": None})
        if ao.get("moneyLine") is not None:
            rows.append({**base, "market": "h2h", "outcome": "away", "line": None,
                         "price": _price(ao.get("moneyLine")), "raw": None})
        # spread (home perspective)
        sp = it.get("spread")
        if sp is not None:
            rows.append({**base, "market": "spread", "outcome": "home", "line": to_num(sp),
                         "price": _price(ho.get("spreadOdds")), "raw": None})
            rows.append({**base, "market": "spread", "outcome": "away", "line": -to_num(sp),
                         "price": _price(ao.get("spreadOdds")), "raw": None})
        # total
        ou = it.get("overUnder")
        if ou is not None:
            rows.append({**base, "market": "total", "outcome": "over", "line": to_num(ou),
                         "price": _price(it.get("overOdds")), "raw": None})
            rows.append({**base, "market": "total", "outcome": "under", "line": to_num(ou),
                         "price": _price(it.get("underOdds")), "raw": None})
    return rows


COLS = ["sport", "source", "event_ref", "game_id", "commence_ts", "home_team", "away_team",
        "book", "market", "outcome", "line", "price", "raw"]


def collect_game_odds(con, date=None):
    date = date or datetime.date.today()
    import psycopg2.extras
    total = 0
    for sport in ("nba", "nfl", "mlb", "nhl"):
        try:
            evs = _events(sport, date)
        except Exception as e:
            print(f"  {sport}: scoreboard ERR {repr(e)[:60]}"); continue
        rows = []
        for eid, cid, commence, home, away in evs:
            rows += _rows_for_event(sport, eid, cid, commence, home, away)
            time.sleep(0.25 + random.random() * 0.25)
        if rows:
            vals = [tuple(r.get(c) for c in COLS) for r in rows]
            with con.cursor() as cur:
                psycopg2.extras.execute_values(cur,
                    f"INSERT INTO odds_game_snapshot ({','.join(COLS)}) VALUES %s", vals, page_size=1000)
            con.commit()
        print(f"  {sport}: {len(evs)} games, {len(rows)} odds rows")
        total += len(rows)
    return total


if __name__ == "__main__":
    con = db.connect()
    n = collect_game_odds(con)
    db.log_run(con, "odds_game", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL game-odds rows: {n}")
    con.close()
