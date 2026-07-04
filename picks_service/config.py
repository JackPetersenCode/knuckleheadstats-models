"""Central config for the picks service.

Edit this file with your secrets and channel URLs before running anything.
"""
import os
from pathlib import Path

# === Database ===
PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))

# === Model artifacts ===
ROOT = Path(__file__).resolve().parent.parent
V5_MODEL = ROOT / "v5_seedavg_model.pkl"

# === Discord webhooks (one per channel) ===
# Set up via Server Settings > Integrations > Webhooks in your Discord server.
# Free tier: visible to everyone. VIP tier: gated to paid role.
DISCORD_FREE_WEBHOOK = os.environ.get("DISCORD_FREE_WEBHOOK", "")
DISCORD_VIP_WEBHOOK  = os.environ.get("DISCORD_VIP_WEBHOOK", "")

# === Affiliate links ===
# Replace these placeholder URLs with your actual affiliate links from each book.
# Sign up: DraftKings (impact.com), FanDuel (myaffiliates), BetMGM (myaffiliates),
# Caesars (impact.com), BetRivers (impact.com), Underdog Fantasy, PrizePicks.
# Most pay $100-$300 per funded new account.
AFFILIATE_LINKS = {
    "DraftKings": "https://sportsbook.draftkings.com/?wpcid=YOUR_ID",
    "FanDuel":    "https://sportsbook.fanduel.com/?CMP=YOUR_ID",
    "BetMGM":     "https://sports.betmgm.com/?wm=YOUR_ID",
    "Caesars":    "https://www.caesars.com/sportsbook?promo=YOUR_ID",
    "PrizePicks": "https://app.prizepicks.com/sign-up?invite_code=YOUR_ID",
}

# === Pick service settings ===
FREE_PICKS_PER_DAY = 2       # max picks posted to free channel
VIP_PICKS_PER_DAY  = 6       # max picks posted to VIP channel
FREE_EDGE_THRESHOLD = 0.08   # higher bar for free picks (more conservative)
VIP_EDGE_THRESHOLD  = 0.06   # lower bar — VIPs get more volume

# === Optional: Odds API for line-movement detection ===
# Free tier: 500 requests/month. Sign up at the-odds-api.com.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# === Disclaimers (always appended) ===
DISCLAIMER = (
    "21+ and present in legal jurisdictions only. Gambling problem? Call 1-800-GAMBLER. "
    "Picks are for entertainment; past performance does not guarantee future results."
)
