"""Sharp(er) reference: Bovada player props — FREE, scrapable, two-way American odds.

Bovada is a real sportsbook (vig priced in), far sharper than DFS apps. We collect
its two-way O/U player props so we can DE-VIG them into fair probabilities and bet
Sleeper wherever Sleeper's payout beats Bovada's fair price (the +EV-vs-sharp play).

Endpoints (no auth, browser UA):
  list:  /services/sports/event/v2/events/A/description/{path}
  event: /services/sports/event/coupon/events/A/description{link}?lang=en
Props are pulled for imminent/in-progress games, so we skip games starting soon.
Append-only into odds_book_prop_snapshot.
"""
import json, time, random, re
import db
from http_util import get_json

SPORT_PATH = {"mlb": "baseball/mlb", "nba": "basketball/nba",
              "nhl": "hockey/nhl", "nfl": "football/nfl"}

# Bovada market label (before ' - ') -> canonical stat string (matches stat_map vocab)
STAT = {
    "total strikeouts": "pitcher strikeouts", "total hits allowed": "hits allowed",
    "total pitcher walks": "walks allowed", "total pitcher outs": "pitching outs",
    "total earned runs": "earned runs allowed",
    "total bases": "total bases", "total hits": "hits", "total runs": "runs",
    "total rbis": "rbis", "total hits, runs and rbis": "hits+runs+rbis",
    "total doubles": "doubles", "total singles": "singles", "total home runs": "home runs",
    "total stolen bases": "stolen bases", "total walks": "batter walks",
    # nba
    "total points": "points", "total rebounds": "rebounds", "total assists": "assists",
    "total made threes": "3-pt made", "total 3-pt made": "3-pt made",
    "total steals": "steals", "total blocks": "blocked shots",
    "total points + rebounds + assists": "pts+rebs+asts",
    # nhl
    "total shots on goal": "shots on goal", "total goals": "goals", "total saves": "saves",
}


def _am(price):
    a = (price or {}).get("american")
    if a in (None, "EVEN", "even"):
        return 100 if a else None
    try:
        return int(str(a).replace("+", ""))
    except (ValueError, TypeError):
        return None


def _events(sport):
    d = get_json(f"https://www.bovada.lv/services/sports/event/v2/events/A/description/{SPORT_PATH[sport]}")
    return d[0]["events"] if d else []


def _event_markets(link):
    d = get_json(f"https://www.bovada.lv/services/sports/event/coupon/events/A/description{link}?lang=en")
    return d[0]["events"][0] if d and d[0].get("events") else None


def collect_bovada(con, now_ms=None, skip_within_min=35):
    import psycopg2.extras
    rows = []
    for sport in ("mlb", "nba", "nhl"):
        try:
            evs = _events(sport)
        except Exception as e:
            print(f"  bovada {sport}: events ERR {repr(e)[:60]}"); continue
        ngames = 0
        for e in evs:
            start = e.get("startTime")
            # skip in-progress / imminent (props pulled); needs now_ms passed in (no Date.now here)
            if e.get("live"):
                continue
            link = e.get("link")
            if not link:
                continue
            try:
                ev = _event_markets(link)
            except Exception:
                continue
            if not ev:
                continue
            comps = ev.get("competitors", [])
            home = next((c.get("description") for c in comps if c.get("home")), None)
            away = next((c.get("description") for c in comps if not c.get("home")), None)
            got = 0
            for g in ev.get("displayGroups", []):
                if "Prop" not in (g.get("description") or ""):
                    continue
                for m in g.get("markets", []):
                    desc = m.get("description", "")
                    if " - " not in desc:
                        continue
                    label, who = desc.split(" - ", 1)
                    stat = STAT.get(label.strip().lower())
                    if not stat:
                        continue
                    player = re.sub(r"\s*\([^)]*\)\s*$", "", who).strip()
                    outs = {(o.get("description") or "").lower(): o for o in m.get("outcomes", [])}
                    ov, un = outs.get("over"), outs.get("under")
                    if not ov or not un:
                        continue
                    line = (ov.get("price") or {}).get("handicap")
                    rows.append(dict(
                        sport=sport, book="bovada", event_ref=str(e.get("id")),
                        home_team=home, away_team=away, commence_ts=None,
                        player_name=player, stat_type=stat,
                        line=float(line) if line is not None else None,
                        over_american=_am(ov.get("price")), under_american=_am(un.get("price")),
                        raw=json.dumps({"label": label.strip(), "start": start})))
                    got += 1
            if got:
                ngames += 1
            time.sleep(0.4 + random.random() * 0.5)
        print(f"  bovada {sport}: {ngames} games with props")

    rows = [r for r in rows if r["line"] is not None and r["over_american"] and r["under_american"]]
    if rows:
        cols = ["sport", "book", "event_ref", "home_team", "away_team", "commence_ts",
                "player_name", "stat_type", "line", "over_american", "under_american", "raw"]
        vals = [tuple(r.get(c) for c in cols) for r in rows]
        with con.cursor() as cur:
            psycopg2.extras.execute_values(cur,
                f"INSERT INTO odds_book_prop_snapshot ({','.join(cols)}) VALUES %s", vals, page_size=1000)
        con.commit()
    print(f"  bovada: {len(rows)} prop lines stored")
    return len(rows)


if __name__ == "__main__":
    con = db.connect()
    n = collect_bovada(con)
    db.log_run(con, "odds_bovada", "all", None, 0, n, "ok")
    con.commit()
    print(f"TOTAL bovada book-prop lines: {n}")
    con.close()
