"""Post recent line_findings to Discord webhooks (free + pro tier).

Only alerts findings inserted within the last N minutes that haven't been
alerted yet (we track alerted state in line_findings.alerted_at).

Usage:
  python alert.py                     # once
  python alert.py --since-min 30      # consider findings from last 30 min
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG, DISCORD_WEBHOOK_FREE, DISCORD_WEBHOOK_PRO


def ensure_alerted_col(pg):
    with pg.cursor() as c:
        c.execute("""
            ALTER TABLE line_findings
              ADD COLUMN IF NOT EXISTS alerted_at timestamptz
        """)
    pg.commit()


def build_embed(findings, tier):
    color = 0xFFD700 if tier == "pro" else 0x4CAF50
    title = ("💎 LINE-SHOPPER PRO" if tier == "pro" else "🆓 LINE-SHOPPER FREE") + " — Live signals"
    fields = []
    for f in findings:
        if f["kind"] == "EV":
            name = (f"EV {f['edge_pct']:+.2f}pp · {f['sport'].split('_')[-1].upper()} · "
                    f"{f['side']} @ {f['book']}")
            val = (
                f"**Bet**: {f['side']} {('+'+str(f['price_usd'])) if f['price_usd']>0 else f['price_usd']}\n"
                f"**Game**: {f['away_team']} @ {f['home_team']}\n"
                f"**Sharp fair**: {float(f['ref_fair_pct']):.1f}%  "
                f"(Pinnacle ref price {('+'+str(f['price_other'])) if f['price_other']>0 else f['price_other']})\n"
                f"**Edge**: {float(f['edge_pct']):+.2f}pp above fair"
            )
        else:
            name = f"ARB {float(f['edge_pct']):+.2f}% · {f['sport'].split('_')[-1].upper()}"
            val = (
                f"**{f['away_team']} @ {f['home_team']}**\n"
                f"Bet both: {f['side']}\n"
                f"Lock {float(f['edge_pct']):+.2f}% risk-free (bankroll-split required)"
            )
        fields.append({"name": name[:256], "value": val[:1024], "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields[:25],  # discord limit
        "footer": {"text": "21+ · gamble responsibly · 1-800-GAMBLER"},
    }


def post(webhook, embed):
    if not webhook:
        print("  (no webhook URL set — skipped)")
        return False
    try:
        r = requests.post(webhook, json={"username": "LineShopper", "embeds": [embed]}, timeout=20)
        if r.status_code in (200, 204): return True
        print(f"  Discord error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  Discord exception: {e}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-min", type=int, default=30)
    args = ap.parse_args()

    pg = psycopg2.connect(**PG)
    ensure_alerted_col(pg)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.since_min)
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM line_findings
            WHERE found_at >= %s AND alerted_at IS NULL
            ORDER BY edge_pct DESC
        """, (cutoff,))
        new_findings = c.fetchall()
    if not new_findings:
        print("No new findings to alert.")
        pg.close(); return

    free_finds = [f for f in new_findings if f["tier"] == "free"][:2]
    pro_finds  = new_findings[:25]
    print(f"new findings: {len(new_findings)}  free: {len(free_finds)}  pro: {len(pro_finds)}")

    if free_finds:
        ok = post(DISCORD_WEBHOOK_FREE, build_embed(free_finds, "free"))
        print(f"  free posted: {ok}")
    if pro_finds:
        ok = post(DISCORD_WEBHOOK_PRO, build_embed(pro_finds, "pro"))
        print(f"  pro posted: {ok}")

    with pg.cursor() as c:
        c.execute(
            "UPDATE line_findings SET alerted_at = now() "
            "WHERE find_id = ANY(%s)",
            ([f["find_id"] for f in new_findings],))
    pg.commit()
    pg.close()


if __name__ == "__main__":
    main()
