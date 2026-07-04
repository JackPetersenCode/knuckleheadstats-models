"""Save v5 model artifact WITHOUT the broken isotonic calibrator.

After the sanity check + OOF backtest revealed isotonic was collapsing
probabilities to ~5 buckets and adding noise, this version uses ONLY the
raw ensemble (LR + XGBoost + LightGBM averaged).

Output: v5_clean_model.pkl
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
OUT = Path(r"c:\Users\jackp\Desktop\new_game\v5_clean_model.pkl")

# Reuse load + engineer + FEATURES from oof_backtest.py
exec(open(r"c:\Users\jackp\Desktop\new_game\oof_backtest.py").read().split("def main():")[0])


def main():
    df = load(); df = engineer(df)
    print(f"Total games: {len(df)}")

    cutoff = df["game_date"].max() - pd.Timedelta(days=42)
    train = df[df["game_date"] <= cutoff].copy()
    val   = df[df["game_date"] > cutoff].copy()
    print(f"  train: {len(train)} games up to {cutoff.date()}")
    print(f"  val:   {len(val)} games (last 6 weeks, used only for early-stopping)")

    Xt = train[FEATURES].values; yt = train["y"].astype(int).values
    Xv = val[FEATURES].values; yv = val["y"].astype(int).values

    imp = SimpleImputer(strategy="median").fit(Xt)
    Xt_i = imp.transform(Xt); Xv_i = imp.transform(Xv)
    sc = StandardScaler().fit(Xt_i)
    Xt_s = sc.transform(Xt_i); Xv_s = sc.transform(Xv_i)

    print("Training LR..."); lr = LogisticRegression(C=0.15, max_iter=5000).fit(Xt_s, yt)
    print("Training XGB...")
    xgbm = xgb.XGBClassifier(
        n_estimators=1000, max_depth=4, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_weight=8, eval_metric="logloss",
        early_stopping_rounds=40, random_state=42, n_jobs=4, tree_method="hist")
    xgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], verbose=False)
    print(f"  XGB best iter: {xgbm.best_iteration}")

    print("Training LGB...")
    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], callbacks=[lgb.early_stopping(40)])

    # Just the raw ensemble — NO isotonic, NO Platt
    val_ens = (lr.predict_proba(Xv_s)[:,1]
               + xgbm.predict_proba(Xv_i)[:,1]
               + lgbm.predict_proba(Xv_i)[:,1]) / 3
    val_ll = log_loss(yv, np.clip(val_ens, 1e-4, 1-1e-4))
    market_ll = log_loss(yv, np.clip(val["p_home_fair"].values, 1e-4, 1-1e-4))
    print(f"\nVal log loss: market={market_ll:.4f}  raw-ensemble={val_ll:.4f}")
    print(f"  (raw ensemble is roughly market-parity, which is its honest level)")

    artifact = dict(
        features=FEATURES,
        imputer=imp,
        scaler=sc,
        lr=lr, xgb=xgbm, lgb=lgbm,
        # NO calibrator — caller should use the raw ensemble probability directly
        calibration="raw_ensemble_no_calibration",
        trained_through=str(cutoff.date()),
        val_market_log_loss=float(market_ll),
        val_model_log_loss=float(val_ll),
        # Recommended strategy from OOF backtest:
        recommended_strategy=dict(
            description="Bet only home underdogs (ml_home_close > 0) "
                        "where (raw_p_home - market_p_home) > 0.06",
            threshold=0.06,
            home_only=True,
            underdog_only=True,
            expected_oof_roi_pct=5.15,
            oof_p_le_0=0.168,
            oof_bets=537,
            oof_years_positive="3 of 4",
        ),
    )
    with open(OUT, "wb") as f:
        pickle.dump(artifact, f)
    print(f"\nSaved: {OUT}")
    print(f"  size: {OUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
