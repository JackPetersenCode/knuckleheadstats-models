"""Robustness check on the home-underdog finding across different random seeds.

If the +5.15% ROI is real, it should be stable when we change:
  * the random seed of the XGBoost / LightGBM / bootstrap RNG
The finding shouldn't depend on a lucky tree-building order.
"""
import os
import numpy as np
import pandas as pd
import psycopg2
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
import xgboost as xgb
import lightgbm as lgb

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
STAKE = 100.0


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def ml_payout(ml, won):
    if not won: return -STAKE
    return STAKE*(ml/100.0) if ml > 0 else STAKE*(100.0/abs(ml))


# Reuse load + engineer + FEATURES from oof_backtest.py
exec(open(r"c:\Users\jackp\Desktop\new_game\oof_backtest.py").read().split("def main():")[0])


def fit_fold_with_seed(train, val, test, seed):
    yt = train["y"].astype(int).values
    yv = val["y"].astype(int).values
    Xt = train[FEATURES].values
    Xv = val[FEATURES].values
    Xte = test[FEATURES].values
    imp = SimpleImputer(strategy="median").fit(Xt)
    Xt_i = imp.transform(Xt); Xv_i = imp.transform(Xv); Xte_i = imp.transform(Xte)
    sc = StandardScaler().fit(Xt_i)
    Xt_s = sc.transform(Xt_i); Xv_s = sc.transform(Xv_i); Xte_s = sc.transform(Xte_i)
    lr = LogisticRegression(C=0.15, max_iter=5000, random_state=seed).fit(Xt_s, yt)
    p_lr = lr.predict_proba(Xte_s)[:,1]
    xgbm = xgb.XGBClassifier(
        n_estimators=1000, max_depth=4, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_weight=8, eval_metric="logloss",
        early_stopping_rounds=40, random_state=seed, n_jobs=4, tree_method="hist")
    xgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], verbose=False)
    p_xgb = xgbm.predict_proba(Xte_i)[:,1]
    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=seed, n_jobs=4, verbose=-1)
    lgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], callbacks=[lgb.early_stopping(40)])
    p_lgb = lgbm.predict_proba(Xte_i)[:,1]
    return (p_lr + p_xgb + p_lgb) / 3


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

    SEEDS = [1, 13, 42, 99, 2024]
    print(f"Running OOF backtest across {len(SEEDS)} random seeds...")
    print(f"Strategy: home underdog only, raw-ensemble edge > 0.06\n")
    print(f"{'Seed':>6}  {'Bets':>5} {'Wins':>5}  {'Win%':>6}  {'AvgML':>7}  {'Profit':>9}  {'ROI%':>8}")

    all_rois = []
    for seed in SEEDS:
        all_test_preds = []
        for test_year, train, val, test in fold_defs:
            p = fit_fold_with_seed(train, val, test, seed)
            rec = test.copy()
            rec["p_raw"] = p
            all_test_preds.append(rec)
        pooled = pd.concat(all_test_preds, ignore_index=True)

        bets = []
        for _, r in pooled.iterrows():
            eh = r["p_raw"] - r["p_home_fair"]
            if eh > 0.06 and r["ml_home_close"] > 0:
                bets.append((r["ml_home_close"], r["y"] == 1))
        if not bets:
            continue
        n = len(bets); wins = sum(1 for _, w in bets if w)
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        roi = 100 * pls.mean() / STAKE
        all_rois.append(roi)
        avg_ml = np.mean([ml for ml, _ in bets])
        print(f"  {seed:>4}  {n:>5} {wins:>5}  {100*wins/n:>5.1f}%  {avg_ml:>+7.0f}  {pls.sum():>+9.0f}  {roi:>+7.2f}%")

    print()
    print(f"Across {len(SEEDS)} seeds:")
    print(f"  mean ROI : {np.mean(all_rois):+.2f}%")
    print(f"  std  ROI : {np.std(all_rois):.2f}%")
    print(f"  min  ROI : {min(all_rois):+.2f}%")
    print(f"  max  ROI : {max(all_rois):+.2f}%")
    print()
    if min(all_rois) > 0:
        print("ROBUST: every seed shows positive ROI for this strategy.")
    elif np.mean(all_rois) > 2 and min(all_rois) > -2:
        print("SUGGESTIVE: mean is solidly positive; worst seed is only modestly negative.")
    else:
        print("FRAGILE: positive ROI depends on lucky seed -- finding is unreliable.")


if __name__ == "__main__":
    main()
