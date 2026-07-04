# Line Shopper — Real-Time +EV & Arbitrage Scanner

## What it does

Polls 6+ sportsbooks every 5 minutes for current moneyline/spread/total prices.
Detects two kinds of opportunities:

1. **+EV bets**: A book offers a price whose implied probability is at least
   3 percentage points below the Pinnacle vig-free fair line. Pinnacle is the
   sharpest book in the world; consistent +EV vs Pinnacle = real edge.

2. **Arbitrage**: A pair of books prices the two sides of an event such that
   their combined implied probabilities are < 100%. Bet both sides at the right
   ratio → guaranteed profit regardless of outcome.

Alerts go to two Discord channels:
- **Free** channel: 1-2 best EV signals per day (acts as marketing hook)
- **Pro** channel: every signal in real time ($19.99/mo subscription)

## Setup

1. **Sign up for The Odds API**: https://the-odds-api.com
   - Free tier: 500 requests/month (enough for once-an-hour polling)
   - $30/mo: 100,000 requests (5-min polling continuous)
   - $59/mo: 1M requests (if you scale to multiple sports)

2. **Set environment variables**:
   ```powershell
   $env:ODDS_API_KEY = "your_key"
   $env:LINE_SHOPPER_DISCORD_FREE = "https://discord.com/api/webhooks/..."
   $env:LINE_SHOPPER_DISCORD_PRO  = "https://discord.com/api/webhooks/..."
   ```

3. **Verify Postgres connection**: edit `config.py` if your DB credentials differ.

## Usage

### Manual one-shot (testing)
```powershell
python poll.py     # snapshot current lines
python detect.py   # find +EV and arb in latest snapshot
python alert.py    # post any new findings to Discord
```

### Continuous (production)
Open one terminal and run the poller in loop mode:
```powershell
python poll.py --loop
```

In another terminal, run detect + alert in a cron-style loop (every 5 min):
```powershell
while ($true) {
  python detect.py
  python alert.py
  Start-Sleep -Seconds 300
}
```

Or set up Windows Task Scheduler tasks to run each script every 5 minutes.

## Discord subscription setup

In your Discord server → Server Settings → Server Subscription:
1. Enable monthly subscription at **$19.99/mo**
2. Create a "Pro" role that the subscription auto-grants
3. Create two channels:
   - `#line-shopper-free` (public) — webhook = `LINE_SHOPPER_DISCORD_FREE`
   - `#line-shopper-pro` (Pro role only) — webhook = `LINE_SHOPPER_DISCORD_PRO`

Discord takes ~10% + Stripe fees. You keep roughly $17.50 per subscriber-month.

## Realistic economics

| Stage | Subscribers | Monthly revenue | Monthly cost |
|---|---:|---:|---:|
| Month 1 | 0-10 | $0-$175 | $30 Odds API |
| Month 6 | 50-150 | $875-$2,625 | $30-60 |
| Month 12 | 200-500 | $3,500-$8,750 | $59 |
| Month 24 | 500-2,000 | $8,750-$35,000 | $99 |

Tool-product revenue is **stickier than picks subscriptions**. Customers stay
because they use it every day; they're not betting against your "win rate."

## Honest limitations

- **Latency**: Odds API has a ~30-60 second lag from books. Real arbitrage windows
  often close in <30 seconds. Treat ARB alerts as "decent opportunities" not
  "guaranteed locks."
- **Limits**: US books cut max-bet limits on sharp action fast. Pro users
  betting these signals at $1,000+ stakes will be limited within 50-100 bets at
  most retail books.
- **Coverage**: only events Pinnacle prices. Some niche markets (props,
  futures, alt lines) may not have Pinnacle data and so won't produce EV
  signals.

## Why this product wins long-term

- Zero predictive claims → zero FTC exposure
- Pure data tool — same legal status as a Bloomberg terminal
- Users compute their own bets — you don't deliver predictions
- Pricing comparable to OddsJam ($299/mo) but you're undercutting at $19.99
- Discord-native means no payment processor risk
