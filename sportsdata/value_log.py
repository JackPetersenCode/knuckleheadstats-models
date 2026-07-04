"""Persist value plays from the +EV-vs-sharp scan into value_play (one row per
play, kept from FIRST detection = the 'open' recommendation). Run frequently
(wired into the odds cadence). value_grade.py later fills CLV + outcome.

Logs ALL matched plays above a low floor (not just +EV) so we can later validate
that higher predicted-EV plays actually realize higher ROI — i.e. prove the method,
not just the picks. recommended = (ev > 0) at log time.
"""
import datetime
import db
import ev_vs_sharp as E
import best_line as B
import game_value as G


def log_plays(con, min_ev=-0.10):
    # prop plays: Sleeper-vs-anchor (method validation) + real-book +EV-vs-fair (the lead)
    plays = E.scan(con)
    try:
        plays = plays + B.book_ev_plays(con, min_ev=0.02)
    except Exception as e:
        print(f"  value_log: book_ev_plays ERR {repr(e)[:70]}")
    # game-market plays: moneyline / spread / total price value vs consensus fair
    try:
        plays = plays + G.game_ev_plays(con, min_ev=0.02)
    except Exception as e:
        print(f"  value_log: game_ev_plays ERR {repr(e)[:70]}")
    today = datetime.date.today()
    rows = []
    for p in plays:
        if p["ev"] < min_ev:
            continue
        rows.append((today, p["sport"], p["player_name"], p["stat_type"], p["line"],
                     p["side"], p["bet_book"], p["offered_mult"], p["anchor_book"],
                     p["fair_prob"], p["ev"], p["ev"] > 0,
                     p.get("market_type", "prop"), p.get("event_ref"),
                     p.get("home_team"), p.get("away_team"), p.get("model_fair")))
    if not rows:
        print("value_log: 0 plays to log")
        return 0
    import psycopg2.extras
    sql = """INSERT INTO value_play
      (game_date, sport, player_name, stat_type, line, side, bet_book, offered_mult,
       anchor_book, fair_prob, ev, recommended,
       market_type, event_ref, home_team, away_team, model_fair)
      VALUES %s
      ON CONFLICT (game_date, sport, player_name, stat_type, line, side, bet_book)
      DO NOTHING"""
    with con.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=1000)
        logged = cur.rowcount
    con.commit()
    rec = sum(1 for r in rows if r[11])
    print(f"value_log: {len(rows)} matched (>={min_ev:.0%} EV), {rec} recommended (+EV); "
          f"{logged} new rows inserted")
    _log_shop(con)
    return logged


def _log_shop(con):
    """Persist ALL line-shopping plays (props + game markets) so the highest-value
    ones are never lost to the post's display cap."""
    shop = []
    try:
        shop += [dict(p, market_type="prop") for p in B.shop_plays(con)]
    except Exception as e:
        print(f"  value_log: shop_plays ERR {repr(e)[:70]}")
    try:
        shop += G.game_shop_plays(con)            # already carry market_type
    except Exception as e:
        print(f"  value_log: game_shop_plays ERR {repr(e)[:70]}")
    if not shop:
        return
    today = datetime.date.today()
    rows = [(today, p["sport"], p["player_name"], p["stat_type"], p["line"], p["side"],
             p["best_book"], p["best_dec"], p["worst_dec"], p["edge"], p["n_books"],
             p.get("market_type", "prop")) for p in shop]
    import psycopg2.extras
    sql = """INSERT INTO shop_play
      (game_date, sport, player_name, stat_type, line, side, best_book, best_dec, worst_dec, edge, n_books, market_type)
      VALUES %s
      ON CONFLICT (game_date, sport, player_name, stat_type, line, side) DO NOTHING"""
    with con.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=1000)
        n = cur.rowcount
    con.commit()
    print(f"value_log: {len(rows)} shop plays, {n} new persisted to shop_play")


if __name__ == "__main__":
    con = db.connect()
    log_plays(con)
    con.close()
