"""REAL +EV engine: a soft book (Sleeper) priced against a sharp reference
(Bovada), de-vigged. No prediction model.

  1. Bovada two-way O/U props -> de-vig -> fair P(over), P(under).
  2. Match Sleeper props by (player, canonical stat, SAME line).
  3. Sleeper pays a fixed multiplier each side. EV = P_fair(side) * mult - 1.
  4. Surface the best side per match. +EV when EV > 0.

Bovada isn't Pinnacle-sharp, so treat output as candidates; CLV/realized results
(see value_log.py / value_grade.py) are the verdict. `scan()` returns structured
plays for logging; `main()` prints them.
"""
import re, unicodedata
import db

# anchor preference: SGO de-vigged consensus fair odds first, Bovada as fallback
ANCHOR_BOOKS = ("sgo_fair", "bovada")
BET_BOOK = "sleeper"


def am_to_prob(a):
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else (-a) / (-a + 100.0)


def norm(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def scan(con, window_h=12):
    """Return list of play dicts: best side per matched (player,stat,line)."""
    cur = con.cursor()
    # pull both anchors; prefer sgo_fair (de-vigged consensus) over bovada per key
    cur.execute("""
      SELECT DISTINCT ON (book, sport, player_name, stat_type, line)
             book, sport, player_name, stat_type, line, over_american, under_american
      FROM odds_book_prop_snapshot
      WHERE book = ANY(%s) AND snapshot_ts > now() - (%s || ' hours')::interval
      ORDER BY book, sport, player_name, stat_type, line, snapshot_ts DESC
    """, (list(ANCHOR_BOOKS), window_h))
    pref = {b: i for i, b in enumerate(ANCHOR_BOOKS)}   # lower index = preferred
    sharp = {}
    sharp_src = {}
    for book, sport, pname, stat, line, oa, ua in cur.fetchall():
        if oa is None or ua is None:
            continue
        k = (sport, norm(pname), stat, float(line))
        if k in sharp_src and pref[sharp_src[k]] <= pref[book]:
            continue
        qo, qu = am_to_prob(oa), am_to_prob(ua)
        tot = qo + qu
        sharp[k] = (qo / tot, qu / tot, pname)
        sharp_src[k] = book

    cur.execute("""
      SELECT sport, player_name, stat_type, line,
             (array_agg(over_mult  ORDER BY snapshot_ts DESC))[1],
             (array_agg(under_mult ORDER BY snapshot_ts DESC))[1]
      FROM odds_prop_snapshot
      WHERE source=%s AND over_mult IS NOT NULL
        AND snapshot_ts > now() - (%s || ' hours')::interval
      GROUP BY sport, player_name, stat_type, line
    """, (BET_BOOK, window_h))

    plays = []
    for sport, pname, stat, line, om, um in cur.fetchall():
        key = (sport, norm(pname), stat, float(line))
        if key not in sharp:
            continue
        p_over, p_under, sharp_name = sharp[key]
        om, um = float(om), float(um)
        ev_o, ev_u = p_over * om - 1, p_under * um - 1
        if ev_o >= ev_u:
            side, ev, p, mult = "OVER", ev_o, p_over, om
        else:
            side, ev, p, mult = "UNDER", ev_u, p_under, um
        plays.append(dict(sport=sport, player_name=pname, stat_type=stat, line=float(line),
                          side=side, ev=ev, fair_prob=p, offered_mult=mult,
                          bet_book=BET_BOOK, anchor_book=sharp_src[key]))
    plays.sort(key=lambda x: -x["ev"])
    return plays


def main(min_ev=-1.0):
    con = db.connect()
    plays = scan(con)
    print(f"=== Sleeper +EV vs Bovada (de-vigged)   matched: {len(plays)} ===")
    print(f"{'EV':>7} {'SP':4} {'PLAYER':20} {'STAT':18} {'LINE':>5} {'SIDE':5} {'MULT':>5} {'P_fair':>6}")
    for p in plays:
        if p["ev"] < min_ev:
            continue
        print(f"{p['ev']:+7.1%} {p['sport']:4} {p['player_name'][:20]:20} {p['stat_type'][:18]:18} "
              f"{p['line']:5.1f} {p['side']:5} {p['offered_mult']:5.2f} {p['fair_prob']:6.1%}")
    npos = sum(1 for p in plays if p["ev"] > 0)
    print(f"\n{npos} +EV of {len(plays)} matched. (validate on CLV — see value_grade.py)")
    con.close()


if __name__ == "__main__":
    import sys
    main(float(sys.argv[1]) if len(sys.argv) > 1 else -1.0)
