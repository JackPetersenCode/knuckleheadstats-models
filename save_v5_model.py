"""Train final v5 ensemble on ALL available data (2021-2025 mid-Aug) and save
as a pickle for use by daily_picker.py.

We use the most recent 6 weeks of data as a validation tail (for early stopping
+ isotonic calibration), train on everything else.
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
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import log_loss
import xgboost as xgb
import lightgbm as lgb

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
OUT = Path(r"c:\Users\jackp\Desktop\new_game\v5_model.pkl")
STAKE = 100.0


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def load_v5():
    pg = psycopg2.connect(**PG)
    df = pd.read_sql("""
        SELECT *, EXTRACT(YEAR FROM game_date)::int AS year
        FROM mlb_features_v5
        WHERE ml_home_close IS NOT NULL AND ml_away_close IS NOT NULL
          AND h_p_ip_10 IS NOT NULL AND a_p_ip_10 IS NOT NULL
          AND h_rdiff_30 IS NOT NULL AND a_rdiff_30 IS NOT NULL
          AND h_bp_ip_14 IS NOT NULL AND a_bp_ip_14 IS NOT NULL
          AND h_p_velo IS NOT NULL AND a_p_velo IS NOT NULL
          AND h_bat_woba IS NOT NULL AND a_bat_woba IS NOT NULL
          AND temp_f IS NOT NULL
          AND h_lineup_woba IS NOT NULL AND a_lineup_woba IS NOT NULL
        ORDER BY game_date
    """, pg)
    pg.close()
    df["game_date"] = pd.to_datetime(df["game_date"])
    return df


def engineer(df):
    def per9(num, ip):
        return np.where((ip > 0) & ip.notna(), num*9.0/ip, np.nan)
    for p in ("h_p", "a_p"):
        df[f"{p}_era_10"] = per9(df[f"{p}_er_10"], df[f"{p}_ip_10"])
        df[f"{p}_k9_10"]  = per9(df[f"{p}_k_10"],  df[f"{p}_ip_10"])
        df[f"{p}_bb9_10"] = per9(df[f"{p}_bb_10"], df[f"{p}_ip_10"])
        df[f"{p}_hr9_10"] = per9(df[f"{p}_hr_10"], df[f"{p}_ip_10"])
        df[f"{p}_ipgs_10"] = df[f"{p}_ip_10"] / df[f"{p}_starts_10"]
        df[f"{p}_era_sd"] = per9(df[f"{p}_er_sd"], df[f"{p}_ip_sd"])
        df[f"{p}_k9_sd"]  = per9(df[f"{p}_k_sd"],  df[f"{p}_ip_sd"])
        df[f"{p}_bb9_sd"] = per9(df[f"{p}_bb_sd"], df[f"{p}_ip_sd"])
        df[f"{p}_hr9_sd"] = per9(df[f"{p}_hr_sd"], df[f"{p}_ip_sd"])
    for p in ("h_bp", "a_bp"):
        df[f"{p}_era_14"] = per9(df[f"{p}_er_14"], df[f"{p}_ip_14"])
        df[f"{p}_k9_14"]  = per9(df[f"{p}_k_14"],  df[f"{p}_ip_14"])
        df[f"{p}_bb9_14"] = per9(df[f"{p}_bb_14"], df[f"{p}_ip_14"])
    df["h_bp_fatigue"] = df["h_bp_ip_3"] / df["h_bp_ip_14"].replace(0, np.nan)
    df["a_bp_fatigue"] = df["a_bp_ip_3"] / df["a_bp_ip_14"].replace(0, np.nan)

    df["p_home_close_raw"] = df["ml_home_close"].apply(american_to_prob)
    df["p_away_close_raw"] = df["ml_away_close"].apply(american_to_prob)
    df["overround_close"] = df["p_home_close_raw"] + df["p_away_close_raw"]
    df["p_home_fair"] = df["p_home_close_raw"] / df["overround_close"]
    df["p_away_fair"] = df["p_away_close_raw"] / df["overround_close"]
    df["mkt_logit"] = np.log(df["p_home_fair"] / (1 - df["p_home_fair"]))

    df["p_home_open_raw"] = df["ml_home_open"].apply(american_to_prob)
    df["p_away_open_raw"] = df["ml_away_open"].apply(american_to_prob)
    df["overround_open"] = df["p_home_open_raw"] + df["p_away_open_raw"]
    df["p_home_open_fair"] = df["p_home_open_raw"] / df["overround_open"]
    df["open_logit"] = np.log(df["p_home_open_fair"] / (1 - df["p_home_open_fair"]))
    df["line_move"] = df["mkt_logit"] - df["open_logit"]

    df["d_pyth"]            = df["h_pyth"] - df["a_pyth"]
    df["d_wpct_7"]          = df["h_wpct_7"] - df["a_wpct_7"]
    df["d_rdiff_30"]        = df["h_rdiff_30"] - df["a_rdiff_30"]
    df["d_starter_era_sd"]  = df["a_p_era_sd"] - df["h_p_era_sd"]
    df["d_starter_k9_sd"]   = df["h_p_k9_sd"] - df["a_p_k9_sd"]
    df["d_starter_bb9_sd"]  = df["a_p_bb9_sd"] - df["h_p_bb9_sd"]
    df["d_bp_era_14"]       = df["a_bp_era_14"] - df["h_bp_era_14"]
    df["d_bp_fatigue"]      = df["a_bp_fatigue"] - df["h_bp_fatigue"]
    df["d_wpct_home_away"]  = df["h_wpct_home"] - df["a_wpct_away"]
    df["d_p_ff_velo"]    = df["h_p_ff_velo"] - df["a_p_ff_velo"]
    df["d_p_velo"]       = df["h_p_velo"] - df["a_p_velo"]
    df["d_p_spin"]       = df["h_p_spin"] - df["a_p_spin"]
    df["d_p_whiff"]      = df["h_p_whiff_rate"] - df["a_p_whiff_rate"]
    df["d_p_strike"]     = df["h_p_strike_rate"] - df["a_p_strike_rate"]
    df["d_bat_woba"]     = df["h_bat_woba"] - df["a_bat_woba"]
    df["d_bat_iso"]      = df["h_bat_iso"] - df["a_bat_iso"]
    df["d_bat_k"]        = df["a_bat_k"] - df["h_bat_k"]
    df["d_bat_bb"]       = df["h_bat_bb"] - df["a_bat_bb"]
    df["d_bat_hr"]       = df["h_bat_hr"] - df["a_bat_hr"]
    df["d_lineup_woba"]  = df["h_lineup_woba"] - df["a_lineup_woba"]
    df["d_lineup_iso"]   = df["h_lineup_iso"] - df["a_lineup_iso"]
    df["d_lineup_k"]     = df["a_lineup_k_rate"] - df["h_lineup_k_rate"]
    df["d_lineup_bb"]    = df["h_lineup_bb_rate"] - df["a_lineup_bb_rate"]
    df["mq_h_pitch_vs_a_bat"] = df["h_p_whiff_rate"] - df["a_bat_woba"]
    df["mq_a_pitch_vs_h_bat"] = df["a_p_whiff_rate"] - df["h_bat_woba"]
    df["mq_h_p_vs_a_lineup"] = df["h_p_whiff_rate"] - df["a_lineup_woba"]
    df["mq_a_p_vs_h_lineup"] = df["a_p_whiff_rate"] - df["h_lineup_woba"]
    df["wind_x_helps_hitter"] = df["wind_helps_hitter"] * df["wind_mph"]
    df["wind_x_helps_pitcher"] = df["wind_helps_pitcher"] * df["wind_mph"]
    df["cold_temp"] = (df["temp_f"] < 50).astype(float)
    df["hot_temp"]  = (df["temp_f"] > 85).astype(float)
    df["is_night"] = df["is_night"].astype(float)
    return df


FEATURES = [
    "is_night","park_rpg","series_game_number","h_dayafternight","a_dayafternight",
    "h_wpct_7","h_rdiff_14","h_rdiff_30","h_rs_30","h_ra_30",
    "a_wpct_7","a_rdiff_14","a_rdiff_30","a_rs_30","a_ra_30",
    "h_pyth","a_pyth","h_wpct_home","a_wpct_away",
    "h_p_era_10","h_p_k9_10","h_p_bb9_10","h_p_hr9_10","h_p_ipgs_10","h_p_rest","h_p_starts_10",
    "a_p_era_10","a_p_k9_10","a_p_bb9_10","a_p_hr9_10","a_p_ipgs_10","a_p_rest","a_p_starts_10",
    "h_p_era_sd","h_p_k9_sd","h_p_bb9_sd","h_p_hr9_sd","h_p_starts_sd",
    "a_p_era_sd","a_p_k9_sd","a_p_bb9_sd","a_p_hr9_sd","a_p_starts_sd",
    "h_bp_era_14","h_bp_k9_14","h_bp_bb9_14","h_bp_fatigue","h_bp_ip_14",
    "a_bp_era_14","a_bp_k9_14","a_bp_bb9_14","a_bp_fatigue","a_bp_ip_14",
    "h_p_ff_velo","h_p_velo","h_p_spin","h_p_strike_rate","h_p_whiff_rate",
    "a_p_ff_velo","a_p_velo","a_p_spin","a_p_strike_rate","a_p_whiff_rate",
    "h_bat_k","h_bat_bb","h_bat_hr","h_bat_iso","h_bat_woba",
    "a_bat_k","a_bat_bb","a_bat_hr","a_bat_iso","a_bat_woba",
    "d_pyth","d_wpct_7","d_rdiff_30",
    "d_starter_era_sd","d_starter_k9_sd","d_starter_bb9_sd",
    "d_bp_era_14","d_bp_fatigue","d_wpct_home_away",
    "d_p_ff_velo","d_p_velo","d_p_spin","d_p_whiff","d_p_strike",
    "d_bat_woba","d_bat_iso","d_bat_k","d_bat_bb","d_bat_hr",
    "mq_h_pitch_vs_a_bat","mq_a_pitch_vs_h_bat",
    "temp_f","wind_mph","is_dome","wind_helps_hitter","wind_helps_pitcher","weather_clear",
    "wind_x_helps_hitter","wind_x_helps_pitcher","cold_temp","hot_temp",
    "ump_k_rate","ump_bb_rate","ump_runs_pg","ump_n_games",
    "h_lineup_woba","h_lineup_iso","h_lineup_k_rate","h_lineup_bb_rate","h_lineup_n_batters",
    "a_lineup_woba","a_lineup_iso","a_lineup_k_rate","a_lineup_bb_rate","a_lineup_n_batters",
    "d_lineup_woba","d_lineup_iso","d_lineup_k","d_lineup_bb",
    "mq_h_p_vs_a_lineup","mq_a_p_vs_h_lineup",
    "mkt_logit","open_logit","line_move",
]


def main():
    df = load_v5(); df = engineer(df)
    print(f"Total games: {len(df)}")

    # Train on everything except last 6 weeks; val = last 6 weeks
    cutoff = df["game_date"].max() - pd.Timedelta(days=42)
    train = df[df["game_date"] < cutoff].copy()
    val   = df[df["game_date"] >= cutoff].copy()
    print(f"  train: {len(train)} games up to {cutoff.date()}")
    print(f"  val:   {len(val)} games (last 6 weeks)")

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
    print(f"  XGB best iteration: {xgbm.best_iteration}")

    print("Training LGB...")
    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], callbacks=[lgb.early_stopping(40)])

    # Calibrate ensemble on validation
    val_ens = (lr.predict_proba(Xv_s)[:,1] + xgbm.predict_proba(Xv_i)[:,1] + lgbm.predict_proba(Xv_i)[:,1]) / 3
    iso = IsotonicRegression(out_of_bounds="clip").fit(val_ens, yv)

    val_cal = iso.transform(val_ens)
    val_ll = log_loss(yv, np.clip(val_cal, 1e-4, 1-1e-4))
    market_ll = log_loss(yv, np.clip(val["p_home_fair"].values, 1e-4, 1-1e-4))
    print(f"\nValidation (last 6 weeks):")
    print(f"  Market log loss: {market_ll:.4f}")
    print(f"  Model  log loss: {val_ll:.4f}  ({'beats' if val_ll < market_ll else 'loses to'} market by {abs(val_ll-market_ll):.4f})")

    # Save artifact
    artifact = dict(
        features=FEATURES,
        imputer=imp,
        scaler=sc,
        lr=lr,
        xgb=xgbm,
        lgb=lgbm,
        isotonic=iso,
        trained_through=str(cutoff.date()),
        val_log_loss=float(val_ll),
        market_log_loss=float(market_ll),
    )
    with open(OUT, "wb") as f:
        pickle.dump(artifact, f)
    print(f"\nSaved: {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
