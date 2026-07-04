# Sports Betting Content Business — Repo Overview

A complete, legal-safe sports-betting content + tools business in code.

**Read first:** [`BUSINESS_PLAYBOOK.md`](BUSINESS_PLAYBOOK.md) — the operational
plan that ties everything together.

---

## Three things you'll actually sell

| Product | Price | Where it lives | Year-2 revenue share |
|---|---:|---|---:|
| **Sportsbook signup bonuses** (affiliate) | free | `landing_page/` | 40-50% |
| **Line-Shopper Pro** (SaaS tool) | $19.99/mo | `line_shopper/` | 30-50% |
| **VIP daily picks** | $5.99/mo | `picks_service/` | 5-15% |

The picks subscription is mainly a **lead-gen** for the first two. Most
revenue comes from affiliate kickbacks ($100-300 per funded signup) and the
tool subscription (sticky, low churn, no predictive claim).

---

## Directory layout

```
new_game/
│
├── BUSINESS_PLAYBOOK.md       ← read this first
├── README.md                  ← you are here
│
├── landing_page/              ← affiliate-driver static site (Netlify deploy)
│   ├── index.html
│   ├── style.css
│   ├── update_record.py       ← refreshes record.json from Postgres weekly
│   ├── record.json            ← public verified record (auto-generated)
│   └── record.csv             ← full audit log
│
├── line_shopper/              ← real-time +EV / arbitrage scanner ($19.99/mo product)
│   ├── config.py
│   ├── poll.py                ← polls Odds API every 5 min
│   ├── detect.py              ← finds +EV vs Pinnacle, finds arbs
│   ├── alert.py               ← Discord webhook publisher
│   └── README.md
│
├── picks_service/             ← free + VIP picks Discord ($5.99/mo)
│   ├── config.py
│   ├── aggregate_picks.py     ← multi-source pick aggregator
│   ├── discord_post.py        ← posts to free + VIP channels
│   ├── ig_caption.py          ← generates IG caption + image layout
│   ├── settler.py             ← post-game P&L from MLB API
│   ├── record.py              ← rolling W-L dashboard
│   └── PLAYBOOK.md            ← picks-service operational details
│
├── v5_seedavg_model.pkl       ← MLB home-dog prediction model (seed-averaged)
├── daily_picker.py            ← scores today's MLB games using the model
├── save_seedavg_model.py      ← weekly retrain script
└── (Postgres database `hoop_scoop` with all underlying data)
```

---

## Quickstart (first 24 hours)

```powershell
# 1. Apply to affiliate programs (do FIRST, takes 1-7 days for approval)
#    PrizePicks, Underdog, FanDuel, DraftKings (impact.com), Caesars, BetMGM

# 2. Form your LLC + get EIN

# 3. Sign up for The Odds API ($30/mo plan): https://the-odds-api.com

# 4. Set env vars
$env:ODDS_API_KEY = "your-key"
$env:DISCORD_FREE_WEBHOOK = "https://discord.com/api/webhooks/..."
$env:DISCORD_VIP_WEBHOOK  = "..."
$env:LINE_SHOPPER_DISCORD_FREE = "..."
$env:LINE_SHOPPER_DISCORD_PRO  = "..."

# 5. Edit affiliate URLs in:
#    - landing_page/index.html  (replace YOUR_ID placeholders)
#    - picks_service/config.py  (AFFILIATE_LINKS dict)

# 6. Deploy landing page to Netlify
#    Drop the `landing_page` folder on https://app.netlify.com/drop
#    Get a free .netlify.app URL; add to your IG bio.

# 7. Run the line shopper (in background, always on)
cd line_shopper
python poll.py --loop
# in another terminal:
while ($true) {
  python detect.py
  python alert.py
  Start-Sleep -Seconds 300
}

# 8. Daily MLB picks (during season)
cd c:\Users\jackp\Desktop\new_game
python daily_picker.py --odds today_odds.csv
cd picks_service
python aggregate_picks.py
python discord_post.py
python ig_caption.py   # output copy-pasted to IG app

# 9. After games end
python settler.py
cd ..\landing_page
python update_record.py
```

---

## Revenue trajectory (realistic)

| Month | IG followers | Free Discord | VIP subs | Pro subs | Monthly $ |
|---:|---:|---:|---:|---:|---:|
| 1 | 50-200 | 20-50 | 0 | 0 | $0-$200 |
| 3 | 500-1,500 | 100-300 | 5-15 | 5-20 | $300-$1k |
| 6 | 2k-5k | 500-1k | 30-80 | 30-100 | $1k-$3k |
| 12 | 5k-20k | 1k-3k | 100-250 | 80-200 | $3k-$10k |
| 24 | 20k-50k | 3k-10k | 200-500 | 200-800 | $8k-$30k |
| 36 | 50k-100k | 10k-25k | 500-1.5k | 800-2.5k | $20k-$80k |

The picks model has small expected ROI (~+1-2%) and isn't life-changing on its
own — but layered with affiliate revenue + tool revenue, the **content business
makes 30-100x more than the betting model.**

---

## Why this stack stays legal

| Component | Why it's safe |
|---|---|
| **Landing page** | Affiliate marketing with required disclosure — same legal status as Wirecutter |
| **Line-Shopper Pro** | Pure data tool, no predictive claim — same status as a Bloomberg terminal |
| **Free picks** | Real picks, timestamped, full record published, FTC-disclosure-compliant |
| **VIP picks ($5.99)** | Same as free, lower volume threshold; record.csv proves picks were timestamped before games |

If FTC subpoenas you, you can hand them `record.csv` and `picks_published`
table dumps that exactly match every public claim. That's the entire defense.

---

## Closing note

The honest play with this code: this is a side business that should earn
$10k-40k in year 1 and $50k-200k by year 3 if you stick with it.

It is not get-rich-quick. The competitors doing fraud earn faster in months
1-12 and lose everything by month 18-24. You'll outlast them.

For the legal-zone reasoning, see [`BUSINESS_PLAYBOOK.md`](BUSINESS_PLAYBOOK.md) → "Phase 5".
