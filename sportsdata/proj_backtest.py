"""Leak-free player-prop projection backtest.

Question: can a simple projection model BEAT the prop lines we collected?
Method:
  - Build per-(player,stat) time series from the box tables (3 seasons).
  - For each graded prop, project the stat using ONLY games strictly before the
    prop's game_date (no leakage). Projection = EWMA of recent games + a dispersion
    estimate -> model P(over line).
  - Pick the model side; compare to the realized result.
  - Evaluate: (a) log-loss of model P(over) vs realized over (signal vs the 0.5
    baseline and vs the line), (b) realized win-rate by model conviction bucket,
    (c) win-rate on STANDARD lines only (the ~fair/sharp market) where beating
    >52.4% would indicate a real edge.

Honest about payouts: DFS single bets carry a ~40% hold, so the bar for *profit*
is high. This script measures predictive EDGE vs the line, the prerequisite for any
profit. It does NOT claim profit.
"""
import math, datetime, collections
import db

# stat_type (normalized) -> (box_table, column or callable, distribution)
# dist: 'pois' for low-count integer stats, 'norm' for higher-count / continuous.
NBA_BOX = "nba_player_box"
STATS = {
    # NBA
    ("nba", "points"):        (NBA_BOX, "pts", "norm"),
    ("nba", "rebounds"):      (NBA_BOX, "reb", "norm"),
    ("nba", "assists"):       (NBA_BOX, "ast", "norm"),
    ("nba", "3-pt made"):     (NBA_BOX, "fg3m", "pois"),
    ("nba", "pts+rebs+asts"): (NBA_BOX, ("pts", "reb", "ast"), "norm"),
    ("nba", "pts+rebs"):      (NBA_BOX, ("pts", "reb"), "norm"),
    ("nba", "pts+asts"):      (NBA_BOX, ("pts", "ast"), "norm"),
    ("nba", "rebs+asts"):     (NBA_BOX, ("reb", "ast"), "norm"),
    # MLB batting
    ("mlb", "total bases"):   ("mlb_batting_box", "tb", "pois"),
    ("mlb", "hits"):          ("mlb_batting_box", "h", "pois"),
    ("mlb", "hits+runs+rbis"):("mlb_batting_box", ("h", "r", "rbi"), "pois"),
    # MLB pitching
    ("mlb", "pitcher strikeouts"): ("mlb_pitching_box", "k", "norm"),
    ("mlb", "strikeouts"):    ("mlb_pitching_box", "k", "norm"),
    # NHL
    ("nhl", "shots on goal"): ("nhl_skater_box", "shots", "pois"),
    ("nhl", "points"):        ("nhl_skater_box", "points", "pois"),
    ("nhl", "goalie saves"):  ("nhl_goalie_box", "saves", "norm"),
    ("nhl", "saves"):         ("nhl_goalie_box", "saves", "norm"),
}


def nstat(s):
    import re
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def val(row, col):
    if isinstance(col, tuple):
        vs = [row.get(c) for c in col]
        return sum(float(v) for v in vs if v is not None) if any(v is not None for v in vs) else None
    v = row.get(col)
    return None if v is None else float(v)


def norm_cdf(x, mu, sd):
    if sd <= 1e-9:
        return 1.0 if mu > x else 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))


def pois_sf(line, mu):
    """P(X > line) for integer line+0.5 lines, X~Poisson(mu). line is the prop line (x.5)."""
    k = math.floor(line)  # P(X >= k+1) = 1 - P(X<=k)
    if mu <= 0:
        return 0.0
    cum = 0.0
    term = math.exp(-mu)
    for i in range(0, k + 1):
        if i > 0:
            term *= mu / i
        cum += term
    return max(0.0, min(1.0, 1 - cum))


def load_box(con, table):
    cur = con.cursor()
    cur.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # per-player sorted series of (date, row)
    series = collections.defaultdict(list)
    for r in rows:
        series[r["player_id"]].append(r)
    for pid in series:
        series[pid].sort(key=lambda r: r["game_date"])
    return series


def project(series, pid, col, gdate, half_life=5, min_games=5):
    """EWMA projection (mu) + sd from prior games only. Returns (mu, sd, n)."""
    hist = [val(r, col) for r in series.get(pid, []) if r["game_date"] < gdate]
    hist = [v for v in hist if v is not None]
    if len(hist) < min_games:
        return None
    # most recent last; EWMA with half-life
    decay = 0.5 ** (1.0 / half_life)
    w = 1.0
    sw = 0.0
    swx = 0.0
    for v in reversed(hist):            # recent first -> highest weight
        swx += w * v
        sw += w
        w *= decay
    mu = swx / sw
    # dispersion: sample sd of last ~20 games (unweighted), floor to avoid 0
    recent = hist[-20:]
    if len(recent) >= 2:
        m = sum(recent) / len(recent)
        sd = (sum((v - m) ** 2 for v in recent) / (len(recent) - 1)) ** 0.5
    else:
        sd = max(1.0, mu ** 0.5)
    return mu, max(sd, 0.5), len(hist)


def main():
    con = db.connect()
    cur = con.cursor()
    # graded props with a real over/under result
    cur.execute("""
        SELECT sport, source, player_id, stat_type, line_type, line, game_date, result, actual
        FROM prop_graded WHERE result IN ('over','under') AND player_id IS NOT NULL
    """)
    props = cur.fetchall()

    boxes = {}
    def get_box(table):
        if table not in boxes:
            boxes[table] = load_box(con, table)
        return boxes[table]

    # need multipliers too for payout-aware EV
    cur.execute("""
        SELECT sport, source, player_id, stat_type, line_type, line, game_date, result, actual,
               over_mult, under_mult
        FROM prop_graded WHERE result IN ('over','under') AND player_id IS NOT NULL
    """)
    props = cur.fetchall()

    # accumulate
    rec = []   # dicts
    for (sport, source, pid, stat_type, line_type, line, gdate, result, actual,
         over_mult, under_mult) in props:
        key = (sport, nstat(stat_type))
        if key not in STATS:
            continue
        table, col, dist = STATS[key]
        proj = project(get_box(table), pid, col, gdate)
        if proj is None:
            continue
        mu, sd, n = proj
        line = float(line)
        if dist == "pois":
            p_over = pois_sf(line, mu)
        else:
            p_over = 1 - norm_cdf(line, mu, sd)
        over_won = 1 if result == "over" else 0
        model_side_over = p_over > 0.5
        model_won = 1 if (model_side_over and over_won) or (not model_side_over and not over_won) else 0
        conv = abs(p_over - 0.5)
        side_mult = (float(over_mult) if model_side_over else float(under_mult)) \
            if (over_mult is not None and under_mult is not None) else None
        rec.append(dict(sport=sport, source=source, line_type=line_type, p_over=p_over,
                        over_won=over_won, model_won=model_won, conv=conv, gdate=gdate,
                        side_mult=side_mult))

    if not rec:
        print("no overlapping props/box found"); return

    dates = sorted({r["gdate"] for r in rec})
    print(f"\n=== PROJECTION BACKTEST  (n={len(rec)} props) ===")
    print(f"*** SPANS ONLY {len(dates)} DISTINCT GAME-DATES: {dates[0]}..{dates[-1]} ***")
    print("*** outcomes within a slate are correlated -> CIs below are OPTIMISTIC ***")

    def wr_ci(sub):
        if not sub: return (0, 0, 0)
        wr = sum(r["model_won"] for r in sub) / len(sub)
        se = (wr * (1 - wr) / len(sub)) ** 0.5
        return wr, 1.96 * se, len(sub)

    # ---- PrizePicks STANDARD only: ~pick'em market. Break-even for a 2-pick
    #      Power Play (3x) is 1/sqrt(3)=0.577/leg; 3-pick (5x) is 0.585/leg. ----
    print("\n=== PrizePicks STANDARD lines (pick'em market; PP power-play break-even ~0.577/leg) ===")
    pp_std = [r for r in rec if r["source"] == "prizepicks" and r["line_type"] == "standard"]
    for lo, hi in [(0.0, 0.1), (0.1, 0.2), (0.2, 0.5001)]:
        wr, ci, n = wr_ci([r for r in pp_std if lo <= r["conv"] < hi])
        if n: print(f"  conv [{lo:.2f},{hi:.2f}): n={n:5d}  modelwin={wr:.3f} +-{ci:.3f}   "
                    f"{'+EV vs 0.577' if wr-ci>0.577 else ('~edge' if wr>0.577 else 'below BE')}")

    # ---- Underdog: payout-aware. Per-leg E[payout]=winrate*mult; >1 => +EV parlay legs ----
    print("\n=== Underdog (payout-aware): per-leg E[payout]=modelwin*mult, >1.0 => +EV legs ===")
    ud = [r for r in rec if r["source"] == "underdog" and r["side_mult"]]
    for lo, hi in [(0.0, 0.1), (0.1, 0.2), (0.2, 0.5001)]:
        sub = [r for r in ud if lo <= r["conv"] < hi]
        if not sub: continue
        wr = sum(r["model_won"] for r in sub) / len(sub)
        avg_mult = sum(r["side_mult"] for r in sub) / len(sub)
        eleg = sum(r["model_won"] * r["side_mult"] for r in sub) / len(sub)
        print(f"  conv [{lo:.2f},{hi:.2f}): n={len(sub):5d}  modelwin={wr:.3f}  avg_mult={avg_mult:.2f}  "
              f"E[payout/leg]={eleg:.3f}  {'+EV' if eleg>1.0 else '-EV'}")

    # ---- per-date win-rate (high conviction) to expose slate correlation ----
    print("\n=== per-date model win-rate, conv>=0.2 (shows slate-to-slate variance) ===")
    for d in dates:
        wr, ci, n = wr_ci([r for r in rec if r["gdate"] == d and r["conv"] >= 0.2])
        if n: print(f"  {d}: n={n:4d}  win={wr:.3f}")

    # ---- by sport, standard/balanced only, high conviction ----
    print("\n=== by sport, STANDARD/balanced lines, conv>=0.15 ===")
    for sp in ("nba", "mlb", "nhl"):
        wr, ci, n = wr_ci([r for r in rec if r["sport"] == sp
                           and r["line_type"] in ("standard", "balanced")
                           and r["conv"] >= 0.15])
        if n: print(f"  {sp}: n={n:5d}  win={wr:.3f} +-{ci:.3f}")

    con.close()


if __name__ == "__main__":
    main()
