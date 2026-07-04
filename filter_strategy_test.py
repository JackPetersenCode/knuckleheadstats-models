"""Test one final strategy on the pooled OOF data:

  Bet only when (a) raw-model edge > thr AND (b) line moved IN AGREEMENT with
  the model pick. Idea: sharp-money agreement with model = stronger signal.

Then bootstrap. If this slice is positive at p<0.10, real-money paper-trade may
be worth it. If not, project is well and truly closed.

Reuses the OOF predictions from oof_backtest.py (regenerated here for cleanness).
"""
import os
import numpy as np
import pandas as pd
import psycopg2
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.metrics import log_loss
import xgboost as xgb
import lightgbm as lgb

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


# Re-import the load/engineer/FEATURES/fit_fold from the oof script
exec(open(r"c:\Users\jackp\Desktop\new_game\oof_backtest.py").read().split("def main():")[0])


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
    print(f"\nPooled OOF: {len(pooled)} games")

    # Strategy: bet only when model edge > thr AND line moved in agreement
    # line_move > 0 means home line moved toward home (sharp money on home)
    # line_move < 0 means sharp money on away
    print("\n" + "="*72)
    print("STRATEGY 1: raw model edge + line-movement agreement")
    print("="*72)
    print("Bet HOME only if (raw p_home - market_p_home > thr) AND line_move > 0")
    print("Bet AWAY only if (raw p_away - market_p_away > thr) AND line_move < 0")
    print(f"\n  {'Thr':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8} {'P(<=0)':>8}")
    for thr in (0.00, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
        bets = []
        for _, r in pooled.iterrows():
            eh = r["p_raw"] - r["p_home_fair"]
            ea = (1-r["p_raw"]) - (1 - r["p_home_fair"])
            lm = r.get("line_move", 0.0) if not pd.isna(r.get("line_move", np.nan)) else 0.0
            if eh > thr and lm > 0:
                bets.append((r["ml_home_close"], r["y"] == 1))
            elif ea > thr and lm < 0:
                bets.append((r["ml_away_close"], r["y"] == 0))
        if not bets: continue
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        n = len(pls); roi = pls.mean()/STAKE
        wins = sum(1 for _, w in bets if w)
        boots = RNG.choice(pls, size=(8000, n), replace=True).mean(axis=1)/STAKE
        p_le = float((boots <= 0).mean())
        avg_ml = np.mean([ml for ml, _ in bets])
        wp = 100*wins/n
        profit = pls.sum()
        print(f"  {thr:>6.2f} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+7.2f}% {p_le:>8.3f}")

    # Strategy 2: model edge AGAINST line direction (contrarian)
    print("\n" + "="*72)
    print("STRATEGY 2: model edge with line moving AGAINST model (contrarian)")
    print("="*72)
    print(f"  {'Thr':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8} {'P(<=0)':>8}")
    for thr in (0.00, 0.02, 0.04, 0.06, 0.08, 0.10):
        bets = []
        for _, r in pooled.iterrows():
            eh = r["p_raw"] - r["p_home_fair"]
            ea = (1-r["p_raw"]) - (1 - r["p_home_fair"])
            lm = r.get("line_move", 0.0) if not pd.isna(r.get("line_move", np.nan)) else 0.0
            if eh > thr and lm < 0:
                bets.append((r["ml_home_close"], r["y"] == 1))
            elif ea > thr and lm > 0:
                bets.append((r["ml_away_close"], r["y"] == 0))
        if not bets: continue
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        n = len(pls); roi = pls.mean()/STAKE
        wins = sum(1 for _, w in bets if w)
        boots = RNG.choice(pls, size=(8000, n), replace=True).mean(axis=1)/STAKE
        p_le = float((boots <= 0).mean())
        avg_ml = np.mean([ml for ml, _ in bets])
        wp = 100*wins/n
        profit = pls.sum()
        print(f"  {thr:>6.2f} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+7.2f}% {p_le:>8.3f}")

    # Strategy 3: only bet underdogs (model thinks dog wins more than market does)
    print("\n" + "="*72)
    print("STRATEGY 3: underdog-only picks (avoid heavy favorites where vig hurts most)")
    print("="*72)
    print(f"  {'Thr':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8} {'P(<=0)':>8}")
    for thr in (0.00, 0.02, 0.04, 0.06, 0.08, 0.10):
        bets = []
        for _, r in pooled.iterrows():
            eh = r["p_raw"] - r["p_home_fair"]
            ea = (1-r["p_raw"]) - (1 - r["p_home_fair"])
            # Only bet if the picked side is an underdog (positive ML)
            if eh > thr and r["ml_home_close"] > 0:
                bets.append((r["ml_home_close"], r["y"] == 1))
            elif ea > thr and r["ml_away_close"] > 0:
                bets.append((r["ml_away_close"], r["y"] == 0))
        if not bets: continue
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        n = len(pls); roi = pls.mean()/STAKE
        wins = sum(1 for _, w in bets if w)
        boots = RNG.choice(pls, size=(8000, n), replace=True).mean(axis=1)/STAKE
        p_le = float((boots <= 0).mean())
        avg_ml = np.mean([ml for ml, _ in bets])
        wp = 100*wins/n
        profit = pls.sum()
        print(f"  {thr:>6.2f} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+7.2f}% {p_le:>8.3f}")

    # Strategy 4: home dogs only (well-known soft inefficiency)
    print("\n" + "="*72)
    print("STRATEGY 4: home underdogs only (known weak market segment)")
    print("="*72)
    print(f"  {'Thr':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8} {'P(<=0)':>8}")
    for thr in (0.00, 0.02, 0.04, 0.06, 0.08):
        bets = []
        for _, r in pooled.iterrows():
            eh = r["p_raw"] - r["p_home_fair"]
            if eh > thr and r["ml_home_close"] > 0:
                bets.append((r["ml_home_close"], r["y"] == 1))
        if not bets: continue
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        n = len(pls); roi = pls.mean()/STAKE
        wins = sum(1 for _, w in bets if w)
        boots = RNG.choice(pls, size=(8000, n), replace=True).mean(axis=1)/STAKE
        p_le = float((boots <= 0).mean())
        avg_ml = np.mean([ml for ml, _ in bets])
        wp = 100*wins/n
        profit = pls.sum()
        print(f"  {thr:>6.2f} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+7.2f}% {p_le:>8.3f}")


if __name__ == "__main__":
    main()
