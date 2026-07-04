"""Generate Instagram caption text for today's picks + yesterday's recap.

Instagram doesn't support API posting for non-business accounts in 2026, so
this script outputs a copy-paste-ready block: caption text, hashtags, and a
suggested image layout. You paste it into the IG app to post.

Usage:
  python ig_caption.py [YYYY-MM-DD]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG, AFFILIATE_LINKS, DISCLAIMER


def fetch_picks(pg, d, tier="free"):
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM picks_published WHERE game_date = %s AND tier = %s
            ORDER BY edge_pct DESC
        """, (d, tier))
        return c.fetchall()


def yesterday_recap(pg, yesterday):
    """Pull yesterday's settled picks for the recap."""
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT pick_id, sport, pick_side, home_team, away_team,
                   ml_price, settled_y, settled_pl, stake_units
            FROM picks_published
            WHERE game_date = %s AND settled_y IS NOT NULL
            ORDER BY pick_id
        """, (yesterday,))
        return c.fetchall()


def rolling_record(pg, days=30):
    """Last N days W-L-Profit for marketing copy.

    NOTE: this uses only settled picks. Honest, no cherry-picking.
    """
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT
              COUNT(*) FILTER (WHERE settled_y IS NOT NULL)               AS settled,
              COUNT(*) FILTER (WHERE settled_y = 1)                       AS wins,
              COUNT(*) FILTER (WHERE settled_y = 0)                       AS losses,
              COALESCE(SUM(settled_pl), 0)::numeric                       AS profit,
              COALESCE(SUM(stake_units), 0)::numeric                      AS risked
            FROM picks_published
            WHERE game_date >= CURRENT_DATE - INTERVAL '%s days'
              AND settled_y IS NOT NULL
        """, (days,))
        return c.fetchone()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    args = ap.parse_args()
    today = args.date
    yesterday = str(date.fromisoformat(today) - timedelta(days=1))

    pg = psycopg2.connect(**PG)

    rec = yesterday_recap(pg, yesterday)
    rolling = rolling_record(pg)
    picks_today = fetch_picks(pg, today, "free")

    out = []
    out.append("=" * 60)
    out.append(f"INSTAGRAM POST — {today}")
    out.append("=" * 60)
    out.append("")

    # ============ Caption text ============
    out.append("CAPTION:")
    out.append("─" * 60)
    if rec:
        wins = sum(1 for r in rec if r["settled_y"] == 1)
        losses = len(rec) - wins
        pl = sum(float(r["settled_pl"] or 0) for r in rec)
        out.append(f"📊 Yesterday: {wins}-{losses} ({pl:+.1f}u)")
        out.append("")

    if rolling and rolling["settled"]:
        wpct = 100 * rolling["wins"] / rolling["settled"]
        roi  = 100 * float(rolling["profit"]) / max(float(rolling["risked"]) * 100, 1)
        out.append(f"🔥 Last 30 days: {rolling['wins']}-{rolling['losses']} "
                   f"({wpct:.0f}% W) {float(rolling['profit']):+.1f}u")
        out.append("")

    if picks_today:
        out.append(f"🎯 TODAY'S FREE PLAYS ({len(picks_today)}):")
        for i, p in enumerate(picks_today, 1):
            team = p["home_team"] if p["pick_side"] == "HOME" else p["away_team"]
            ml = p["ml_price"]
            ml_str = f"+{ml}" if ml and ml > 0 else str(ml)
            out.append(f"{i}. {p['sport']} {p['pick_side']}: {team} ({ml_str}) — {p['stake_units']}u")
        out.append("")
        out.append("💎 VIP plays in Discord — link in bio ($5.99/mo)")
        out.append("")

    out.append("📒 Best book for these plays: " +
               list(AFFILIATE_LINKS.keys())[0] +
               " (sign-up link in bio for $100+ free bets)")
    out.append("")
    out.append("─" * 60)
    out.append("HASHTAGS (paste at bottom or first comment):")
    out.append("─" * 60)
    hashtags = (
        "#sportsbetting #picksoftheday #mlbpicks #freepicks "
        "#sportsbettingtips #parlay #fadethepublic #sharps "
        "#bettingsystem #dailypicks #lockoftheday #sportstrader "
        "#draftkings #fanduel"
    )
    out.append(hashtags)
    out.append("")

    # ============ Image layout suggestion ============
    out.append("─" * 60)
    out.append("IMAGE LAYOUT SUGGESTION (build in Canva or similar):")
    out.append("─" * 60)
    out.append("Format: 1080x1350 (vertical, IG feed-optimized)")
    out.append("Top:    Your logo + date")
    out.append("Middle: One pick per card, stacked. Big team name, price.")
    out.append("Bottom: 'Get VIP picks: link in bio | $5.99/mo'")
    out.append("Color:  Use your brand color; green for picks, gold for VIP CTA")
    out.append("")

    # ============ Disclaimer ============
    out.append("─" * 60)
    out.append("DISCLAIMER (always include in caption):")
    out.append("─" * 60)
    out.append(DISCLAIMER)
    out.append("")

    print("\n".join(out))
    pg.close()


if __name__ == "__main__":
    main()
