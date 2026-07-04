"""Generate the daily 'best value' post — the audience/affiliate content layer.

Turns the line-shopping + +EV-vs-sharp engines into a ready-to-post writeup with
affiliate routing. Two honest sections:
  1. BEST PRICES  — same prop, who pays most (save money) -> safe, always-available
  2. +EV vs SHARP — books pricing above consensus fair value (label as value, not locks)
No "guaranteed picks" framing — value + price, the only honest + durable angle.

Output: content/value_post_YYYY-MM-DD.md (copy into IG caption/story, X, Discord).
Run after the daily SGO pull. Affiliate URLs come from affiliate_links.py.
"""
import os, datetime
import db
import best_line as B
import affiliate_links as A


def dec_to_am(d):
    d = float(d)
    return f"+{round((d-1)*100)}" if d >= 2 else f"-{round(100/(d-1))}"


def _route(book):
    nm = A.name(book); url = A.link(book)
    return f"{nm} ({url})" if url else nm


_TIER_ICON = {"proven": "✅", "edge": "📈", "price": "💲", "model": "🔬", "efficient": "•"}


def _best_bets_section(con, date, top=15):
    cur = con.cursor()
    cur.execute("""SELECT sport, market_type, selection, stat_type, line, side, bet_book,
                          offered_dec, ev, confidence
                   FROM best_bets WHERE game_date=%s ORDER BY rank LIMIT %s""", (date, top))
    rows = cur.fetchall()
    if not rows:
        return "## 🏆 Today's Best Bets\n\n_No +EV plays surfaced yet today._"
    out = ["## 🏆 Today's Best Bets (ranked by value, all sports & bet types)",
           "_✅ proven edge · 📈 positive-CLV · 💲 game-line price value · 🔬 model. EV capped at 25% for honesty._", ""]
    for i, (sp, mt, sel, stat, line, side, book, dec, ev, conf) in enumerate(rows, 1):
        ev_eff = min(float(ev), 0.25); cap = "+" if float(ev) > 0.25 else ""
        mkt = stat if mt == "prop" else mt.upper()
        sidetxt = "" if line is None else (f" {side.title()}" if mt == "h2h" else f" {side.title()} {float(line):g}")
        out.append(f"{i}. {_TIER_ICON.get(conf,'')} **{sel}** ({sp.upper()}) {mkt}{sidetxt} @ "
                   f"{_route(book)} `{dec_to_am(dec)}` — **{ev_eff:+.0%}{cap} EV** · {conf}")
    return "\n".join(out)


def build(con, date=None, max_shop=10, max_ev=6, max_per_stat=2):
    date = date or datetime.date.today()
    shop = B.shop_plays(con)
    ev = B.book_ev_plays(con, min_ev=0.03)

    L = []
    L.append(f"# 🎯 Daily Value — {date:%b %d, %Y}")
    L.append("")
    L.append("We don't sell picks. We find you the **best price** and flag where the "
             "market is **mispriced** — so your bankroll goes further. 21+. Bet responsibly.")
    L.append("")

    # ranked best-bets board (all sports + bet types), from the best_bets table
    L.append(_best_bets_section(con, date))
    L.append("")

    def shop_line(p):
        return (f"- **{p['player_name']}** {p['side'].title()} {p['line']:g} "
                f"{p['stat_type']} — best at **{_route(p['best_book'])}** "
                f"`{dec_to_am(p['best_dec'])}` (vs `{dec_to_am(p['worst_dec'])}` elsewhere, "
                f"+{p['edge']*100:.0f}% better payout)")

    # headline: diversified across stat types (cap per_stat) for broad appeal
    L.append("## 💰 Best Prices Today (shop the line, win more on the same bet)")
    if not shop:
        L.append("_No standout price gaps right now — lines are tight today._")
    per_stat, headline = {}, []
    for p in shop:                         # shop is sorted by edge desc
        if per_stat.get(p["stat_type"], 0) >= max_per_stat:
            continue
        per_stat[p["stat_type"]] = per_stat.get(p["stat_type"], 0) + 1
        headline.append(p)
        if len(headline) >= max_shop:
            break
    for p in headline:
        L.append(shop_line(p))
    L.append("")

    # full ranked list: EVERY qualifying play (incl. all doubles) so nothing is lost
    if len(shop) > len(headline):
        L.append(f"<details><summary>Full best-price list — all {len(shop)} plays "
                 f"(highest value first)</summary>")
        L.append("")
        for p in shop:
            L.append(shop_line(p))
        L.append("")
        L.append("</details>")
        L.append("")

    L.append("## 💎 +EV vs Sharp Market (books pricing above fair value)")
    L.append("_Candidates flagged by our model vs the consensus fair line — value, not "
             "guarantees. Tracked publicly on CLV._")
    if not ev:
        L.append("_None clearing our threshold today._")
    for p in ev[:max_ev]:
        L.append(f"- **{p['player_name']}** {p['side'].title()} {p['line']:g} "
                 f"{p['stat_type']} @ **{_route(p['bet_book'])}** "
                 f"`{dec_to_am(p['offered_mult'])}` — +{p['ev']*100:.1f}% EV")
    L.append("")
    L.append("---")
    L.append("_Lines move fast — prices shown at generation time. Full CLV-graded "
             "record updated daily._")
    txt = "\n".join(L)

    outdir = os.path.join(os.path.dirname(__file__), "..", "content")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"value_post_{date:%Y-%m-%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"daily_post: {len(shop)} price plays, {len(ev)} +EV plays -> {os.path.abspath(path)}")
    return txt


if __name__ == "__main__":
    con = db.connect()
    build(con)   # writes the file + prints a summary line (avoids console emoji errors)
    con.close()
