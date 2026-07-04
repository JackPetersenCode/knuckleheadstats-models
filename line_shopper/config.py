"""Line-shopper config. Edit before running."""
import os
from pathlib import Path

# === Odds API ===
# Sign up: https://the-odds-api.com (free tier = 500 req/mo, $30/mo = 100k req)
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"

# Sports to scan. Add/remove based on season + your audience.
# Full list: https://the-odds-api.com/sports-odds-data/sports-apis.html
SPORTS = [
    "baseball_mlb",
    # "icehockey_nhl",     # uncomment in season
    # "americanfootball_nfl",
    # "basketball_nba",
]

# Books to query. Pinnacle is the "sharp" reference (low vig, fastest line).
# US books are the ones your subscribers will actually bet at.
BOOKMAKERS = [
    "pinnacle",      # sharp reference
    "draftkings",    # US retail
    "fanduel",
    "betmgm",
    "caesars",
    "betrivers",
    "espnbet",
]

# Markets to scan. Moneyline (h2h) is most efficient; spreads/totals less so.
MARKETS = ["h2h", "spreads", "totals"]

# === Alert thresholds ===
# +EV bet: market price's implied prob is > 3% below the Pinnacle vig-free fair prob.
MIN_EV_PCT = 3.0     # only alert on edges >= this %
# Arbitrage: combined implied probabilities across two books < 100 - MIN_ARB_MARGIN_PCT
MIN_ARB_MARGIN_PCT = 0.3   # 0.3% guaranteed return after vig

# === Output: where to send alerts ===
DISCORD_WEBHOOK_FREE = os.environ.get("LINE_SHOPPER_DISCORD_FREE", "")
DISCORD_WEBHOOK_PRO  = os.environ.get("LINE_SHOPPER_DISCORD_PRO", "")

# === DB for caching + dedupe ===
PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))

# === Polling interval ===
POLL_INTERVAL_SECONDS = 300   # 5 min, ~12 polls/hr * 14 hr active = ~170/day
