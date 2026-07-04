"""Unified daily best-bets ranker. Pulls every +EV play logged today (props + game
markets, all sports), assigns a CONFIDENCE tier from the realized track record of
that (sport, market_type, bet_book) category, scores, ranks, and writes best_bets +
a markdown section. Honest by construction: only categories with proven forward CLV
get full weight; game markets are price-value; sharp-book props stay efficient.
"""
import datetime
import collections
import db

# tier -> weight on EV in the value score
WEIGHT = {"proven": 1.0, "edge": 0.7, "price": 0.4, "model": 0.4, "efficient": 0.15}

# Realized ROI on +EV plays is ~+10-16%, NOT the raw flagged EV (which can read 100%+
# from soft-book line mismatches). Cap EV so the board ranks by realistic value, not
# by the noisiest mismatched lines. The honest expected edge lives well below this.
EV_CAP = 0.25


def _category_weights(con):
    cur = con.cursor()
    cur.execute("""SELECT sport, market_type, bet_book, n_graded, roi, avg_clv, clv_pos_share
                   FROM value_category_stats""")
    out = {}
    for s, m, b, n, roi, clv, pos in cur.fetchall():
        out[(s, m, b)] = dict(n=n or 0, roi=float(roi) if roi is not None else None,
                              avg_clv=float(clv) if clv is not None else 0.0,
                              clv_pos=float(pos) if pos is not None else 0.0)
    return out


def _tier(sport, market_type, bet_book, anchor_book, cats):
    c = cats.get((sport, market_type, bet_book))
    if market_type == "prop" and c and c["n"] >= 40:
        if c["avg_clv"] > 0.03 and c["clv_pos"] > 0.55:
            return "proven"
        if c["avg_clv"] > 0:
            return "edge"
        return "efficient"            # enough data, no edge (e.g. sharp books)
    if market_type in ("h2h", "spread", "total"):
        return "price"                # price value only, validated forward by CLV
    if anchor_book == "model":
        return "model"                # projection-model research
    return "efficient"


def rank_today(con, date=None, max_bets=60, per_selection=2):
    date = date or datetime.date.today()
    cats = _category_weights(con)
    cur = con.cursor()
    cur.execute("""SELECT id, sport, market_type, player_name, stat_type, line, side, bet_book,
                          offered_mult, fair_prob, ev, anchor_book
                   FROM value_play WHERE game_date=%s AND recommended""", (date,))
    rows = cur.fetchall()
    scored = []
    for (vid, sport, mkt, sel, stat, line, side, book, mult, fair, ev, anchor) in rows:
        tier = _tier(sport, mkt, book, anchor, cats)
        c = cats.get((sport, mkt, book))
        ev_eff = min(float(ev), EV_CAP)          # realistic edge for ranking/display
        nudge = 0.3 * max(0.0, c["avg_clv"]) if (c and tier in ("proven", "edge")) else 0.0
        score = WEIGHT[tier] * ev_eff + nudge
        scored.append(dict(id=vid, sport=sport, market_type=mkt, selection=sel, stat_type=stat,
                           line=float(line) if line is not None else None, side=side, bet_book=book,
                           offered_dec=float(mult) if mult is not None else None,
                           fair_prob=float(fair) if fair is not None else None,
                           ev=float(ev), ev_eff=ev_eff, ev_capped=float(ev) > EV_CAP,
                           tier=tier, value_score=round(score, 4)))
    scored.sort(key=lambda x: -x["value_score"])

    # persist confidence + score on every scored play
    with con.cursor() as c2:
        for p in scored:
            c2.execute("UPDATE value_play SET confidence=%s, value_score=%s WHERE id=%s",
                       (p["tier"], p["value_score"], p["id"]))
    con.commit()

    # diversify (cap per sport+selection) for the surfaced board
    seen = collections.Counter(); board = []
    for p in scored:
        k = (p["sport"], p["selection"])
        if seen[k] >= per_selection:
            continue
        seen[k] += 1
        board.append(p)
        if len(board) >= max_bets:
            break

    rows_db = [dict(game_date=date, sport=p["sport"], market_type=p["market_type"],
                    selection=p["selection"], stat_type=p["stat_type"], line=p["line"],
                    side=p["side"], bet_book=p["bet_book"], offered_dec=p["offered_dec"],
                    fair_prob=p["fair_prob"], ev=p["ev"], confidence=p["tier"],
                    value_score=p["value_score"], rank=i + 1)
               for i, p in enumerate(board)]
    if rows_db:
        db.upsert(con, "best_bets", rows_db,
                  ["game_date", "sport", "market_type", "selection", "stat_type", "line", "side", "bet_book"])
        con.commit()
    print(f"ranker: {len(scored)} +EV plays scored, {len(board)} on today's board "
          f"({sum(1 for p in board if p['tier']=='proven')} proven)")
    return board


def markdown(board, top=25):
    if not board:
        return "## Today's Best Bets\n\n_No +EV plays found yet today._\n"
    icon = {"proven": "✅", "edge": "📈", "price": "💲", "model": "🔬", "efficient": "•"}
    lines = ["## Today's Best Bets (ranked by value)\n",
             "_Tiers: ✅ proven edge · 📈 positive-CLV · 💲 price value (game lines) · 🔬 model · • efficient_\n"]
    for i, p in enumerate(board[:top], 1):
        ln = "" if p["line"] is None else (f" {p['side'].title()} {p['line']:g}" if p["market_type"] != "h2h"
                                           else f" {p['side'].title()}")
        mkt = p["stat_type"] if p["market_type"] == "prop" else p["market_type"].upper()
        dec = f"{p['offered_dec']:.2f}" if p["offered_dec"] else "?"
        evtxt = f"{p['ev_eff']:+.0%}{'+' if p.get('ev_capped') else ''}"
        lines.append(f"{i}. {icon.get(p['tier'],'')} **{p['selection']}** {mkt}{ln} @ "
                     f"{p['bet_book']} ({dec}) — **{evtxt} EV**, {p['tier']}")
    return "\n".join(lines) + "\n"


def main(date=None):
    con = db.connect()
    board = rank_today(con, date)
    print("\n" + markdown(board))
    con.close()


if __name__ == "__main__":
    main()
