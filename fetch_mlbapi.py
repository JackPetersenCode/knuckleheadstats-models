"""Fetch weather + umpires + lineups for every game in mlb_features_v2.

Uses MLB Stats API directly (no auth, public).  Concurrency 12 workers.
Persists to:
  mlb_game_weather  (game_pk pk)
  mlb_game_umpires  (game_pk pk)
  mlb_game_lineups  (game_pk, team_side, batting_order) composite pk
Skips game_pks already loaded so it's safely resumable.
"""
import os
import re
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from concurrent.futures import ThreadPoolExecutor, as_completed

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
URL = "https://statsapi.mlb.com/api/v1.1/game/{}/feed/live"
CONCURRENCY = 12
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (research)"


def parse_wind(w):
    """'5 mph, Out To LF' -> (5, 'Out To LF')"""
    if not w: return None, None
    m = re.match(r"(\d+)\s*mph[,\s]*(.*)", w)
    if not m: return None, w
    return int(m.group(1)), (m.group(2) or "").strip() or None


def parse_temp(t):
    if not t: return None
    try: return int(t)
    except Exception:
        try: return int(re.search(r"\d+", t).group(0))
        except Exception: return None


def fetch_one(game_pk):
    """Returns dict with weather, umpires, lineups, or None on hard failure."""
    try:
        r = SESSION.get(URL.format(game_pk), timeout=20)
        if r.status_code == 404:
            return {"game_pk": game_pk, "missing": True}
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return {"game_pk": game_pk, "error": str(e)[:80]}

    gd = d.get("gameData", {}) or {}
    ld = d.get("liveData", {}) or {}

    w = gd.get("weather", {}) or {}
    wind_mph, wind_dir = parse_wind(w.get("wind"))
    weather = dict(
        game_pk=game_pk,
        condition=(w.get("condition") or None),
        temp_f=parse_temp(w.get("temp")),
        wind_mph=wind_mph,
        wind_dir=wind_dir,
    )

    plate = None
    plate_id = None
    for o in ld.get("boxscore", {}).get("officials", []) or []:
        if o.get("officialType") == "Home Plate":
            plate = o.get("official", {}).get("fullName")
            plate_id = o.get("official", {}).get("id")
            break
    umpires = dict(game_pk=game_pk, plate_umpire_id=plate_id, plate_umpire_name=plate)

    lineups = []
    teams = ld.get("boxscore", {}).get("teams", {}) or {}
    for side in ("home", "away"):
        t = teams.get(side, {}) or {}
        bo = t.get("battingOrder") or []
        players = t.get("players") or {}
        for i, pid in enumerate(bo):
            key = f"ID{pid}"
            pl = players.get(key, {}) or {}
            person = pl.get("person", {}) or {}
            bat_side = (pl.get("batSide") or {}).get("code")
            lineups.append((
                game_pk, side, i + 1, pid,
                person.get("fullName"), bat_side,
            ))
    return dict(game_pk=game_pk, weather=weather, umpires=umpires, lineups=lineups)


def ensure_schema(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mlb_game_weather (
                game_pk    integer PRIMARY KEY,
                condition  varchar(40),
                temp_f     integer,
                wind_mph   integer,
                wind_dir   varchar(40)
            );
            CREATE TABLE IF NOT EXISTS mlb_game_umpires (
                game_pk            integer PRIMARY KEY,
                plate_umpire_id    integer,
                plate_umpire_name  varchar(80)
            );
            CREATE TABLE IF NOT EXISTS mlb_game_lineups (
                game_pk        integer  NOT NULL,
                team_side      varchar(8) NOT NULL,
                batting_order  integer  NOT NULL,
                player_id      integer,
                player_name    varchar(80),
                bat_side       varchar(2),
                PRIMARY KEY (game_pk, team_side, batting_order)
            );
            CREATE INDEX IF NOT EXISTS idx_lineups_player_game
                ON mlb_game_lineups (player_id, game_pk);
        """)
    pg.commit()


def already_fetched(pg):
    with pg.cursor() as c:
        c.execute("SELECT game_pk FROM mlb_game_weather")
        return {r[0] for r in c.fetchall()}


def list_targets(pg):
    with pg.cursor() as c:
        c.execute("""
            SELECT DISTINCT game_pk FROM mlb_features_v2
            WHERE ml_home_close IS NOT NULL
            ORDER BY game_pk
        """)
        return [r[0] for r in c.fetchall()]


def flush(pg, rows):
    if not rows: return
    weather_rows = [(r["weather"]["game_pk"], r["weather"]["condition"],
                     r["weather"]["temp_f"], r["weather"]["wind_mph"],
                     r["weather"]["wind_dir"])
                    for r in rows if "weather" in r]
    umpire_rows  = [(r["umpires"]["game_pk"], r["umpires"]["plate_umpire_id"],
                     r["umpires"]["plate_umpire_name"])
                    for r in rows if "umpires" in r]
    lineup_rows  = [t for r in rows if "lineups" in r for t in r["lineups"]]

    with pg.cursor() as c:
        if weather_rows:
            execute_values(c,
                "INSERT INTO mlb_game_weather VALUES %s "
                "ON CONFLICT (game_pk) DO NOTHING", weather_rows)
        if umpire_rows:
            execute_values(c,
                "INSERT INTO mlb_game_umpires VALUES %s "
                "ON CONFLICT (game_pk) DO NOTHING", umpire_rows)
        if lineup_rows:
            execute_values(c,
                "INSERT INTO mlb_game_lineups VALUES %s "
                "ON CONFLICT (game_pk, team_side, batting_order) DO NOTHING",
                lineup_rows)
    pg.commit()


def main():
    pg = psycopg2.connect(**PG)
    ensure_schema(pg)
    done = already_fetched(pg)
    targets = [g for g in list_targets(pg) if g not in done]
    print(f"Total targets: {len(targets)} (already fetched: {len(done)})")

    t0 = time.time()
    batch = []
    n_ok = n_err = n_missing = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(fetch_one, g): g for g in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if "error" in res:
                n_err += 1
            elif res.get("missing"):
                n_missing += 1
            else:
                batch.append(res)
                n_ok += 1
            if len(batch) >= 200:
                flush(pg, batch); batch = []
            if i % 500 == 0:
                rate = i / max(time.time()-t0, 0.001)
                print(f"  {i}/{len(targets)}  ok={n_ok} err={n_err} miss={n_missing}  "
                      f"({rate:.1f} games/sec)")
        flush(pg, batch)

    print(f"\nDone. ok={n_ok} err={n_err} miss={n_missing} in {time.time()-t0:.0f}s")
    with pg.cursor() as c:
        c.execute("SELECT COUNT(*) FROM mlb_game_weather"); print(f"  weather rows: {c.fetchone()[0]}")
        c.execute("SELECT COUNT(*) FROM mlb_game_umpires"); print(f"  umpire rows: {c.fetchone()[0]}")
        c.execute("SELECT COUNT(*) FROM mlb_game_lineups"); print(f"  lineup rows: {c.fetchone()[0]}")
    pg.close()


if __name__ == "__main__":
    main()
