"""Historical backfill for the v2 predictive-feature layer — idempotent + resumable.

  statcast : day-by-day over MLB season windows, 2015 (first Statcast season) -> now
  context  : per-game weather/ump/probables for all finalized MLB games (collect_pending)
  nfl      : nflverse snap counts + advanced, per season 2016 -> last completed
  nhl      : MoneyPuck skater/goalie advanced, per season 2015 -> current

Usage:
  python backfill_advanced.py --part statcast            # the long pole (~30M rows)
  python backfill_advanced.py --part context
  python backfill_advanced.py --part nfl
  python backfill_advanced.py --part nhl
  python backfill_advanced.py --part all                 # everything, in order
  python backfill_advanced.py --part statcast --start 2015-03-01 --end 2026-06-14
"""
import datetime, time, random, argparse
import db
import statcast_src, mlb_context_src, nfl_adv_src, nhl_adv_src

# MLB-season day windows for Statcast (Mar->Nov incl. postseason). Generous bounds; empty days are fast.
STATCAST_WINDOWS = [(f"{y}-03-15", f"{y}-11-10") for y in range(2015, 2027)]
NFL_SEASONS = list(range(2016, 2026))   # 2016..2025 (10 completed seasons)
NHL_SEASONS = list(range(2015, 2027))   # 2015..2026 (MoneyPuck season = start year)


def backfill_statcast(con, start=None, end=None):
    windows = [(start, end)] if start and end else STATCAST_WINDOWS
    grand = 0
    for s, e in windows:
        d0 = datetime.date.fromisoformat(s); d1 = datetime.date.fromisoformat(e)
        today = datetime.date.today()
        if d1 > today:
            d1 = today
        day, tot, t0 = d0, 0, time.time()
        while day <= d1:
            try:
                n = statcast_src.collect_day(day, con)
                tot += n
                if n:
                    print(f"  statcast {day}: {n}  (win cum {tot})", flush=True)
                db.log_run(con, "bf_statcast", "mlb", day, 0, n, "ok")
            except Exception as ex:
                con.rollback(); print(f"  statcast {day}: ERR {repr(ex)[:90]}", flush=True)
                try: db.log_run(con, "bf_statcast", "mlb", day, 0, 0, "error", repr(ex))
                except Exception: con.rollback()
            con.commit()
            time.sleep(0.5 + random.random() * 0.6)
            day += datetime.timedelta(days=1)
        grand += tot
        print(f"== statcast {s}..{e}: {tot} pitches, {(time.time()-t0)/60:.1f} min ==", flush=True)
    print(f"STATCAST BACKFILL DONE: {grand} pitches", flush=True)


def backfill_context(con):
    """Loop collect_pending in batches until no MLB final lacks a context row."""
    total = 0
    while True:
        n = mlb_context_src.collect_pending(con, limit=500)
        total += n
        print(f"  context batch: +{n} (total {total})", flush=True)
        if n == 0:
            break
    print(f"CONTEXT BACKFILL DONE: {total} games", flush=True)


def backfill_nfl(con):
    for yr in NFL_SEASONS:
        nfl_adv_src.collect_season(yr, con)
        time.sleep(1.0)
    print("NFL ADV BACKFILL DONE", flush=True)


def backfill_nhl(con):
    for yr in NHL_SEASONS:
        nhl_adv_src.collect_season(yr, con)
        time.sleep(1.0)
    print("NHL ADV BACKFILL DONE", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", default="all", choices=["all", "statcast", "context", "nfl", "nhl"])
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    con = db.connect()
    if args.part in ("all", "nhl"):
        backfill_nhl(con)
    if args.part in ("all", "nfl"):
        backfill_nfl(con)
    if args.part in ("all", "context"):
        backfill_context(con)
    if args.part in ("all", "statcast"):
        backfill_statcast(con, args.start, args.end)
    con.close()
    print("BACKFILL_ADVANCED COMPLETE", flush=True)


if __name__ == "__main__":
    main()
