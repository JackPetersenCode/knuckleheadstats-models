"""Regenerate the picks site's data files from the LIVE value engine (sportsedge).

Writes, next to index.html:
  - picks.json   : the latest daily ranked best-bets board (what to bet today)
  - record.json  : the CLV-verified results record (last 30d + all-time)
  - record.csv   : full public audit of every graded, recommended play

Run daily after value_grade settles (see publish.bat). The page fetches these client-side.
Reads sportsedge (best_bets + value_play) — NOT the old hoop_scoop.picks_published.
"""
import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path

import psycopg2

OUT = Path(__file__).resolve().parent
PG = dict(host=os.environ.get("SPORTSEDGE_PGHOST", "localhost"),
          user=os.environ.get("SPORTSEDGE_PGUSER", "postgres"),
          dbname=os.environ.get("SPORTSEDGE_PGDB", "sportsedge"),
          # password comes from the environment (SPORTSEDGE_PGPASS), never hardcoded
          # here — this file is committed. Set it once with:  setx SPORTSEDGE_PGPASS ...
          password=(os.environ.get("SPORTSEDGE_PGPASS") or os.environ.get("PGPASSWORD")))

TIER_LABEL = {"proven": "Proven edge", "edge": "Positive CLV", "price": "Price value",
              "model": "Model", "efficient": "Lean"}
BOOK_LABEL = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
              "espnbet": "ESPN BET", "caesars": "Caesars", "sleeper": "Sleeper", "bovada": "Bovada"}


def dec_to_am(d):
    if not d:
        return ""
    d = float(d)
    return f"+{round((d-1)*100)}" if d >= 2 else f"-{round(100/(d-1))}"


def _picks(cur):
    """Latest slate's ranked board -> picks.json."""
    cur.execute("SELECT MAX(game_date) FROM best_bets")
    day = cur.fetchone()[0]
    picks = []
    if day:
        cur.execute("""SELECT rank, sport, market_type, selection, stat_type, line, side,
                              bet_book, offered_dec, ev, confidence
                       FROM best_bets WHERE game_date=%s ORDER BY rank LIMIT 30""", (day,))
        for rk, sport, mt, sel, stat, line, side, book, dec, ev, conf in cur.fetchall():
            market = stat if mt == "prop" else mt.upper()
            if line is not None and mt != "h2h":
                market += f" {float(line):g}"
            picks.append(dict(
                rank=rk, sport=(sport or "").upper(), selection=sel, market=market,
                side=(side or "").title(), book=BOOK_LABEL.get(book, (book or "").title()),
                odds=dec_to_am(dec), ev=round(min(float(ev or 0), 0.25), 3),
                ev_capped=bool(float(ev or 0) > 0.25),
                tier=conf, tier_label=TIER_LABEL.get(conf, conf or "")))
    (OUT / "picks.json").write_text(json.dumps(
        dict(date=str(day) if day else None, count=len(picks), picks=picks), indent=2))
    print(f"picks.json: {len(picks)} picks for {day}")


def _summary(cur, since=None):
    where = "recommended AND result IS NOT NULL"
    args = []
    if since:
        where += " AND game_date >= %s"
        args.append(since)
    cur.execute(f"""
      SELECT COUNT(*) FILTER (WHERE result IN ('win','loss')) settled,
             COUNT(*) FILTER (WHERE result='win') w,
             COUNT(*) FILTER (WHERE result='loss') l,
             COALESCE(SUM(CASE result WHEN 'win' THEN offered_mult-1 WHEN 'loss' THEN -1 ELSE 0 END),0)::float profit,
             AVG(clv)::float avg_clv,
             AVG((clv>0)::int)::float clv_pos
      FROM value_play WHERE {where}""", args)
    settled, w, l, profit, avg_clv, clv_pos = cur.fetchone()
    roi = (profit / settled) if settled else 0.0
    return dict(settled=int(settled or 0), wins=int(w or 0), losses=int(l or 0),
                profit_units=round(float(profit or 0), 1), roi=round(roi, 4),
                avg_clv=round(float(avg_clv or 0), 4),
                clv_pos_share=round(float(clv_pos or 0), 3))


def _recent(cur, n=14):
    cur.execute("""SELECT game_date, sport, player_name, stat_type, market_type, side, line,
                          bet_book, result, round(clv::numeric,3)
                   FROM value_play WHERE recommended AND result IN ('win','loss')
                   ORDER BY graded_ts DESC NULLS LAST, game_date DESC LIMIT %s""", (n,))
    out = []
    for gd, sport, pn, stat, mt, side, line, book, result, clv in cur.fetchall():
        market = stat if mt == "prop" else (mt or "").upper()
        if line is not None and mt != "h2h":
            market += f" {float(line):g}"
        out.append(dict(date=str(gd), sport=(sport or "").upper(), pick=pn,
                        market=market, side=(side or "").title(),
                        book=BOOK_LABEL.get(book, (book or "").title()),
                        result=result, clv=float(clv) if clv is not None else None))
    return out


def _record(cur):
    rec = dict(as_of=str(date.today()),
               l30=_summary(cur, date.today() - timedelta(days=30)),
               all=_summary(cur),
               recent=_recent(cur))
    # keep legacy keys the old page used, so nothing breaks mid-deploy
    rec.update(window_days=30, settled_30=rec["l30"]["settled"],
               wins_30=rec["l30"]["wins"], losses_30=rec["l30"]["losses"],
               profit_units_30=rec["l30"]["profit_units"])
    (OUT / "record.json").write_text(json.dumps(rec, indent=2))
    print("record.json all-time:", json.dumps(rec["all"]))


def _audit(cur):
    cur.execute("""SELECT game_date, sport, market_type, player_name AS pick, stat_type, side, line,
                          bet_book, offered_mult, round(ev::numeric,3) AS ev, result,
                          round(clv::numeric,3) AS clv
                   FROM value_play
                   WHERE recommended AND result IS NOT NULL
                   ORDER BY game_date DESC, value_score DESC NULLS LAST LIMIT 5000""")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    with (OUT / "record.csv").open("w", newline="", encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(cols)
        wtr.writerows(rows)
    print(f"record.csv: {len(rows)} rows")


def main():
    con = psycopg2.connect(**PG)
    with con.cursor() as cur:
        _picks(cur)
        _record(cur)
        _audit(cur)
    con.close()


if __name__ == "__main__":
    main()
