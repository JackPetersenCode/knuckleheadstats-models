"""Daily driver for the v2 predictive-feature layer (companion to collect.py).

  MLB: Statcast pitches (last N days) + per-game context (weather/ump/probables/handedness)
  NFL: current-season nflverse snap counts + advanced offensive stats (idempotent re-pull)
  NHL: current-season MoneyPuck skater/goalie advanced metrics

Run after collect.py in run_stats.bat. Per-season CSVs are re-pulled in full each run
(cheap, idempotent upsert) so late stat corrections are captured.
"""
import datetime, time, random, argparse
import db
import statcast_src, mlb_context_src, nfl_adv_src, nhl_adv_src


def mlb_current_season(today):
    return today.year


def nfl_current_season(today):
    # nflverse season = starting year; Sep-Dec -> this year, Jan-Aug -> last year
    return today.year if today.month >= 9 else today.year - 1


def nhl_current_season(today):
    # MoneyPuck season = starting year; Oct-Dec -> this year, Jan-Sep -> last year
    return today.year if today.month >= 10 else today.year - 1


def run_daily(con, days=3, today=None):
    today = today or datetime.date.today()

    print("== MLB Statcast (last %d days) ==" % days)
    sc = 0
    for d in range(days):
        day = today - datetime.timedelta(days=d)
        try:
            n = statcast_src.collect_day(day, con)
            sc += n
            if n:
                print(f"  statcast {day}: {n} pitches")
            db.log_run(con, "statcast", "mlb", day, 0, n, "ok")
        except Exception as e:
            con.rollback(); print(f"  statcast {day}: ERR {repr(e)[:80]}")
        time.sleep(0.4 + random.random() * 0.4)

    print("== MLB game context (pending finals) ==")
    try:
        nc = mlb_context_src.collect_pending(con, limit=200)
        print(f"  filled {nc} contexts")
        db.log_run(con, "mlb_context", "mlb", None, 0, nc, "ok")
    except Exception as e:
        con.rollback(); print(f"  context ERR {repr(e)[:80]}")

    print("== NFL advanced (nflverse, current season) ==")
    try:
        nfl_adv_src.collect_season(nfl_current_season(today), con)
    except Exception as e:
        con.rollback(); print(f"  nfl adv ERR {repr(e)[:80]}")

    print("== NHL advanced (MoneyPuck, current season) ==")
    try:
        nhl_adv_src.collect_season(nhl_current_season(today), con)
    except Exception as e:
        con.rollback(); print(f"  nhl adv ERR {repr(e)[:80]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    con = db.connect()
    run_daily(con, days=args.days)
    con.close()
    print("ADVANCED DAILY DONE")
