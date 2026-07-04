"""Generate a 1080x1350 IG-ready PNG of today's picks.

Output: ig_post_YYYY-MM-DD.png (in current directory)

Usage:
  python ig_image.py [YYYY-MM-DD]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG

W, H = 1080, 1350
BG = (13, 17, 23)
FG = (230, 237, 243)
ACCENT = (255, 215, 0)          # gold
GREEN = (46, 160, 67)
RED = (248, 81, 73)
MUTED = (110, 118, 129)
CARD_BG = (22, 27, 34)

# Try a few common font paths; fall back to default if none found
def load_font(size, bold=False):
    paths = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else r"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def fetch_today_picks(pg, d, tier="free"):
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT * FROM picks_published
            WHERE game_date = %s AND tier = %s
            ORDER BY edge_pct DESC
        """, (d, tier))
        return c.fetchall()


def fetch_yesterday_recap(pg, yesterday):
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            SELECT pick_side, home_team, away_team, ml_price,
                   settled_y, settled_pl, stake_units
            FROM picks_published
            WHERE game_date = %s AND settled_y IS NOT NULL
            ORDER BY pick_id
        """, (yesterday,))
        return c.fetchall()


def fetch_rolling_record(pg, days):
    with pg.cursor() as c:
        c.execute(f"""
            SELECT
              COUNT(*) FILTER (WHERE settled_y IS NOT NULL),
              COUNT(*) FILTER (WHERE settled_y = 1),
              COUNT(*) FILTER (WHERE settled_y = 0),
              COALESCE(SUM(settled_pl), 0)::float
            FROM picks_published
            WHERE game_date >= CURRENT_DATE - INTERVAL '{days} days'
              AND settled_y IS NOT NULL
        """)
        return c.fetchone()


def draw_text(d, xy, text, font, fill=FG, anchor="lt"):
    d.text(xy, text, font=font, fill=fill, anchor=anchor)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=str(date.today()))
    args = ap.parse_args()
    today = date.fromisoformat(args.date)
    yesterday = today - timedelta(days=1)

    pg = psycopg2.connect(**PG)
    picks = fetch_today_picks(pg, str(today))
    recap = fetch_yesterday_recap(pg, str(yesterday))
    rolling = fetch_rolling_record(pg, 30)
    pg.close()

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # === Header ===
    f_brand   = load_font(36, bold=True)
    f_h1      = load_font(56, bold=True)
    f_h2      = load_font(28, bold=True)
    f_pick    = load_font(44, bold=True)
    f_ml      = load_font(36, bold=True)
    f_meta    = load_font(22)
    f_small   = load_font(20)

    # Brand bar
    draw_text(d, (40, 36), "YOUR BRAND", f_brand, fill=ACCENT)
    draw_text(d, (W - 40, 50), str(today), f_meta, fill=MUTED, anchor="rt")

    # Headline
    draw_text(d, (W // 2, 130), "TODAY'S FREE PLAYS", f_h1, fill=FG, anchor="mt")

    # === Recap strip ===
    if rolling and rolling[0]:
        settled, wins, losses, profit = rolling
        rec_text = f"Last 30 days: {wins}-{losses}  ({profit:+.1f}u)"
        rec_color = GREEN if profit >= 0 else RED
        draw_text(d, (W // 2, 210), rec_text, f_meta, fill=rec_color, anchor="mt")

    # === Pick cards ===
    y = 280
    if not picks:
        draw_text(d, (W // 2, y + 100), "No qualifying picks today.", f_h2, fill=MUTED, anchor="mm")
        draw_text(d, (W // 2, y + 150), "Check Discord for VIP plays.", f_small, fill=MUTED, anchor="mm")
    else:
        for i, p in enumerate(picks[:4]):  # max 4 picks per image
            card_top = y + i * 200
            d.rounded_rectangle([40, card_top, W - 40, card_top + 170],
                                radius=16, fill=CARD_BG, outline=(48, 54, 61), width=1)
            team = p["home_team"] if p["pick_side"] == "HOME" else p["away_team"]
            ml = p["ml_price"]
            ml_str = f"+{ml}" if ml and ml > 0 else str(ml)
            draw_text(d, (70, card_top + 26),
                      f"{p['sport']} • {p['pick_side']}",
                      f_small, fill=ACCENT)
            draw_text(d, (70, card_top + 60), team[:28], f_pick, fill=FG)
            draw_text(d, (W - 70, card_top + 60), ml_str, f_ml, fill=GREEN, anchor="rt")
            draw_text(d, (70, card_top + 120),
                      f"{p['stake_units']}u  ·  edge {float(p['edge_pct'])*100:+.1f}pp  ·  {p['source']}",
                      f_small, fill=MUTED)

    # === Footer CTA ===
    cta_y = H - 200
    draw_text(d, (W // 2, cta_y), "Join free Discord — link in bio",
              f_h2, fill=ACCENT, anchor="mt")
    draw_text(d, (W // 2, cta_y + 50), "Every pick timestamped • Full record public",
              f_meta, fill=MUTED, anchor="mt")
    draw_text(d, (W // 2, cta_y + 110), "21+ • Gamble responsibly • 1-800-GAMBLER",
              f_small, fill=MUTED, anchor="mt")

    out = f"ig_post_{today}.png"
    img.save(out)
    print(f"Saved: {out}")
    print(f"  size: {W}x{H}, recommended IG feed format")


if __name__ == "__main__":
    main()
