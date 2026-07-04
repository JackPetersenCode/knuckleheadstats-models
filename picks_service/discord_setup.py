"""One-time Discord server setup helper.

Run this once after creating your Discord server + bot to:
  1. Send welcome/rules messages to #welcome
  2. Post the pinned "How this works" message to #free-picks
  3. Verify your webhook configs work

Discord setup steps (do these manually first):
  1. Create server: "YourBrand Picks"
  2. Create channels: #welcome, #free-picks, #vip-picks, #line-shopper-free,
     #line-shopper-pro, #general, #bet-tracking
  3. For each pick channel: Edit Channel -> Integrations -> Create Webhook
     -> copy webhook URL.
  4. Server Settings -> Server Subscription -> Enable -> set $5.99/mo for VIP role
  5. (Later) $19.99/mo for Pro role

This script then drops the welcome content using your free-picks webhook.
"""
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DISCORD_FREE_WEBHOOK


# ============ Messages to post ============

WELCOME_MSG = {
    "username": "PicksBot",
    "embeds": [{
        "title": "👋 Welcome to YourBrand Picks",
        "description": (
            "**This is a free MLB picks community.** Every pick is timestamped "
            "before games start, every result is published, every loss is "
            "tracked alongside wins.\n\n"
            "**What to expect:**\n"
            "• 1-2 free MLB picks per day in `#free-picks`\n"
            "• Plays drop around 11am ET\n"
            "• Full audit log: `record.csv` (link in our IG bio)\n"
            "• VIP tier coming soon — founding-member pricing $4.99/mo\n\n"
            "**Rules:**\n"
            "• No spam, no DM-spamming other members\n"
            "• Be respectful in `#general`\n"
            "• No promoting other picks services\n"
            "• 21+ only\n\n"
            "**Important:** Past results don't guarantee future performance. "
            "We aim for modest, consistent edge — not get-rich-quick. If you "
            "can't sit through cold streaks (which WILL happen), this isn't "
            "for you.\n\n"
            "Gambling problem? Call 1-800-GAMBLER."
        ),
        "color": 0xFFD700,
        "footer": {"text": "21+ • Bet responsibly • All picks publicly tracked"},
    }],
}


HOW_IT_WORKS_MSG = {
    "username": "PicksBot",
    "embeds": [{
        "title": "📊 How the picks work",
        "description": (
            "**The model**\n"
            "Picks come from a machine-learning model trained on 11,000+ MLB "
            "games (2021-2025). It's a 3-model ensemble (logistic regression "
            "+ XGBoost + LightGBM), averaged across 3 random seeds.\n\n"
            "**The strategy**\n"
            "We only bet HOME UNDERDOGS where the model's win probability "
            "is at least 6 percentage points above the market's fair line. "
            "About 1-2 plays per day on average.\n\n"
            "**Why home dogs?**\n"
            "Public bettors love road favorites. Books shade the line to "
            "balance, which creates value on home dogs. Our model only bets "
            "when it ALSO has an ML-detected edge — not blind dog betting.\n\n"
            "**Honest expectations**\n"
            "• Expected win rate: ~43%\n"
            "• Expected ROI: +1% to +2.5% per bet\n"
            "• Variance is high. Losing streaks of 5-8 are normal.\n"
            "• Don't bet more than 1-2% of bankroll per pick.\n\n"
            "**The verified record**\n"
            "Every pick → `record.csv` → linked in our IG bio. Compare ours "
            "to any other picks account.\n\n"
            "Questions? Ping me in `#general`."
        ),
        "color": 0x58A6FF,
    }],
}


# ============ Run ============

def post(message_payload):
    if not DISCORD_FREE_WEBHOOK:
        print("ERROR: DISCORD_FREE_WEBHOOK env var not set.")
        print("Set it to your #free-picks (or #welcome) channel webhook URL.")
        sys.exit(1)
    r = requests.post(DISCORD_FREE_WEBHOOK, json=message_payload, timeout=20)
    if r.status_code in (200, 204):
        print(f"  ✓ posted (HTTP {r.status_code})")
        return True
    print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}")
    return False


def main():
    print("Posting Discord welcome content...\n")

    print("1. Welcome message")
    post(WELCOME_MSG)
    time.sleep(2)

    print("2. How-it-works pinned message")
    post(HOW_IT_WORKS_MSG)
    time.sleep(2)

    print("\nDone.")
    print("\nTODO manually in Discord (web/desktop client):")
    print("  1. Right-click each message → Pin to Channel")
    print("  2. Server Settings → Server Subscription → enable $5.99/mo VIP role")
    print("  3. Edit channel permissions: gate #vip-picks to VIP role")


if __name__ == "__main__":
    main()
