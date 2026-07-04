"""Detect +EV bets and arbitrage opportunities from line_snapshots.

Two strategies:

  1. +EV vs sharp reference (Pinnacle).
     Pinnacle is the sharpest book in MLB/NHL/NFL/NBA. We treat its vig-free
     two-way prices as the "true" probability of each outcome. If any other
     book offers a price whose implied probability is meaningfully BELOW
     Pinnacle's fair probability, that's a +EV bet.

  2. Two-book arbitrage.
     If book A prices the home team and book B prices the away team such that
     implied_A_home + implied_B_away < 1, you can bet both sides at the right
     stake ratio and lock in a guaranteed profit equal to (1 - sum) / sum.

This script reads the latest snapshot per (event, book, market, side), runs
both checks, and writes findings to `line_findings`.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PG, MIN_EV_PCT, MIN_ARB_MARGIN_PCT


def ensure_schema(pg):
    with pg.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS line_findings (
                find_id     bigserial PRIMARY KEY,
                found_at    timestamptz NOT NULL,
                kind        varchar(10) NOT NULL,    -- 'EV' or 'ARB'
                sport       varchar(40),
                event_id    varchar(64),
                commence    timestamptz,
                home_team   varchar(80),
                away_team   varchar(80),
                market      varchar(20),
                side        varchar(40),
                book        varchar(20),
                book_other  varchar(20),
                price_usd   integer,
                price_other integer,
                ref_fair_pct numeric,
                edge_pct    numeric,
                tier        varchar(8)               -- 'free' or 'pro'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_finding
              ON line_findings (kind, event_id, market, side, book, book_other);
        """)
    pg.commit()


def ml_to_prob(ml):
    if ml is None: return None
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def fetch_latest_lines(pg, since_seconds=600):
    """Latest line per (event, market, book, side) within the last N seconds."""
    with pg.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""
            WITH ranked AS (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY event_id, market, book, side
                ORDER BY snap_time DESC
              ) AS rn
              FROM line_snapshots
              WHERE snap_time > now() - make_interval(secs => %s)
            )
            SELECT * FROM ranked WHERE rn = 1
        """, (since_seconds,))
        return c.fetchall()


def group_by_event_market(lines):
    out = {}
    for r in lines:
        key = (r["event_id"], r["market"])
        out.setdefault(key, []).append(r)
    return out


def detect_ev(group):
    """For each (event, market), find +EV bets vs Pinnacle's vig-free fair price."""
    findings = []
    pin_lines = [l for l in group if l["book"] == "pinnacle"]
    if not pin_lines:
        return findings

    # Build pinnacle vig-free fair prob per side (for h2h, this is just
    # implied_a / (implied_a + implied_b); generalizes to spreads/totals same way
    # since each market has exactly 2 sides per outcome group).
    pin_by_point = {}
    for l in pin_lines:
        bucket = (l["side"], float(l["point"]) if l["point"] is not None else None)
        pin_by_point.setdefault(bucket[1], []).append(l)

    for point, side_lines in pin_by_point.items():
        if len(side_lines) != 2: continue
        a, b = side_lines
        pa = ml_to_prob(a["price_usd"]); pb = ml_to_prob(b["price_usd"])
        overround = pa + pb
        if overround <= 0: continue
        fair_a = pa / overround; fair_b = pb / overround
        fair_by_side = {a["side"]: fair_a, b["side"]: fair_b}

        # Now check every non-Pinnacle book's price on these sides
        for l in group:
            if l["book"] == "pinnacle": continue
            if (l["point"] or None) != point: continue
            side = l["side"]
            if side not in fair_by_side: continue
            offered_prob = ml_to_prob(l["price_usd"])
            fair = fair_by_side[side]
            if not offered_prob or not fair: continue
            edge = (fair - offered_prob) * 100.0  # in percentage points
            if edge < MIN_EV_PCT: continue
            findings.append(dict(
                kind="EV",
                event_id=l["event_id"], commence=l["commence"],
                home_team=l["home_team"], away_team=l["away_team"],
                market=l["market"], side=side,
                book=l["book"], book_other="pinnacle",
                price_usd=l["price_usd"], price_other=(a if a["side"]==side else b)["price_usd"],
                ref_fair_pct=round(fair*100, 2),
                edge_pct=round(edge, 2),
                sport=l["sport"],
            ))
    return findings


def detect_arb(group):
    """For h2h only: check if any pair of (book_A on home, book_B on away)
    gives implied_A + implied_B < 1, yielding guaranteed profit."""
    findings = []
    # group is all lines for one (event, market)
    if not group: return findings
    market = group[0]["market"]
    if market != "h2h": return findings  # only main MLs for v1

    # Build best price per side across books
    by_side = {}
    for l in group:
        if l["side"] in by_side:
            if l["price_usd"] > by_side[l["side"]]["price_usd"]:
                by_side[l["side"]] = l
        else:
            by_side[l["side"]] = l

    if len(by_side) != 2: return findings
    sides = list(by_side.items())
    s1, l1 = sides[0]; s2, l2 = sides[1]
    p1 = ml_to_prob(l1["price_usd"]); p2 = ml_to_prob(l2["price_usd"])
    if not p1 or not p2: return findings
    total = p1 + p2
    if total >= 1 - (MIN_ARB_MARGIN_PCT / 100): return findings

    margin = (1 - total) * 100
    findings.append(dict(
        kind="ARB",
        event_id=l1["event_id"], commence=l1["commence"],
        home_team=l1["home_team"], away_team=l1["away_team"],
        market="h2h", side=f"{s1} @ {l1['book']} + {s2} @ {l2['book']}",
        book=l1["book"], book_other=l2["book"],
        price_usd=l1["price_usd"], price_other=l2["price_usd"],
        ref_fair_pct=None, edge_pct=round(margin, 3),
        sport=l1["sport"],
    ))
    return findings


def main():
    pg = psycopg2.connect(**PG)
    ensure_schema(pg)
    lines = fetch_latest_lines(pg)
    print(f"Lines in window: {len(lines)}")
    groups = group_by_event_market(lines)
    print(f"Event-market groups: {len(groups)}")

    findings = []
    for key, group in groups.items():
        findings.extend(detect_ev(group))
        findings.extend(detect_arb(group))
    print(f"Findings: {len(findings)}")
    if not findings:
        pg.close(); return

    # Tier: pro gets all, free gets only the top 1-2 of the day
    findings.sort(key=lambda f: -f["edge_pct"])
    for i, f in enumerate(findings):
        f["tier"] = "free" if i < 2 and f["kind"] == "EV" else "pro"

    rows = [
        (datetime.now(timezone.utc), f["kind"], f["sport"], f["event_id"],
         f["commence"], f["home_team"], f["away_team"], f["market"], f["side"],
         f["book"], f["book_other"], f["price_usd"], f["price_other"],
         f.get("ref_fair_pct"), f["edge_pct"], f["tier"])
        for f in findings
    ]
    with pg.cursor() as c:
        execute_values(c,
            "INSERT INTO line_findings "
            "(found_at, kind, sport, event_id, commence, home_team, away_team, "
            "market, side, book, book_other, price_usd, price_other, "
            "ref_fair_pct, edge_pct, tier) VALUES %s "
            "ON CONFLICT (kind, event_id, market, side, book, book_other) DO NOTHING",
            rows)
    pg.commit()
    print(f"Inserted (deduped): up to {len(rows)} rows into line_findings")

    print("\nTop findings:")
    for f in findings[:8]:
        if f["kind"] == "EV":
            print(f"  [EV {f['edge_pct']:+.2f}pp]  {f['away_team']} @ {f['home_team']}  "
                  f"-> {f['side']} {f['price_usd']:+} at {f['book']}  "
                  f"(Pinnacle fair {f['ref_fair_pct']:.1f}%)")
        else:
            print(f"  [ARB {f['edge_pct']:+.2f}%]  {f['away_team']} @ {f['home_team']}  "
                  f"-> {f['side']}")
    pg.close()


if __name__ == "__main__":
    main()
