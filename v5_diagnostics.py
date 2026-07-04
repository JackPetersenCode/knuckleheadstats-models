"""V5 rolling-origin cross-validation + calibration + subset diagnostics.

Train/val/test/hold splits (rolling-origin, never train on future):

  Fold A:  train 2021..2022, val 2023 H1, test 2023 H2, hold 2024+2025
  Fold B:  train 2021..2023 H1, val 2023 H2, test 2024 H1, hold 2024 H2 + 2025
  Fold C:  train 2021..2024 H1, val 2024 H1 last 30d, test 2024 H2, hold 2025  (= original v5)

For each fold:
  - Log loss vs market on test & hold
  - ROI at thr=0.06, 0.08 on test & hold
  - Calibration: predicted-prob decile -> actual win-rate
The +2.55% slice replicates iff it's positive across folds.
"""
import os
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
STAKE = 100.0
RNG = np.random.default_rng(42)


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


def ml_payout(ml, won):
    if not won: return -STAKE
    return STAKE*(ml/100.0) if ml > 0 else STAKE*(100.0/abs(ml))


def load():
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


FEATURES = [  # same as v5
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


def fit(Xtr, ytr, Xva, yva, Xte_dict):
    imp = SimpleImputer(strategy="median").fit(Xtr)
    Xtr_i = imp.transform(Xtr); Xva_i = imp.transform(Xva)
    Xte_i = {k: imp.transform(v) for k, v in Xte_dict.items()}
    sc = StandardScaler().fit(Xtr_i)
    Xtr_s = sc.transform(Xtr_i); Xva_s = sc.transform(Xva_i)
    Xte_s = {k: sc.transform(v) for k, v in Xte_i.items()}

    lr = LogisticRegression(C=0.15, max_iter=5000).fit(Xtr_s, ytr)
    xgbm = xgb.XGBClassifier(
        n_estimators=1000, max_depth=4, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_weight=8, objective="binary:logistic", eval_metric="logloss",
        early_stopping_rounds=40, random_state=42, n_jobs=4, tree_method="hist")
    xgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], verbose=False)
    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, max_depth=-1, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], callbacks=[lgb.early_stopping(40)])

    out = {}
    for sk, v_i, v_s in [("val", Xva_i, Xva_s)] + [(k, Xte_i[k], Xte_s[k]) for k in Xte_dict]:
        ens = (lr.predict_proba(v_s)[:,1] + xgbm.predict_proba(v_i)[:,1] + lgbm.predict_proba(v_i)[:,1]) / 3
        out[sk] = ens
    iso = IsotonicRegression(out_of_bounds="clip").fit(out["val"], yva)
    out_cal = {sk: iso.transform(p) for sk, p in out.items()}
    return out, out_cal


def collect_bets(df, prob_col, thr):
    bets = []
    p = df[prob_col].values; ph = df["p_home_fair"].values; pa = df["p_away_fair"].values
    y = df["y"].values; mh = df["ml_home_close"].values; ma = df["ml_away_close"].values
    for i in range(len(df)):
        eh = p[i] - ph[i]; ea = (1-p[i]) - pa[i]
        if eh > thr: bets.append((mh[i], y[i] == 1, "home"))
        elif ea > thr: bets.append((ma[i], y[i] == 0, "away"))
    return bets


def bet_stats(bets):
    if not bets: return (0, 0, np.nan, 0.0, np.nan)
    pls = np.array([ml_payout(ml, w) for ml, w, _ in bets])
    n = len(pls); roi = pls.mean()/STAKE
    wins = sum(1 for _, w, _ in bets if w)
    boots = RNG.choice(pls, size=(4000, n), replace=True).mean(axis=1)/STAKE
    p_le_0 = float((boots <= 0).mean())
    return (n, wins, 100*roi, sum(pls), p_le_0)


def split_fold(df, fold_id):
    """Returns (train, val, test, hold) DataFrames for the requested fold."""
    d = df["game_date"]
    if fold_id == "A":
        train = df[d.dt.year <= 2022]
        val   = df[(d.dt.year == 2023) & (d.dt.month <= 6)]
        test  = df[(d.dt.year == 2023) & (d.dt.month >= 7)]
        hold  = df[d.dt.year >= 2024]
    elif fold_id == "B":
        train = df[(d.dt.year <= 2022) | ((d.dt.year == 2023) & (d.dt.month <= 6))]
        val   = df[(d.dt.year == 2023) & (d.dt.month >= 7)]
        test  = df[(d.dt.year == 2024) & (d.dt.month <= 6)]
        hold  = df[((d.dt.year == 2024) & (d.dt.month >= 7)) | (d.dt.year == 2025)]
    elif fold_id == "C":
        train = df[d.dt.year <= 2023]
        val   = df[(d.dt.year == 2024) & (d.dt.month <= 6)]
        test  = df[(d.dt.year == 2024) & (d.dt.month >= 7)]
        hold  = df[d.dt.year == 2025]
    return train.copy(), val.copy(), test.copy(), hold.copy()


def main():
    df = load(); df = engineer(df)
    print(f"Total v5 games: {len(df)}")
    folds_results = {}

    for fold in ("A", "B", "C"):
        print("\n" + "="*72)
        print(f"FOLD {fold}")
        print("="*72)
        train, val, test, hold = split_fold(df, fold)
        print(f"  train={len(train)} val={len(val)} test={len(test)} hold={len(hold)}")

        Xt = train[FEATURES].values; yt = train["y"].astype(int).values
        Xv = val[FEATURES].values;   yv = val["y"].astype(int).values
        Xtest_dict = {"test": test[FEATURES].values, "hold": hold[FEATURES].values}

        out, out_cal = fit(Xt, yt, Xv, yv, Xtest_dict)
        test["p_ens"] = out["test"]
        test["p_cal"] = out_cal["test"]
        hold["p_ens"] = out["hold"]
        hold["p_cal"] = out_cal["hold"]

        market_test = test["p_home_fair"].values
        market_hold = hold["p_home_fair"].values

        def safe_ll(y, p):
            p = np.clip(p, 1e-4, 1-1e-4); return log_loss(y, p)
        print(f"\n  TEST log loss: market={safe_ll(test['y'].values, market_test):.4f}  "
              f"ens={safe_ll(test['y'].values, test['p_ens']):.4f}  "
              f"cal={safe_ll(test['y'].values, test['p_cal']):.4f}")
        print(f"  HOLD log loss: market={safe_ll(hold['y'].values, market_hold):.4f}  "
              f"ens={safe_ll(hold['y'].values, hold['p_ens']):.4f}  "
              f"cal={safe_ll(hold['y'].values, hold['p_cal']):.4f}")

        print("\n  Backtest p_cal (ens_cal):")
        print(f"    {'Set':<10}{'Thr':>6}{'Bets':>6}{'Wins':>6}{'Win%':>7}{'ROI%':>8}{'P<=0':>7}")
        rows = []
        for tag, dset in [("TEST", test), ("HOLD", hold), ("T+H", pd.concat([test, hold]))]:
            for thr in [0.04, 0.06, 0.08, 0.10]:
                b = collect_bets(dset, "p_cal", thr)
                n, w, roi, pl, p = bet_stats(b)
                if n == 0: continue
                wp = 100*w/n if n else 0
                print(f"    {tag:<10}{thr:>6.2f}{n:>6}{w:>6}{wp:>6.1f}%{roi:>+7.2f}%{p:>7.3f}")
                rows.append((fold, tag, thr, n, w, wp, roi, p))
        folds_results[fold] = rows

    # Summary: at each (tag, thr), how consistent is positive ROI across folds?
    print("\n" + "="*72)
    print("CROSS-FOLD CONSISTENCY at p_cal")
    print("="*72)
    print(f"{'Tag':<8}{'Thr':>6}{'Fold A ROI':>14}{'Fold B ROI':>14}{'Fold C ROI':>14}{'Avg':>10}")
    for tag in ("TEST", "HOLD", "T+H"):
        for thr in (0.04, 0.06, 0.08, 0.10):
            rois = []
            for fold in ("A", "B", "C"):
                hit = [r for r in folds_results[fold] if r[1] == tag and r[2] == thr]
                rois.append(hit[0][6] if hit else None)
            avg = np.mean([r for r in rois if r is not None])
            cells = [f"{r:>+10.2f}%" if r is not None else "       --" for r in rois]
            print(f"{tag:<8}{thr:>6.2f}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}{avg:>+9.2f}%")

    # Calibration plot for Fold C
    print("\n" + "="*72)
    print("CALIBRATION (Fold C, test+hold combined)")
    print("="*72)
    print(f"{'Decile':<8}{'pred_mean':>11}{'actual_rate':>13}{'n':>8}{'avg_market':>12}")
    train, val, test, hold = split_fold(df, "C")
    Xt = train[FEATURES].values; yt = train["y"].astype(int).values
    Xv = val[FEATURES].values;   yv = val["y"].astype(int).values
    out, out_cal = fit(Xt, yt, Xv, yv,
                       {"test": test[FEATURES].values, "hold": hold[FEATURES].values})
    th = pd.concat([test.assign(p=out_cal["test"]), hold.assign(p=out_cal["hold"])], ignore_index=True)
    th["dec"] = pd.qcut(th["p"], 10, labels=False, duplicates="drop")
    for d, g in th.groupby("dec"):
        print(f"  {int(d):<6}{g['p'].mean():>11.3f}{g['y'].mean():>13.3f}{len(g):>8}{g['p_home_fair'].mean():>12.3f}")


if __name__ == "__main__":
    main()
