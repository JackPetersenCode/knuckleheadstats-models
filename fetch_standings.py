"""Fetch daily standings snapshots from MLB Stats API.

Endpoint: https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season=YYYY&date=YYYY-MM-DD
Stores per (game_date, team_id): wins, losses, win_pct, gb (games back), streak,
last_ten_wins, last_ten_losses, run_diff.

We fetch one snapshot per distinct game_date for which we have games.
"""
import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from concurrent.futures import ThreadPoolExecutor, as_completed

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
URL = "https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season={}&date={}"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (research)"


def fetch_date(season, date_str):
    try:
        r = SESSION.get(URL.format(season, date_str), timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"date": date_str, "error": str(e)[:80]}
    rows = []
    for rec in data.get("records", []) or []:
        for tr in rec.get("teamRecords", []) or []:
            team_id = tr.get("team", {}).get("id")
            streak = tr.get("streak", {}).get("streakCode")
            # last 10 from splitRecords
            l10w = l10l = None
            for sr in tr.get("records", {}).get("splitRecords", []) or []:
                if sr.get("type") == "lastTen":
                    l10w = sr.get("wins"); l10l = sr.get("losses")
                    break
            rows.append((
                date_str, team_id, tr.get("wins"), tr.get("losses"),
                float(tr.get("winningPercentage") or 0),
                _to_float(tr.get("gamesBack")),
                streak, l10w, l10l,
                _safe_int(tr.get("runDifferential") or tr.get("runsScored", 0) and (
                    int(tr.get("runsScored", 0)) - int(tr.get("runsAllowed", 0))
                )),
            ))
    return {"date": date_str, "rows": rows}


def _to_float(s):
    if s in (None, "-", ""):
        return None
    try: return float(s)
    except Exception: return None


def _safe_int(v):
    try: return int(v) if v is not None else None
    except Exception: return None


def ensure_schema(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mlb_team_standings (
                snapshot_date date    NOT NULL,
                team_id       integer NOT NULL,
                wins          integer,
                losses        integer,
                win_pct       numeric,
                games_back    numeric,
                streak        varchar(8),
                l10_wins      integer,
                l10_losses    integer,
                run_diff      integer,
                PRIMARY KEY (snapshot_date, team_id)
            );
            CREATE INDEX IF NOT EXISTS idx_stand_team_date
                ON mlb_team_standings (team_id, snapshot_date);
        """)
    pg.commit()


def main():
    pg = psycopg2.connect(**PG)
    ensure_schema(pg)
    with pg.cursor() as c:
        c.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM game_date)::int AS season, game_date::date
            FROM mlb_features_v2 WHERE ml_home_close IS NOT NULL
            ORDER BY 2
        """)
        all_dates = c.fetchall()
        c.execute("SELECT DISTINCT snapshot_date FROM mlb_team_standings")
        done = {r[0] for r in c.fetchall()}
    targets = [(s, d) for s, d in all_dates if d not in done]
    print(f"Targets: {len(targets)} dates ({len(done)} already done)")

    t0 = time.time()
    n_ok = n_err = 0
    batch = []

    def flush():
        nonlocal batch
        if not batch:
            return
        flat = [r for x in batch for r in x.get("rows", [])]
        if flat:
            with pg.cursor() as c:
                execute_values(c,
                    "INSERT INTO mlb_team_standings VALUES %s "
                    "ON CONFLICT (snapshot_date, team_id) DO NOTHING", flat)
            pg.commit()
        batch = []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_date, s, str(d)): (s, d) for s, d in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if "error" in r: n_err += 1
            else:
                n_ok += 1
                batch.append(r)
            if len(batch) >= 50:
                flush()
            if i % 200 == 0:
                print(f"  {i}/{len(targets)}  ok={n_ok} err={n_err}  "
                      f"({i/(time.time()-t0):.1f} req/s)")
        flush()

    with pg.cursor() as c:
        c.execute("SELECT COUNT(*) FROM mlb_team_standings")
        print(f"\nDone. ok={n_ok} err={n_err} in {time.time()-t0:.0f}s")
        print(f"  total standings rows: {c.fetchone()[0]}")
    pg.close()


if __name__ == "__main__":
    main()
