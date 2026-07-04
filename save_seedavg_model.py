"""Save a seed-averaged ensemble. Averages predictions across 3 seeds to reduce
seed-dependent variance. Better operational model than v5_clean_model.pkl.

Output: v5_seedavg_model.pkl
"""
import os
import pickle
from pathlib import Path
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
OUT = Path(r"c:\Users\jackp\Desktop\new_game\v5_seedavg_model.pkl")
SEEDS = [1, 42, 99]

exec(open(r"c:\Users\jackp\Desktop\new_game\oof_backtest.py").read().split("def main():")[0])


def main():
    df = load(); df = engineer(df)
    cutoff = df["game_date"].max() - pd.Timedelta(days=42)
    train = df[df["game_date"] <= cutoff].copy()
    val   = df[df["game_date"] > cutoff].copy()
    print(f"train: {len(train)} val: {len(val)}")

    Xt = train[FEATURES].values; yt = train["y"].astype(int).values
    Xv = val[FEATURES].values; yv = val["y"].astype(int).values

    imp = SimpleImputer(strategy="median").fit(Xt)
    Xt_i = imp.transform(Xt); Xv_i = imp.transform(Xv)
    sc = StandardScaler().fit(Xt_i)
    Xt_s = sc.transform(Xt_i); Xv_s = sc.transform(Xv_i)

    models = []
    for seed in SEEDS:
        print(f"\nTraining seed={seed}...")
        lr = LogisticRegression(C=0.15, max_iter=5000, random_state=seed).fit(Xt_s, yt)
        xgbm = xgb.XGBClassifier(
            n_estimators=1000, max_depth=4, learning_rate=0.02,
            subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
            min_child_weight=8, eval_metric="logloss",
            early_stopping_rounds=40, random_state=seed, n_jobs=4, tree_method="hist")
        xgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], verbose=False)
        lgbm = lgb.LGBMClassifier(
            n_estimators=1000, num_leaves=31, learning_rate=0.02,
            subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
            min_child_samples=20, random_state=seed, n_jobs=4, verbose=-1)
        lgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], callbacks=[lgb.early_stopping(40)])
        models.append((seed, lr, xgbm, lgbm))

    # Val performance of seed-averaged ensemble
    preds_seedavg = np.zeros(len(yv))
    for _, lr, xgbm, lgbm in models:
        ens = (lr.predict_proba(Xv_s)[:,1] + xgbm.predict_proba(Xv_i)[:,1] + lgbm.predict_proba(Xv_i)[:,1]) / 3
        preds_seedavg += ens
    preds_seedavg /= len(models)
    val_ll = log_loss(yv, np.clip(preds_seedavg, 1e-4, 1-1e-4))
    market_ll = log_loss(yv, np.clip(val["p_home_fair"].values, 1e-4, 1-1e-4))
    print(f"\nVal log loss: market={market_ll:.4f}  seed-avg ensemble={val_ll:.4f}")

    artifact = dict(
        features=FEATURES,
        imputer=imp, scaler=sc,
        seed_models=[(seed, lr, xgbm, lgbm) for seed, lr, xgbm, lgbm in models],
        calibration="raw_seed_averaged_no_calibration",
        seeds=SEEDS,
        trained_through=str(cutoff.date()),
        val_market_log_loss=float(market_ll),
        val_model_log_loss=float(val_ll),
        recommended_strategy=dict(
            description="Bet only home underdogs (ml_home_close > 0) where (seed_avg_p_home - market_p_home_fair) > 0.06",
            threshold=0.06,
            home_only=True,
            underdog_only=True,
            expected_seed_robust_roi_pct=1.84,
            seed_std_roi_pct=1.77,
            seed_range="all 5 seeds positive, +0.35% to +4.98%",
            oof_bets_per_season="~135",
        ),
    )
    with open(OUT, "wb") as f:
        pickle.dump(artifact, f)
    print(f"Saved: {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
