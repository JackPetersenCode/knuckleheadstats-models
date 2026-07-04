"""Second game-odds source: The Odds API (multi-book h2h/spreads/totals).

Needs a FREE api key (the-odds-api.com, 500 credits/mo) in env ODDS_API_KEY.
Without the key this skips cleanly, so the platform still runs on ESPN alone.

Quota care: free tier = 500 credits/mo; cost = (#markets x #regions) per call.
We request 3 markets x 1 region = 3 credits per sport-call. To stay well under
500/mo we (a) only call sports that have games today, and (b) throttle to at most
once per ~20h (so ~1 call/sport/day). Rows go to odds_game_snapshot (source='oddsapi').
"""
import os, json, ssl, urllib.request, datetime
import db
from http_util import to_int, to_num

KEY = os.environ.get("ODDS_API_KEY")
SPORT_KEY = {"nba": "basketball_nba", "nfl": "americanfootball_nfl",
             "mlb": "baseball_mlb", "nhl": "icehockey_nhl"}
_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE
MARKET = {"h2h": "h2h", "spreads": "spread", "totals": "total"}
COLS = ["sport", "source", "event_ref", "game_id", "commence_ts", "home_team", "away_team",
        "book", "market", "outcome", "line", "price", "raw"]


def _fetch(sport_key):
    url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
           f"?apiKey={KEY}&regions=us&markets=h2h,spreads,totals&oddsFormat=american&dateFormat=iso")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
        remaining = r.headers.get("x-requests-remaining")
        return json.load(r), remaining


def _parse(sport, events):
    rows = []
    for ev in events:
        home, away = ev.get("home_team"), ev.get("away_team")
        base = dict(sport=sport, source="oddsapi", event_ref=ev.get("id"), game_id=None,
                    commence_ts=ev.get("commence_time"), home_team=home, away_team=away)
        for bk in ev.get("bookmakers", []):
            book = bk.get("title")
            for mk in bk.get("markets", []):
                market = MARKET.get(mk.get("key"))
                if not market:
                    continue
                for o in mk.get("outcomes", []):
                    nm = o.get("name")
                    if market == "total":
                        outcome = "over" if nm == "Over" else "under"
                    else:
                        outcome = "home" if nm == home else "away"
                    rows.append({**base, "book": book, "market": market, "outcome": outcome,
                                 "line": to_num(o.get("point")), "price": to_int(o.get("price")), "raw": None})
    return rows


def _has_games_today(con, sport):
    today = datetime.date.today()
    with con.cursor() as cur:
        cur.execute("SELECT count(*) FROM game WHERE sport=%s AND game_date BETWEEN %s AND %s",
                    (sport, today - datetime.timedelta(days=1), today + datetime.timedelta(days=1)))
        return cur.fetchone()[0] > 0


def _recently_pulled(con, sport, hours):
    with con.cursor() as cur:
        cur.execute("SELECT max(snapshot_ts) FROM odds_game_snapshot WHERE source='oddsapi' AND sport=%s", (sport,))
        last = cur.fetchone()[0]
    if not last:
        return False
    return (datetime.datetime.now(last.tzinfo) - last) < datetime.timedelta(hours=hours)


def collect(con, throttle_hours=20):
    if not KEY:
        print("  oddsapi: SKIP (set ODDS_API_KEY to enable second game-odds source)")
        return 0
    import psycopg2.extras
    total = 0
    for sport, skey in SPORT_KEY.items():
        if not _has_games_today(con, sport):
            continue
        if _recently_pulled(con, sport, throttle_hours):
            print(f"  oddsapi {sport}: skip (pulled within {throttle_hours}h)")
            continue
        try:
            events, remaining = _fetch(skey)
        except Exception as e:
            print(f"  oddsapi {sport}: ERR {repr(e)[:70]}")
            continue
        rows = _parse(sport, events)
        if rows:
            vals = [tuple(r.get(c) for c in COLS) for r in rows]
            with con.cursor() as cur:
                psycopg2.extras.execute_values(cur,
                    f"INSERT INTO odds_game_snapshot ({','.join(COLS)}) VALUES %s", vals, page_size=1000)
            con.commit()
        nbooks = len({r["book"] for r in rows})
        print(f"  oddsapi {sport}: {len(events)} games, {len(rows)} rows, {nbooks} books "
              f"(credits left: {remaining})")
        total += len(rows)
    return total


if __name__ == "__main__":
    con = db.connect()
    n = collect(con)
    db.log_run(con, "odds_game_api", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL oddsapi rows: {n}")
    con.close()
