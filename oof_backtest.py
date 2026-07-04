"""Out-of-fold (OOF) backtest — the gold-standard validation.

For each test fold, train models on ALL earlier data, val on last 6 weeks of
training data (for early stopping + calibration), predict on test fold. Pool
ALL test predictions across folds → one giant truly-out-of-sample sample.

Compare:
  * Isotonic calibration (current v5)
  * Platt calibration (sigmoid)
  * Raw ensemble (no calibration)

Then backtest pooled OOF at multiple thresholds with bootstrap p-values.

This answers: "What ROI would v5 have produced if you'd used it correctly
every day from 2022 onward, with the model retrained each year?"
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
    def per9(num, ip): return np.where((ip>0)&ip.notna(), num*9.0/ip, np.nan)
    for p in ("h_p","a_p"):
        df[f"{p}_era_10"]=per9(df[f"{p}_er_10"],df[f"{p}_ip_10"])
        df[f"{p}_k9_10"]=per9(df[f"{p}_k_10"],df[f"{p}_ip_10"])
        df[f"{p}_bb9_10"]=per9(df[f"{p}_bb_10"],df[f"{p}_ip_10"])
        df[f"{p}_hr9_10"]=per9(df[f"{p}_hr_10"],df[f"{p}_ip_10"])
        df[f"{p}_ipgs_10"]=df[f"{p}_ip_10"]/df[f"{p}_starts_10"]
        df[f"{p}_era_sd"]=per9(df[f"{p}_er_sd"],df[f"{p}_ip_sd"])
        df[f"{p}_k9_sd"]=per9(df[f"{p}_k_sd"],df[f"{p}_ip_sd"])
        df[f"{p}_bb9_sd"]=per9(df[f"{p}_bb_sd"],df[f"{p}_ip_sd"])
        df[f"{p}_hr9_sd"]=per9(df[f"{p}_hr_sd"],df[f"{p}_ip_sd"])
    for p in ("h_bp","a_bp"):
        df[f"{p}_era_14"]=per9(df[f"{p}_er_14"],df[f"{p}_ip_14"])
        df[f"{p}_k9_14"]=per9(df[f"{p}_k_14"],df[f"{p}_ip_14"])
        df[f"{p}_bb9_14"]=per9(df[f"{p}_bb_14"],df[f"{p}_ip_14"])
    df["h_bp_fatigue"]=df["h_bp_ip_3"]/df["h_bp_ip_14"].replace(0,np.nan)
    df["a_bp_fatigue"]=df["a_bp_ip_3"]/df["a_bp_ip_14"].replace(0,np.nan)
    df["p_home_close_raw"]=df["ml_home_close"].apply(american_to_prob)
    df["p_away_close_raw"]=df["ml_away_close"].apply(american_to_prob)
    df["overround_close"]=df["p_home_close_raw"]+df["p_away_close_raw"]
    df["p_home_fair"]=df["p_home_close_raw"]/df["overround_close"]
    df["p_away_fair"]=df["p_away_close_raw"]/df["overround_close"]
    df["mkt_logit"]=np.log(df["p_home_fair"]/(1-df["p_home_fair"]))
    df["p_home_open_raw"]=df["ml_home_open"].apply(american_to_prob)
    df["p_away_open_raw"]=df["ml_away_open"].apply(american_to_prob)
    df["overround_open"]=df["p_home_open_raw"]+df["p_away_open_raw"]
    df["p_home_open_fair"]=df["p_home_open_raw"]/df["overround_open"]
    df["open_logit"]=np.log(df["p_home_open_fair"]/(1-df["p_home_open_fair"]))
    df["line_move"]=df["mkt_logit"]-df["open_logit"]
    df["d_pyth"]=df["h_pyth"]-df["a_pyth"]
    df["d_wpct_7"]=df["h_wpct_7"]-df["a_wpct_7"]
    df["d_rdiff_30"]=df["h_rdiff_30"]-df["a_rdiff_30"]
    df["d_starter_era_sd"]=df["a_p_era_sd"]-df["h_p_era_sd"]
    df["d_starter_k9_sd"]=df["h_p_k9_sd"]-df["a_p_k9_sd"]
    df["d_starter_bb9_sd"]=df["a_p_bb9_sd"]-df["h_p_bb9_sd"]
    df["d_bp_era_14"]=df["a_bp_era_14"]-df["h_bp_era_14"]
    df["d_bp_fatigue"]=df["a_bp_fatigue"]-df["h_bp_fatigue"]
    df["d_wpct_home_away"]=df["h_wpct_home"]-df["a_wpct_away"]
    df["d_p_ff_velo"]=df["h_p_ff_velo"]-df["a_p_ff_velo"]
    df["d_p_velo"]=df["h_p_velo"]-df["a_p_velo"]
    df["d_p_spin"]=df["h_p_spin"]-df["a_p_spin"]
    df["d_p_whiff"]=df["h_p_whiff_rate"]-df["a_p_whiff_rate"]
    df["d_p_strike"]=df["h_p_strike_rate"]-df["a_p_strike_rate"]
    df["d_bat_woba"]=df["h_bat_woba"]-df["a_bat_woba"]
    df["d_bat_iso"]=df["h_bat_iso"]-df["a_bat_iso"]
    df["d_bat_k"]=df["a_bat_k"]-df["h_bat_k"]
    df["d_bat_bb"]=df["h_bat_bb"]-df["a_bat_bb"]
    df["d_bat_hr"]=df["h_bat_hr"]-df["a_bat_hr"]
    df["d_lineup_woba"]=df["h_lineup_woba"]-df["a_lineup_woba"]
    df["d_lineup_iso"]=df["h_lineup_iso"]-df["a_lineup_iso"]
    df["d_lineup_k"]=df["a_lineup_k_rate"]-df["h_lineup_k_rate"]
    df["d_lineup_bb"]=df["h_lineup_bb_rate"]-df["a_lineup_bb_rate"]
    df["mq_h_pitch_vs_a_bat"]=df["h_p_whiff_rate"]-df["a_bat_woba"]
    df["mq_a_pitch_vs_h_bat"]=df["a_p_whiff_rate"]-df["h_bat_woba"]
    df["mq_h_p_vs_a_lineup"]=df["h_p_whiff_rate"]-df["a_lineup_woba"]
    df["mq_a_p_vs_h_lineup"]=df["a_p_whiff_rate"]-df["h_lineup_woba"]
    df["wind_x_helps_hitter"]=df["wind_helps_hitter"]*df["wind_mph"]
    df["wind_x_helps_pitcher"]=df["wind_helps_pitcher"]*df["wind_mph"]
    df["cold_temp"]=(df["temp_f"]<50).astype(float)
    df["hot_temp"]=(df["temp_f"]>85).astype(float)
    df["is_night"]=df["is_night"].astype(float)
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


def fit_fold(train, val, test):
    """Train ensemble on `train` with early-stopping on `val`, predict test+val.
    Returns dict of probabilities (raw_ens, platt, isotonic) for both val and test."""
    yt = train["y"].astype(int).values
    yv = val["y"].astype(int).values

    Xt = train[FEATURES].values
    Xv = val[FEATURES].values
    Xte = test[FEATURES].values

    imp = SimpleImputer(strategy="median").fit(Xt)
    Xt_i = imp.transform(Xt); Xv_i = imp.transform(Xv); Xte_i = imp.transform(Xte)
    sc = StandardScaler().fit(Xt_i)
    Xt_s = sc.transform(Xt_i); Xv_s = sc.transform(Xv_i); Xte_s = sc.transform(Xte_i)

    lr_clf = LogisticRegression(C=0.15, max_iter=5000).fit(Xt_s, yt)
    p_lr_v  = lr_clf.predict_proba(Xv_s)[:,1]
    p_lr_te = lr_clf.predict_proba(Xte_s)[:,1]

    xgbm = xgb.XGBClassifier(
        n_estimators=1000, max_depth=4, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_weight=8, eval_metric="logloss",
        early_stopping_rounds=40, random_state=42, n_jobs=4, tree_method="hist")
    xgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], verbose=False)
    p_xgb_v  = xgbm.predict_proba(Xv_i)[:,1]
    p_xgb_te = xgbm.predict_proba(Xte_i)[:,1]

    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xt_i, yt, eval_set=[(Xv_i, yv)], callbacks=[lgb.early_stopping(40)])
    p_lgb_v  = lgbm.predict_proba(Xv_i)[:,1]
    p_lgb_te = lgbm.predict_proba(Xte_i)[:,1]

    ens_v  = (p_lr_v + p_xgb_v + p_lgb_v) / 3
    ens_te = (p_lr_te + p_xgb_te + p_lgb_te) / 3

    # Isotonic
    iso = IsotonicRegression(out_of_bounds="clip").fit(ens_v, yv)
    iso_te = iso.transform(ens_te)

    # Platt (sigmoid calibration via LR on the ensemble probability)
    # Build logit feature so LR fits a sigmoid in probability space
    ens_v_clipped = np.clip(ens_v, 1e-4, 1-1e-4)
    val_logit = np.log(ens_v_clipped / (1 - ens_v_clipped))
    platt = LogisticRegression().fit(val_logit.reshape(-1,1), yv)
    te_clipped = np.clip(ens_te, 1e-4, 1-1e-4)
    te_logit = np.log(te_clipped / (1 - te_clipped))
    platt_te = platt.predict_proba(te_logit.reshape(-1,1))[:,1]

    return dict(raw=ens_te, iso=iso_te, platt=platt_te)


def main():
    df = load(); df = engineer(df)
    print(f"Total games: {len(df)}  date range: {df['game_date'].min().date()}..{df['game_date'].max().date()}")

    # Folds: each test = full year (except 2021 which is too small).
    # Within each fold, val = last 6 weeks of training data.
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
        print(f"\nFold test={test_year}: train={len(train)} val={len(val)} test={len(test)}")
        preds = fit_fold(train, val, test)
        rec = test.copy()
        rec["p_raw"] = preds["raw"]
        rec["p_iso"] = preds["iso"]
        rec["p_platt"] = preds["platt"]
        all_preds.append(rec)

        # Per-fold log-loss summary
        for name, p in [("market", test["p_home_fair"].values),
                        ("raw   ", preds["raw"]),
                        ("iso   ", preds["iso"]),
                        ("platt ", preds["platt"])]:
            ll = log_loss(test["y"].astype(int).values, np.clip(p, 1e-4, 1-1e-4))
            print(f"  log_loss  {name}: {ll:.4f}")

    pooled = pd.concat(all_preds, ignore_index=True)
    print(f"\n{'='*72}\nPOOLED OOF SAMPLE: {len(pooled)} games (genuinely out-of-sample)\n{'='*72}")

    # Pooled log-loss
    print("\nPooled log loss (lower is better):")
    for name, col in [("market         ", "p_home_fair"),
                      ("raw ensemble   ", "p_raw"),
                      ("isotonic       ", "p_iso"),
                      ("platt (sigmoid)", "p_platt")]:
        y = pooled["y"].astype(int).values
        ll = log_loss(y, np.clip(pooled[col].values, 1e-4, 1-1e-4))
        print(f"  {name}: {ll:.4f}")

    # Calibration spread
    print("\nUnique probability values (out of 10000 rounding to 4 decimals):")
    for name, col in [("raw   ", "p_raw"), ("iso   ", "p_iso"), ("platt ", "p_platt")]:
        u = len(np.unique(np.round(pooled[col].values, 4)))
        print(f"  {name}: {u}")

    # Backtest pooled OOF
    def collect_bets(df, prob_col, thr):
        bets = []
        p = df[prob_col].values; ph = df["p_home_fair"].values
        y = df["y"].values; mh = df["ml_home_close"].values; ma = df["ml_away_close"].values
        for i in range(len(df)):
            eh = p[i] - ph[i]; ea = (1-p[i]) - (1 - ph[i])
            if eh > thr: bets.append((mh[i], y[i] == 1))
            elif ea > thr: bets.append((ma[i], y[i] == 0))
        return bets

    def boot(bets, n_iter=10000):
        if not bets: return None
        pls = np.array([ml_payout(ml, w) for ml, w in bets])
        n = len(pls); roi = pls.mean()/STAKE
        boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1)/STAKE
        return n, 100*roi, 100*boots.std(), float((boots <= 0).mean())

    print("\nPooled OOF backtest (single number per row, no cherry-picking):")
    for cal_name, prob_col in [("raw_ens", "p_raw"), ("isotonic", "p_iso"), ("platt", "p_platt")]:
        print(f"\n  Calibration: {cal_name}")
        print(f"  {'Thr':>6} {'Bets':>6} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>9} {'StdROI':>8} {'P(<=0)':>8}")
        for thr in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
            bets = collect_bets(pooled, prob_col, thr)
            r = boot(bets)
            if r is None: continue
            n, roi, sd, p = r
            wins = sum(1 for _, w in bets if w)
            profit = sum(ml_payout(ml, w) for ml, w in bets)
            avg_ml = np.mean([ml for ml, _ in bets])
            wp = 100*wins/n
            print(f"  {thr:>6.2f} {n:>6} {wp:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+8.2f}% {sd:>7.2f}% {p:>8.3f}")


if __name__ == "__main__":
    main()
