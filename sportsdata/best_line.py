"""Line-shopping + +EV finder across REAL sportsbooks — the affiliate engine.

Now that SGO gives us true two-way prices from DraftKings/FanDuel/BetMGM/Caesars/
ESPN BET (+ Sleeper, + Bovada) and a de-vigged consensus FAIR price, we can produce:
  (1) BEST-LINE value  — which book pays most for a pick ("bet it at <book>" -> affiliate)
  (2) +EV-vs-fair      — a book whose price beats the consensus fair prob (true +EV)

All prices normalized to decimal. Same-line comparison only. sgo_fair is the
reference (not a bettable book). Sleeper is DFS (true odds); PrizePicks/Underdog are
parlay-only and excluded (their multipliers aren't standalone prices).
"""
import re, unicodedata, collections
import db

# bettable books with TRUE single-bet odds (each maps to an affiliate program except bovada)
BETTABLE = {"draftkings", "fanduel", "betmgm", "caesars", "espnbet", "sleeper", "bovada"}
AFFILIATE = {"draftkings", "fanduel", "betmgm", "caesars", "espnbet", "sleeper"}


def norm(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def am_to_dec(a):
    a = float(a)
    return 1 + (a / 100.0 if a > 0 else 100.0 / -a)


def am_to_prob(a):
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else (-a) / (-a + 100.0)


def _build_market(con, window_h):
    """-> (market[key][side]={book:decimal}, fair[key]=(p_over,p_under), disp[npn]=name)."""
    cur = con.cursor()
    market = collections.defaultdict(lambda: collections.defaultdict(dict))
    fair, disp = {}, {}
    # sportsbooks + fair from odds_book_prop_snapshot
    cur.execute("""
      SELECT DISTINCT ON (book, sport, player_name, stat_type, line)
             book, sport, player_name, stat_type, line, over_american, under_american
      FROM odds_book_prop_snapshot
      WHERE snapshot_ts > now() - (%s || ' hours')::interval
      ORDER BY book, sport, player_name, stat_type, line, snapshot_ts DESC
    """, (window_h,))
    for book, sport, pname, stat, line, oa, ua in cur.fetchall():
        if oa is None or ua is None:
            continue
        k = (sport, norm(pname), stat, float(line)); disp[norm(pname)] = pname
        if book == "sgo_fair":
            qo, qu = am_to_prob(oa), am_to_prob(ua); t = qo + qu
            fair[k] = (qo / t, qu / t)
        elif book in BETTABLE:
            market[k]["OVER"][book] = am_to_dec(oa)
            market[k]["UNDER"][book] = am_to_dec(ua)

    # Sleeper (DFS multipliers == decimal) from odds_prop_snapshot
    cur.execute("""
      SELECT sport, player_name, stat_type, line,
             (array_agg(over_mult  ORDER BY snapshot_ts DESC))[1],
             (array_agg(under_mult ORDER BY snapshot_ts DESC))[1]
      FROM odds_prop_snapshot WHERE source='sleeper' AND over_mult IS NOT NULL
        AND snapshot_ts > now() - (%s || ' hours')::interval
      GROUP BY sport, player_name, stat_type, line
    """, (window_h,))
    for sport, pname, stat, line, om, um in cur.fetchall():
        k = (sport, norm(pname), stat, float(line)); disp[norm(pname)] = pname
        if om: market[k]["OVER"]["sleeper"] = float(om)
        if um: market[k]["UNDER"]["sleeper"] = float(um)
    return market, fair, disp


# +EV guardrails: thin markets + longshot fair-odds are false-positive zones.
MIN_BOOKS = 4          # need a well-supported consensus fair line
EV_SANITY_CAP = 0.20   # >20% +EV vs a consensus fair line = almost surely stale/mismatch


def book_ev_plays(con, window_h=14, min_ev=0.02):
    """+EV-vs-consensus-fair plays on real books -> structured dicts (for logging)."""
    market, fair, disp = _build_market(con, window_h)
    out = []
    for k, sides in market.items():
        sport, npn, stat, line = k
        if k not in fair:
            continue
        for side, books in sides.items():
            if len(books) < MIN_BOOKS:
                continue
            best_b, best_d = max(books.items(), key=lambda x: x[1])
            if best_b not in AFFILIATE:
                continue
            p = fair[k][0] if side == "OVER" else fair[k][1]
            ev = p * best_d - 1
            if min_ev <= ev <= EV_SANITY_CAP:
                out.append(dict(sport=sport, player_name=disp[npn], stat_type=stat, line=line,
                                side=side, ev=ev, fair_prob=p, offered_mult=best_d,
                                bet_book=best_b, anchor_book="sgo_fair"))
    out.sort(key=lambda x: -x["ev"])
    return out


# line-shopping compares SPORTSBOOKS only (same bet, different book). Sleeper is a
# DFS product with different line semantics -> excluded (mixing it = stale artifacts).
SHOP_BOOKS = {"draftkings", "fanduel", "betmgm", "caesars", "espnbet"}


def shop_plays(con, window_h=14, min_edge=0.06):
    """Best-line value: where the best sportsbook price beats the worst by >= min_edge.
    Robust value (no fair-odds dependency) -> the safe affiliate content."""
    market, fair, disp = _build_market(con, window_h)
    out = []
    for k, sides in market.items():
        sport, npn, stat, line = k
        for side, allbooks in sides.items():
            books = {b: d for b, d in allbooks.items() if b in SHOP_BOOKS}
            if len(books) < 3:                 # >=3 sportsbooks so a stale price isn't "best"
                continue
            best_b, best_d = max(books.items(), key=lambda x: x[1])
            worst_d = min(books.values())
            edge = best_d / worst_d - 1
            if min_edge <= edge <= 0.60 and best_b in AFFILIATE:
                out.append(dict(sport=sport, player_name=disp[npn], stat_type=stat, line=line,
                                side=side, best_book=best_b, best_dec=best_d, worst_dec=worst_d,
                                edge=edge, n_books=len(books)))
    out.sort(key=lambda x: -x["edge"])
    return out


def main(window_h=14, min_shop_edge=0.04, min_ev=0.02):
    con = db.connect()
    market, fair, disp = _build_market(con, window_h)
    shop, ev_plays = [], []
    for k, sides in market.items():
        sport, npn, stat, line = k
        for side, books in sides.items():
            if len(books) < 2:
                continue
            best_b, best_d = max(books.items(), key=lambda x: x[1])
            worst_d = min(books.values())
            edge = best_d / worst_d - 1
            if edge >= min_shop_edge and best_b in AFFILIATE and len(books) >= 3 and edge <= 0.60:
                shop.append((edge, sport, disp[npn], stat, line, side, best_b, best_d, worst_d, len(books)))
            if k in fair and len(books) >= MIN_BOOKS:
                p = fair[k][0] if side == "OVER" else fair[k][1]
                ev = p * best_d - 1
                if min_ev <= ev <= EV_SANITY_CAP and best_b in AFFILIATE:
                    ev_plays.append((ev, sport, disp[npn], stat, line, side, best_b, best_d, p))

    shop.sort(reverse=True); ev_plays.sort(reverse=True)
    print(f"=== +EV vs consensus fair (best book price beats fair prob, >= {min_ev:.0%}) ===")
    if not ev_plays:
        print("  none today")
    for ev, sport, pname, stat, line, side, bb, bd, p in ev_plays[:25]:
        print(f"  {ev:+5.1%}  {sport:4} {pname[:20]:20} {stat[:16]:16} {line:5.1f} {side:5} "
              f"@ {bb:10} {bd:4.2f} (fairP {p:.0%})")

    print(f"\n=== BEST-LINE shopping value (best vs worst book, >= {min_shop_edge:.0%}) ===")
    for edge, sport, pname, stat, line, side, bb, bd, wd, nb in shop[:25]:
        print(f"  +{edge:4.0%}  {sport:4} {pname[:20]:20} {stat[:16]:16} {line:5.1f} {side:5} "
              f"best @ {bb:10} {bd:4.2f} (worst {wd:4.2f}, {nb} books)")
    print(f"\n{len(ev_plays)} +EV plays, {len(shop)} shoppable edges. Each book -> affiliate link.")
    con.close()


if __name__ == "__main__":
    import sys
    main()
