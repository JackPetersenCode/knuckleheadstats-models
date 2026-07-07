"""Post today's board to Discord (free + VIP channels) via webhooks.

Reads the LIVE value engine (sportsedge best_bets + value_play) — NOT the old
hoop_scoop.picks_published. Free channel = a curated set of the strongest (proven-
edge) plays; VIP channel = the full ranked board.

Guarded to post ONCE per day (after POST_AFTER_HOUR local) so it can be called from
the every-2h odds cycle without spamming. Pass --force to post immediately.

Setup: put your channel webhook URLs in env vars, e.g.
  setx DISCORD_FREE_WEBHOOK "https://discord.com/api/webhooks/...."
  setx DISCORD_VIP_WEBHOOK  "https://discord.com/api/webhooks/...."
(Discord: Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy URL.)

Usage:  python discord_post.py [--force] [--date YYYY-MM-DD]
"""
import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DISCORD_FREE_WEBHOOK, DISCORD_VIP_WEBHOOK, DISCLAIMER

SE_PG = dict(host=os.environ.get("SPORTSEDGE_PGHOST", "localhost"),
             user=os.environ.get("SPORTSEDGE_PGUSER", "postgres"),
             dbname=os.environ.get("SPORTSEDGE_PGDB", "sportsedge"),
             password=(os.environ.get("SPORTSEDGE_PGPASS") or os.environ.get("PGPASSWORD")))

POST_AFTER_HOUR = 9          # wait until morning lines are set (local time)
FREE_LIMIT = 6               # curated strongest plays for the free channel
VIP_LIMIT = 20              # full board for VIP
SITE = "https://picks.knuckleheadstats.com"
MARKER = Path(__file__).resolve().parent / ".discord_posted"
TIER_ICON = {"proven": "✅", "edge": "📈", "price": "💲", "model": "🔬", "efficient": "➖"}
BOOK = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
        "espnbet": "ESPN BET", "caesars": "Caesars", "sleeper": "Sleeper", "bovada": "Bovada"}


def dec_to_am(d):
    if not d:
        return ""
    d = float(d)
    return f"+{round((d-1)*100)}" if d >= 2 else f"-{round(100/(d-1))}"


def fetch_board(cur, day):
    cur.execute("""SELECT rank, sport, market_type, selection, stat_type, line, side,
                          bet_book, offered_dec, ev, confidence
                   FROM best_bets WHERE game_date=%s ORDER BY rank""", (day,))
    out = []
    for rk, sport, mt, sel, stat, line, side, book, dec, ev, conf in cur.fetchall():
        market = stat if mt == "prop" else mt.upper()
        if line is not None and mt != "h2h":
            market += f" {float(line):g}"
        out.append(dict(rank=rk, sport=(sport or "").upper(), sel=sel, market=market,
                        side=(side or "").title(), book=BOOK.get(book, (book or "").title()),
                        odds=dec_to_am(dec), ev=min(float(ev or 0), 0.25),
                        capped=float(ev or 0) > 0.25, tier=conf))
    return out


def record_line(cur):
    cur.execute("""SELECT COUNT(*) FILTER (WHERE result IN ('win','loss')) n,
                          COUNT(*) FILTER (WHERE result='win') w,
                          COUNT(*) FILTER (WHERE result='loss') l,
                          SUM(CASE result WHEN 'win' THEN offered_mult-1 WHEN 'loss' THEN -1 ELSE 0 END)
                            /NULLIF(COUNT(*) FILTER (WHERE result IN ('win','loss')),0) roi,
                          AVG((clv>0)::int) clvpos
                   FROM value_play WHERE recommended AND result IS NOT NULL""")
    n, w, l, roi, clvpos = cur.fetchone()
    if not n:
        return None
    return (f"📊 **Verified record:** {w}-{l} · **{float(roi):+.1%} ROI** · "
            f"beat the close **{float(clvpos):.0%}** ({n:,} graded)")


def line(p):
    ic = TIER_ICON.get(p["tier"], "")
    ev = f"{p['ev']:+.0%}{'+' if p['capped'] else ''}"
    return f"{ic} **{p['rank']}. {p['sel']}** ({p['sport']}) · {p['market']} {p['side']} · {p['book']} `{p['odds']}` · **{ev} EV**"


def build_embed(picks, day, tier, rec):
    label = day.strftime("%a, %b %d") if hasattr(day, "strftime") else str(day)
    top = ("💎 VIP Board — " if tier == "vip" else "🆓 Free Picks — ") + label
    desc = []
    if rec:
        desc.append(rec + "\n")
    desc += [line(p) for p in picks]
    desc.append(f"\n_Ranked by value • tiers reflect live CLV track record • full board & audit at {SITE}_")
    return {"title": top, "color": 0xFFD700 if tier == "vip" else 0x3FB950,
            "description": "\n".join(desc)[:4000], "footer": {"text": DISCLAIMER}}


def post(webhook, embed, tier):
    if not webhook:
        print(f"  ({tier}: no webhook set — skipped)")
        return False
    r = requests.post(webhook, json={"username": "Knucklehead Picks", "embeds": [embed]}, timeout=20)
    ok = r.status_code in (200, 204)
    print(f"  {tier}: {'posted ok' if ok else 'ERROR '+str(r.status_code)+' '+r.text[:150]}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="post now, ignore once/day + hour guards")
    ap.add_argument("--date")
    args = ap.parse_args()

    if not args.force:
        if MARKER.exists() and MARKER.read_text().strip() == str(date.today()):
            print("discord_post: already posted today — skip"); return
        if datetime.now().hour < POST_AFTER_HOUR:
            print(f"discord_post: before {POST_AFTER_HOUR}:00, lines not set — skip"); return

    con = psycopg2.connect(**SE_PG)
    cur = con.cursor()
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        cur.execute("SELECT MAX(game_date) FROM best_bets"); day = cur.fetchone()[0]
    if not day:
        print("discord_post: no board found"); con.close(); return
    board = fetch_board(cur, day)
    rec = record_line(cur)
    con.close()
    if not board:
        print("discord_post: empty board — skip"); return

    free = [p for p in board if p["tier"] in ("proven", "edge")][:FREE_LIMIT] or board[:3]
    posted = post(DISCORD_FREE_WEBHOOK, build_embed(free, day, "free", rec), "free")
    post(DISCORD_VIP_WEBHOOK, build_embed(board[:VIP_LIMIT], day, "vip", rec), "vip")
    if posted and not args.force:
        MARKER.write_text(str(date.today()))


if __name__ == "__main__":
    main()
