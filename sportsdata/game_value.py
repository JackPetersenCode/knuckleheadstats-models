"""Game-market value scanner (moneyline / spread / total), all sports.

Mirror of best_line.py but for odds_game_snapshot. The anchor is SGO's de-vigged
consensus fair price (book='sgo_fair'); value is purely a PRICE discrepancy vs that
consensus (game prediction models don't beat the market here), validated forward by
CLV. Emits dicts shaped for the unified value logger.
"""
import collections
import json

import db
from best_line import am_to_dec, am_to_prob, AFFILIATE, BETTABLE, SHOP_BOOKS, MIN_BOOKS, EV_SANITY_CAP


def _build_game_market(con, window_h):
    """-> market[key]={book:decimal}, fair[key]=p, fair_open[key]=p_open, disp[event_ref]=(home,away)
    where key = (sport, event_ref, market, outcome, line)  (h2h line -> 0.0)."""
    cur = con.cursor()
    market = collections.defaultdict(dict)
    fairam = collections.defaultdict(dict)   # (sport,eref,market,line) -> {outcome: (close_am, open_am)}
    disp = {}
    cur.execute("""
      SELECT DISTINCT ON (book, sport, event_ref, market, outcome, line)
             book, sport, event_ref, market, outcome, line, price, raw, home_team, away_team
      FROM odds_game_snapshot
      WHERE source='sgo' AND price IS NOT NULL
        AND snapshot_ts > now() - (%s || ' hours')::interval
      ORDER BY book, sport, event_ref, market, outcome, line, snapshot_ts DESC
    """, (window_h,))
    for book, sport, eref, mkt, outcome, line, price, raw, ht, at in cur.fetchall():
        L = float(line) if line is not None else 0.0
        disp[eref] = (ht, at)
        if book == "sgo_fair":
            openam = None
            if raw:
                rj = raw if isinstance(raw, dict) else json.loads(raw)
                openam = rj.get("open_odds")
            fairam[(sport, eref, mkt, L)][outcome] = (price, openam)
        elif book in BETTABLE:
            market[(sport, eref, mkt, outcome, L)][book] = am_to_dec(price)

    fair, fair_open = {}, {}
    for (sport, eref, mkt, L), outs in fairam.items():
        if len(outs) < 2:                       # need both sides to de-vig
            continue
        qs = {o: am_to_prob(am) for o, (am, _) in outs.items()}
        tot = sum(qs.values())
        for o in outs:
            fair[(sport, eref, mkt, o, L)] = qs[o] / tot
        opens = {o: op for o, (_, op) in outs.items() if op is not None}
        if len(opens) == len(outs):
            qo = {o: am_to_prob(am) for o, am in opens.items()}
            to = sum(qo.values())
            for o in opens:
                fair_open[(sport, eref, mkt, o, L)] = qo[o] / to
    return market, fair, fair_open, disp


def _selection(mkt, outcome, ht, at):
    if mkt in ("h2h", "spread"):
        return ht if outcome == "home" else at
    return f"{at}@{ht}"                          # total -> matchup


def game_ev_plays(con, window_h=20, min_ev=0.02):
    """Best affiliate-book price beats consensus fair -> +EV (price value)."""
    market, fair, _open, disp = _build_game_market(con, window_h)
    out = []
    for key, books in market.items():
        sport, eref, mkt, outcome, L = key
        if key not in fair or len(books) < MIN_BOOKS:
            continue
        aff = {b: d for b, d in books.items() if b in AFFILIATE}
        if not aff:
            continue
        best_b, best_d = max(aff.items(), key=lambda x: x[1])
        p = fair[key]
        ev = p * best_d - 1
        if min_ev <= ev <= EV_SANITY_CAP:
            ht, at = disp.get(eref, (None, None))
            out.append(dict(sport=sport, market_type=mkt, event_ref=eref,
                            player_name=_selection(mkt, outcome, ht, at), stat_type=mkt,
                            line=L, side=outcome.upper(), ev=ev, fair_prob=p,
                            offered_mult=best_d, bet_book=best_b, anchor_book="sgo_fair",
                            home_team=ht, away_team=at))
    out.sort(key=lambda x: -x["ev"])
    return out


def game_shop_plays(con, window_h=20, min_edge=0.04):
    """Best vs worst sportsbook price on the same game bet (line-shopping value)."""
    market, _fair, _open, disp = _build_game_market(con, window_h)
    out = []
    for key, allbooks in market.items():
        sport, eref, mkt, outcome, L = key
        books = {b: d for b, d in allbooks.items() if b in SHOP_BOOKS}
        if len(books) < 3:
            continue
        best_b, best_d = max(books.items(), key=lambda x: x[1])
        worst_d = min(books.values())
        edge = best_d / worst_d - 1
        if min_edge <= edge <= 0.60 and best_b in AFFILIATE:
            ht, at = disp.get(eref, (None, None))
            out.append(dict(sport=sport, market_type=mkt, player_name=_selection(mkt, outcome, ht, at),
                            stat_type=mkt, line=L, side=outcome.upper(), best_book=best_b,
                            best_dec=best_d, worst_dec=worst_d, edge=edge, n_books=len(books)))
    out.sort(key=lambda x: -x["edge"])
    return out


def main(window_h=20, min_ev=0.02):
    con = db.connect()
    ev = game_ev_plays(con, window_h, min_ev)
    shop = game_shop_plays(con, window_h)
    print(f"=== GAME +EV vs consensus fair (>= {min_ev:.0%}) — {len(ev)} ===")
    for p in ev[:25]:
        print(f"  {p['ev']:+5.1%}  {p['sport']:4} {p['market_type']:6} {p['player_name'][:14]:14} "
              f"{p['side']:5} {p['line']:>5} @ {p['bet_book']:10} {p['offered_mult']:.2f} (fairP {p['fair_prob']:.0%})")
    print(f"\n=== GAME line-shopping (best vs worst, >= 4%) — {len(shop)} ===")
    for p in shop[:25]:
        print(f"  +{p['edge']:4.0%}  {p['sport']:4} {p['market_type']:6} {p['player_name'][:14]:14} "
              f"{p['side']:5} best @ {p['best_book']:10} {p['best_dec']:.2f} ({p['n_books']} books)")
    con.close()


if __name__ == "__main__":
    main()
