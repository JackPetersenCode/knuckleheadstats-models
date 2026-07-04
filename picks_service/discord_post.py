"""Post today's picks to Discord (free + VIP channels) via webhooks.

Usage:
  python discord_post.py [YYYY-MM-DD]

Required env vars (or hardcoded in config.py):
  DISCORD_FREE_WEBHOOK   - webhook URL for #free-picks
  DISCORD_VIP_WEBHOOK    - webhook URL for #vip-picks (paid role required)

Get webhook URLs from: Discord server -> Edit Channel -> Integrations -> Webhooks.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG, DISCORD_FREE_WEBHOOK, DISCORD_VIP_WEBHOOK, AFFILIATE_LINKS, DISCLAIMER


def fetch_picks(pg, game_date, tier):
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM picks_published
            WHERE game_date = %s AND tier = %s
            ORDER BY edge_pct DESC
        """, (game_date, tier))
        return c.fetchall()


def format_embed(picks, game_date, tier):
    """Build a Discord embed object — clean card style."""
    color = 0xFFD700 if tier == "vip" else 0x4CAF50
    title = ("🏆 VIP PICKS — " if tier == "vip" else "🆓 FREE PICKS — ") + str(game_date)
    fields = []
    for i, p in enumerate(picks, 1):
        team = p["home_team"] if p["pick_side"] == "HOME" else p["away_team"]
        ml = p["ml_price"]
        ml_str = f"+{ml}" if ml and ml > 0 else str(ml)
        field_value = (
            f"**Price**: {ml_str}\n"
            f"**Stake**: {p['stake_units']} unit{'s' if p['stake_units']!=1 else ''}\n"
            f"**Edge**: {float(p['edge_pct'])*100:+.1f}pp\n"
            f"_{p['rationale']}_"
        )
        fields.append({
            "name": f"{i}. {p['sport']} • {p['pick_side']} {team}",
            "value": field_value,
            "inline": False,
        })

    # Affiliate sportsbook links footer
    book_str = " • ".join(
        f"[{name}]({url})" for name, url in AFFILIATE_LINKS.items())
    fields.append({"name": "📒 Bet at", "value": book_str, "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def post_to_discord(webhook_url, payload):
    if not webhook_url:
        print("  (no webhook URL configured — skipped)")
        return False
    r = requests.post(webhook_url, json=payload, timeout=20)
    if r.status_code not in (200, 204):
        print(f"  ERROR {r.status_code}: {r.text[:200]}")
        return False
    print("  posted ok")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    args = ap.parse_args()

    pg = psycopg2.connect(**PG)
    for tier, webhook in [("free", DISCORD_FREE_WEBHOOK), ("vip", DISCORD_VIP_WEBHOOK)]:
        picks = fetch_picks(pg, args.date, tier)
        if not picks:
            print(f"No {tier} picks for {args.date}.")
            continue
        embed = format_embed(picks, args.date, tier)
        payload = {"username": "PicksBot", "embeds": [embed]}
        print(f"Posting {len(picks)} {tier} pick(s) for {args.date}...")
        post_to_discord(webhook, payload)
    pg.close()


if __name__ == "__main__":
    main()
