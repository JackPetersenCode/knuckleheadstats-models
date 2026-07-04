"""Leak-free backtest of mlb_proj against graded MLB props.

Gate for production use: the matchup model must BEAT the naive batter-rate-only
baseline on RANKING (AUC / log loss for predicting the actual over/under outcome) —
the exact failure mode of the old EWMA projector ("compresses to the mean").

Rates are rebuilt once per game_date (strictly prior games only), then every prop on
that date is projected. Reports calibration, Brier/log-loss, AUC vs naive, and a
payout-aware ROI vs Sleeper multipliers.

Run:  python proj_mlb_backtest.py [max_per_date]
"""
import sys
import collections
import numpy as np

import db
import mlb_proj as M

STATS = {"hits", "total bases", "singles", "batter walks"}   # model-covered, non-low-conf


def _opp_pitcher_map(con):
    """(player_id, game_date) -> opposing probable pitcher_id (leak-free: known pre-game)."""
    cur = con.cursor()
    cur.execute("""
      SELECT b.player_id, b.game_date, b.is_home, gc.home_prob_pitcher_id, gc.away_prob_pitcher_id
      FROM mlb_batting_box b JOIN game g ON g.sport='mlb' AND g.game_id=b.game_id
      JOIN mlb_game_context gc ON gc.game_id=b.game_id
      WHERE g.season_type='regular' AND g.game_date >= '2026-05-01'""")
    out = {}
    for pid, gd, is_home, hpp, app in cur.fetchall():
        opp = app if is_home else hpp     # opponent's starter
        if opp:
            out[(pid, gd)] = str(opp)
    return out


def _brier_auc(probs, outcomes):
    p = np.array(probs); y = np.array(outcomes)
    brier = float(np.mean((p - y) ** 2))
    eps = 1e-9
    logloss = float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))
    # AUC via rank statistic
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        auc = float("nan")
    else:
        order = np.argsort(p); ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
        auc = (ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    return brier, logloss, auc


def main(max_per_date=400):
    con = db.connect()
    oppmap = _opp_pitcher_map(con)
    cur = con.cursor()
    cur.execute("""SELECT player_id, stat_type, line, actual, over_mult, under_mult, game_date
                   FROM prop_graded
                   WHERE sport='mlb' AND actual IS NOT NULL AND stat_type = ANY(%s)
                   ORDER BY game_date""", (list(STATS),))
    rows = cur.fetchall()
    by_date = collections.defaultdict(list)
    for r in rows:
        by_date[r[6]].append(r)

    M_p, N_p, Y, sl_roi_m, sl_roi_n = [], [], [], [], []
    n_used = 0
    for gd, props in sorted(by_date.items()):
        brates = M.batter_rates(con, gd)
        prates = M.pitcher_rates(con, gd)
        lg, _ = M.league_rates(con, gd)
        seen = 0
        for pid, stat, line, actual, om, um, _gd in props:
            if seen >= max_per_date:
                break
            bp = brates.get(str(pid)) or brates.get(pid)
            if not bp or bp[0] is None or bp[1] < 50:        # need a real sample
                continue
            brate, b_pa, mu = bp
            mu = min(max(mu, 3.0), 5.0)
            bshr = M._shrink(brate, b_pa, lg)
            # naive: batter-only (no pitcher), matchup: combine with opposing starter
            naive_perpa = bshr
            opp = oppmap.get((str(pid), gd))
            pp = prates.get(opp) if opp else None
            if pp and pp[1] >= 100:                          # opposing starter w/ real sample
                pr = M._pitcher_event_rates(pp[0], lg)
                match_perpa = M.matchup_pa_probs(bshr, pr, lg)
            else:
                match_perpa = bshr
            pm, _ = M.project_batter(match_perpa, mu, stat, float(line))
            pn, _ = M.project_batter(naive_perpa, mu, stat, float(line))
            if pm is None:
                continue
            y = 1 if float(actual) > float(line) else (0 if float(actual) < float(line) else None)
            if y is None:
                continue
            M_p.append(pm); N_p.append(pn); Y.append(y)
            # ROI vs Sleeper: bet each model's favored side (by EV)
            if om and um:
                if pm * float(om) >= (1 - pm) * float(um):
                    sl_roi_m.append((float(om) - 1) if y == 1 else -1)
                else:
                    sl_roi_m.append((float(um) - 1) if y == 0 else -1)
                if pn * float(om) >= (1 - pn) * float(um):
                    sl_roi_n.append((float(om) - 1) if y == 1 else -1)
                else:
                    sl_roi_n.append((float(um) - 1) if y == 0 else -1)
            seen += 1; n_used += 1

    print(f"backtest: {n_used} props over {len(by_date)} dates\n")
    mb, ml, ma = _brier_auc(M_p, Y)
    nb, nl, na = _brier_auc(N_p, Y)
    base = np.mean(Y)
    print(f"  base over-rate          {base:.3f}")
    print(f"  {'':18}{'Brier':>8}{'LogLoss':>9}{'AUC':>7}")
    print(f"  naive (batter-only) {nb:8.4f}{nl:9.4f}{na:7.3f}")
    print(f"  matchup model       {mb:8.4f}{ml:9.4f}{ma:7.3f}")
    print(f"  improvement         {nb-mb:+8.4f}{nl-ml:+9.4f}{ma-na:+7.3f}  (positive = model better)")
    if sl_roi_m:
        print(f"\n  Sleeper ROI  naive {np.mean(sl_roi_n):+.1%}   matchup {np.mean(sl_roi_m):+.1%}  "
              f"(n={len(sl_roi_m)})")
    # ---- gate verdict ----
    beats_naive = (ma - na) > 0.005 and (nl - ml) > 0.002
    roi_ok = bool(sl_roi_m) and np.mean(sl_roi_m) > 0
    print("\n  VERDICT:", "INTEGRATE (beats naive AND +ROI vs Sleeper)" if (beats_naive and roi_ok)
          else "DO NOT INTEGRATE — calibrated but no betting edge vs the sharp-anchored "
               "market approach. Value engine stays on market-based anchors (sgo_fair).")
    # calibration (matchup)
    print("\n  matchup calibration:")
    P = np.array(M_p); Yy = np.array(Y)
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        m = (P >= lo) & (P < lo + 0.2)
        if m.sum():
            print(f"    P[{lo:.1f},{lo+0.2:.1f})  n={m.sum():5d}  pred={P[m].mean():.3f}  actual={Yy[m].mean():.3f}")
    con.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
