"""SportsGameOdds collector — the SHARP anchor + real multi-book prices.

Free "amateur" tier: 2,500 ENTITIES/month, where 1 event = 1 entity (the odds
inside an event are free). So a full daily slate (~15 MLB games) = ~15 entities.
We THROTTLE to a few pulls/day to stay well under budget.

Per player-prop OU market SGO gives:
  fairOdds      -> de-vigged CONSENSUS fair price (our anchor; no manual de-vig)
  openFairOdds  -> opening fair price (built-in CLV reference)
  byBookmaker   -> real DraftKings/FanDuel/BetMGM/Caesars/ESPN BET prices (line-shop)

Storage (odds_book_prop_snapshot, one row per book per market):
  book='sgo_fair'  -> over/under = fairOdds        (the sharp anchor)
  book=<sportsbook> -> that book's over/under odds  (affiliate line-shopping)
Player names resolved from event['players']; stat_type mapped to our canonical vocab.
"""
import os, json, datetime
import db
from http_util import to_int, get_json

KEY = os.environ.get("SGO_API_KEY")
BASE = "https://api.sportsgameodds.com/v2"
LEAGUES = {"mlb": "MLB", "nba": "NBA", "nhl": "NHL"}   # NFL off-season; add when active
# affiliate-relevant sportsbooks to keep for line-shopping (skip the rest)
KEEP_BOOKS = {"draftkings", "fanduel", "betmgm", "caesars", "espnbet"}

# SGO uses PREFIXED statIDs for MLB (batting_/pitching_) but GENERIC ones for NBA/NHL
# (bare "points","rebounds",...) that mean different things per sport -> map sport-aware.
PREFIXED = {
    "batting_hits": "hits", "batting_totalBases": "total bases", "batting_RBI": "rbis",
    "batting_homeRuns": "home runs", "batting_doubles": "doubles", "batting_triples": "triples",
    "batting_singles": "singles", "batting_stolenBases": "stolen bases",
    "batting_basesOnBalls": "batter walks", "batting_strikeouts": "batter strikeouts",
    "batting_hits+runs+rbi": "hits+runs+rbis",
    "pitching_strikeouts": "pitcher strikeouts", "pitching_earnedRuns": "earned runs allowed",
    "pitching_hits": "hits allowed", "pitching_basesOnBalls": "walks allowed",
    "pitching_outs": "pitching outs",
}
GENERIC = {
    "mlb": {"points": "runs"},   # SGO 'points' == runs scored in baseball
    "nba": {"points": "points", "rebounds": "rebounds", "assists": "assists",
            "steals": "steals", "blocks": "blocked shots", "threePointersMade": "3-pt made",
            "points+rebounds+assists": "pts+rebs+asts", "points+rebounds": "pts+rebs",
            "points+assists": "pts+asts", "rebounds+assists": "rebs+asts"},
    "nhl": {"points": "points", "shots": "shots on goal", "goals": "goals",
            "assists": "assists", "saves": "saves"},
}


def map_stat(sport, stat_id):
    if stat_id in PREFIXED:
        return PREFIXED[stat_id]
    return GENERIC.get(sport, {}).get(stat_id)

def _get(path):
    # browser UA via http_util (SGO/Cloudflare 403s the default python-urllib UA)
    return get_json(BASE + path, headers={"X-Api-Key": KEY}, timeout=40, retries=4)


def _am(s):
    return to_int(str(s).replace("EVEN", "100")) if s is not None else None


GAME_BOOKS = KEEP_BOOKS | {"bovada"}   # game-line books to keep (incl bovada for breadth)


def _pulled_today(con):
    with con.cursor() as cur:
        cur.execute("SELECT max(snapshot_ts) FROM odds_book_prop_snapshot WHERE book='sgo_fair'")
        last = cur.fetchone()[0]
    return last is not None and last.astimezone().date() == datetime.date.today()


def collect_sgo(con, after_hour=17, force=False, max_events_per_league=40):
    """Once/day, EVENING pull (so 'current' fair odds ≈ closing line for CLV; SGO also
    gives openFairOdds for the open). Collects BOTH player props (-> odds_book_prop_snapshot)
    and game ML/spread/total (-> odds_game_snapshot), fair + per-book. Game markets ride
    the same event fetch, so they cost NO extra entities."""
    if not KEY:
        print("  sgo: SKIP (set SGO_API_KEY)"); return 0
    if not force:
        if _pulled_today(con):
            print("  sgo: skip (already pulled today; ~1x/day protects 2500/mo budget)"); return 0
        if datetime.datetime.now().hour < after_hour:
            print(f"  sgo: skip (waiting for evening window >={after_hour}:00 local for close-line CLV)")
            return 0
    import psycopg2.extras
    prop_rows, game_rows = [], []
    for sport, lg in LEAGUES.items():
        try:
            resp = _get(f"/events/?leagueID={lg}&oddsAvailable=true&limit={max_events_per_league}")
        except Exception as e:
            print(f"  sgo {sport}: ERR {repr(e)[:60]}"); continue
        evs = resp.get("data", [])
        nprops = ngame = 0
        for ev in evs:
            players = ev.get("players", {})
            teams = ev.get("teams", {})
            home = (teams.get("home", {}).get("names", {}) or {}).get("short")
            away = (teams.get("away", {}).get("names", {}) or {}).get("short")
            commence = (ev.get("status", {}) or {}).get("startsAt")
            eid = ev.get("eventID")
            odds = ev.get("odds", {})
            for oid, o in odds.items():
                bt = o.get("betTypeID")
                # ---------- PLAYER PROPS (ou markets with a playerID) ----------
                if o.get("playerID") and bt == "ou" and o.get("sideID") == "over":
                    stat = map_stat(sport, o.get("statID"))
                    if not stat:
                        continue
                    under = odds.get(o.get("opposingOddID"), {})
                    line = o.get("fairOverUnder") or o.get("bookOverUnder")
                    pname = (players.get(o["playerID"], {}) or {}).get("name")
                    if line is None or not pname:
                        continue
                    line = float(line)
                    base = dict(sport=sport, event_ref=eid, home_team=home, away_team=away,
                                commence_ts=commence, player_name=pname, stat_type=stat, line=line)
                    fo, fu = _am(o.get("fairOdds")), _am(under.get("fairOdds"))
                    if fo is not None and fu is not None:
                        prop_rows.append({**base, "book": "sgo_fair", "over_american": fo,
                            "under_american": fu, "raw": json.dumps({
                                "open_over": _am(o.get("openFairOdds")),
                                "open_under": _am(under.get("openFairOdds")),
                                "open_ou": o.get("openFairOverUnder")})})
                    ob, ub = o.get("byBookmaker", {}), under.get("byBookmaker", {})
                    for bk in KEEP_BOOKS:
                        bo, bu = ob.get(bk), ub.get(bk)
                        if not bo or not bu or not bo.get("available"):
                            continue
                        prop_rows.append({**base, "book": bk, "line": float(bo.get("overUnder", line)),
                            "over_american": _am(bo.get("odds")), "under_american": _am(bu.get("odds")),
                            "raw": None})
                    nprops += 1
                    continue
                # ---------- GAME MARKETS (ml / spread / total, full game) ----------
                if o.get("playerID") or o.get("periodID") != "game" or bt not in ("ml", "sp", "ou"):
                    continue
                market = {"ml": "h2h", "sp": "spread", "ou": "total"}[bt]
                outcome = o.get("sideID")          # home/away or over/under
                if market != "total" and outcome not in ("home", "away"):
                    continue
                gline = (o.get("fairSpread") if bt == "sp"
                         else o.get("fairOverUnder") if bt == "ou" else None)
                gbase = dict(sport=sport, source="sgo", event_ref=eid, game_id=None,
                             commence_ts=commence, home_team=home, away_team=away,
                             market=market, outcome=outcome)
                fp = _am(o.get("fairOdds"))
                if fp is not None:
                    game_rows.append({**gbase, "book": "sgo_fair",
                        "line": float(gline) if gline is not None else None, "price": fp,
                        "raw": json.dumps({"open_odds": _am(o.get("openFairOdds"))})})
                for bk, bd in (o.get("byBookmaker", {}) or {}).items():
                    if bk not in GAME_BOOKS or not bd.get("available"):
                        continue
                    bl = bd.get("spread") if bt == "sp" else bd.get("overUnder") if bt == "ou" else gline
                    game_rows.append({**gbase, "book": bk,
                        "line": float(bl) if bl is not None else None,
                        "price": _am(bd.get("odds")), "raw": None})
                ngame += 1
        print(f"  sgo {sport}: {len(evs)} events, {nprops} props, {ngame} game-market sides")

    # insert props
    prop_rows = [r for r in prop_rows if r.get("over_american") and r.get("under_american")]
    if prop_rows:
        cols = ["sport", "book", "event_ref", "home_team", "away_team", "commence_ts",
                "player_name", "stat_type", "line", "over_american", "under_american", "raw"]
        with con.cursor() as cur:
            psycopg2.extras.execute_values(cur,
                f"INSERT INTO odds_book_prop_snapshot ({','.join(cols)}) VALUES %s",
                [tuple(r.get(c) for c in cols) for r in prop_rows], page_size=1000)
        con.commit()
    # insert game markets
    game_rows = [r for r in game_rows if r.get("price")]
    if game_rows:
        gcols = ["sport", "source", "event_ref", "game_id", "commence_ts", "home_team",
                 "away_team", "book", "market", "outcome", "line", "price", "raw"]
        with con.cursor() as cur:
            psycopg2.extras.execute_values(cur,
                f"INSERT INTO odds_game_snapshot ({','.join(gcols)}) VALUES %s",
                [tuple(r.get(c) for c in gcols) for r in game_rows], page_size=1000)
        con.commit()
    print(f"  sgo: {len(prop_rows)} prop rows + {len(game_rows)} game-line rows stored")
    return len(prop_rows) + len(game_rows)


if __name__ == "__main__":
    con = db.connect()
    n = collect_sgo(con, force=True)   # manual run ignores throttle/time window
    db.log_run(con, "odds_sgo", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL sgo rows: {n}")
    con.close()
