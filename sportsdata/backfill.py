"""Historical backfill — idempotent + resumable.

Iterates day-by-day over each sport's season windows, calling collect_date.
Already-loaded boxscores are skipped, so re-running resumes where it left off.
Empty days (no games) return fast.

Usage:
  python backfill.py                # all sports, default season windows
  python backfill.py --sport mlb    # one sport
  python backfill.py --sport nba --start 2025-10-21 --end 2026-06-03
"""
import sys, datetime, argparse, time
import db
import collect

# season windows to pull (start, end) inclusive — extend freely; re-runs are cheap.
# Generated for 10+ seasons per sport. Bounds are generous (empty days return fast,
# already-loaded boxscores are skipped), so exact dates need not be precise.
def _gen(md_start, md_end, y0, y1, cross_year):
    return [(f"{y}-{md_start}", f"{(y + 1) if cross_year else y}-{md_end}") for y in range(y0, y1 + 1)]

SEASONS = {
    "nba": _gen("10-15", "06-30", 2016, 2025, True),   # 2016-17 .. 2025-26
    "nhl": _gen("10-01", "06-30", 2016, 2025, True),   # 2016-17 .. 2025-26
    "mlb": _gen("03-15", "11-10", 2016, 2026, False),  # 2016 .. 2026
    "nfl": _gen("09-01", "02-20", 2016, 2025, True),   # 2016 .. 2025
}


def run_window(sport, start, end, con):
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    day = d0
    tot_g = tot_b = 0
    t_start = time.time()
    while day <= d1:
        try:
            ng, nb = collect.collect_date(sport, day, con, load_box=True)
            tot_g += ng; tot_b += nb
            if ng or nb:
                print(f"  {sport} {day}: {ng} g, {nb} box   (cum {tot_g}g/{tot_b}b)", flush=True)
            db.log_run(con, "backfill", sport, day, ng, nb, "ok")
        except Exception as e:
            con.rollback()  # clear aborted txn before logging
            print(f"  {sport} {day}: ERR {repr(e)[:100]}", flush=True)
            try:
                db.log_run(con, "backfill", sport, day, 0, 0, "error", repr(e))
            except Exception:
                con.rollback()
        con.commit()
        day += datetime.timedelta(days=1)
    mins = (time.time() - t_start) / 60
    print(f"== {sport} {start}..{end} done: {tot_g} games, {tot_b} box rows, {mins:.1f} min ==", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="all")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    sports = list(SEASONS) if args.sport == "all" else [args.sport]
    con = db.connect()
    for sp in sports:
        windows = [(args.start, args.end)] if args.start and args.end else SEASONS[sp]
        for start, end in windows:
            run_window(sp, start, end, con)
    con.close()
    print("BACKFILL COMPLETE", flush=True)


if __name__ == "__main__":
    main()
