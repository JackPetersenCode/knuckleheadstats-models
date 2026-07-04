"""Verify the home-underdog finding from filter_strategy_test.py:

In pooled OOF (raw ensemble), home underdogs at thr=0.06 showed +5.3% ROI on
545 bets with P(true ROI <= 0) = 0.149. This is the strongest slice ever found.

Sanity checks before believing it:
  1. Re-run with PROPER percentage formatting (the earlier output was confusing).
  2. Decompose by YEAR (2022, 2023, 2024, 2025). Does each year individually
     show positive ROI? If yes → real signal. If one year carries everything → noise.
  3. Bootstrap with 20,000 iterations.
  4. Compare to "home underdog" baseline without ANY model filter — is the model
     adding value or is the segment itself profitable?
"""
import os
import numpy as np
import pandas as pd
import psycopg2
import xgboost as xgb
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
STAKE = 100.0
RNG = np.random.default_rng(42)


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def ml_payout(ml, won):
    if not won: return -STAKE
    return STAKE*(ml/100.0) if ml > 0 else STAKE*(100.0/abs(ml))


# Reuse load + engineer + FEATURES + fit_fold from oof_backtest.py
exec(open(r"c:\Users\jackp\Desktop\new_game\oof_backtest.py").read().split("def main():")[0])


def boot_pl(bets, n_iter=20000):
    if not bets: return None
    pls = np.array([ml_payout(ml, w) for ml, w in bets])
    n = len(pls)
    roi_pct = 100 * pls.mean() / STAKE   # PROPER PERCENT
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1) / STAKE
    return n, roi_pct, 100*boots.std(), float((boots <= 0).mean())


def main():
    df = load(); df = engineer(df)
    fold_defs = []
    for test_year in (2022, 2023, 2024, 2025):
        train_full = df[df["year"] < test_year]
        if len(train_full) < 500: continue
        cutoff = train_full["game_date"].max() - pd.Timedelta(days=42)
        train = train_full[train_full["game_date"] <= cutoff]
        val   = train_full[train_full["game_date"] > cutoff]
        test  = df[df["year"] == test_year]
        if len(val) < 100 or len(test) < 100: continue
        fold_defs.append((test_year, train, val, test))

    all_preds = []
    for test_year, train, val, test in fold_defs:
        print(f"Fold {test_year}: training... ", end="", flush=True)
        preds = fit_fold(train, val, test)
        rec = test.copy()
        rec["p_raw"] = preds["raw"]
        all_preds.append(rec)
        print("done")

    pooled = pd.concat(all_preds, ignore_index=True)
    pooled["year"] = pooled["game_date"].dt.year

    print(f"\nPooled OOF games: {len(pooled)}")

    # ============================ DIAGNOSTIC 1 ============================
    print("\n" + "="*72)
    print("BASELINE: 'bet every home underdog blindly' (no model filter)")
    print("="*72)
    print(f"  {'Year':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>9} {'P(<=0)':>8}")
    overall_bets = []
    for yr in (2022, 2023, 2024, 2025):
        sub = pooled[(pooled["year"] == yr) & (pooled["ml_home_close"] > 0)]
        bets = [(r["ml_home_close"], r["y"] == 1) for _, r in sub.iterrows()]
        overall_bets.extend(bets)
        s = boot_pl(bets, n_iter=4000)
        if s:
            n, roi, sd, p = s
            wins = sum(1 for _, w in bets if w)
            wp = 100*wins/n
            avg_ml = np.mean([ml for ml, _ in bets])
            profit = sum(ml_payout(ml, w) for ml, w in bets)
            print(f"  {yr:>6} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+8.2f}% {p:>7.3f}")
    s = boot_pl(overall_bets)
    if s:
        n, roi, sd, p = s
        wins = sum(1 for _, w in overall_bets if w)
        wp = 100*wins/n
        avg_ml = np.mean([ml for ml, _ in overall_bets])
        profit = sum(ml_payout(ml, w) for ml, w in overall_bets)
        print(f"  {'TOTAL':>6} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+8.2f}% {p:>7.3f}")

    # ============================ DIAGNOSTIC 2 ============================
    print("\n" + "="*72)
    print("STRATEGY: home underdog WITH model filter (raw edge > thr)")
    print("="*72)
    for thr in (0.00, 0.02, 0.04, 0.06, 0.08, 0.10):
        print(f"\n  thr={thr:.2f}")
        print(f"  {'Year':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>9} {'P(<=0)':>8}")
        overall_bets = []
        for yr in (2022, 2023, 2024, 2025):
            sub = pooled[(pooled["year"] == yr) & (pooled["ml_home_close"] > 0)]
            bets = []
            for _, r in sub.iterrows():
                eh = r["p_raw"] - r["p_home_fair"]
                if eh > thr:
                    bets.append((r["ml_home_close"], r["y"] == 1))
            overall_bets.extend(bets)
            if not bets: continue
            s = boot_pl(bets, n_iter=4000)
            n, roi, sd, p = s
            wins = sum(1 for _, w in bets if w)
            wp = 100*wins/n
            avg_ml = np.mean([ml for ml, _ in bets])
            profit = sum(ml_payout(ml, w) for ml, w in bets)
            print(f"  {yr:>6} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+8.2f}% {p:>7.3f}")
        if overall_bets:
            s = boot_pl(overall_bets)
            n, roi, sd, p = s
            wins = sum(1 for _, w in overall_bets if w)
            wp = 100*wins/n
            avg_ml = np.mean([ml for ml, _ in overall_bets])
            profit = sum(ml_payout(ml, w) for ml, w in overall_bets)
            print(f"  {'TOTAL':>6} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+8.2f}% {p:>7.3f}")


if __name__ == "__main__":
    main()
