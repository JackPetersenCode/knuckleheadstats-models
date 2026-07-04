"""V3 training: full feature set with bullpen, Pythagorean, line movement.

Models: Logistic, sklearn GBM, XGBoost, LightGBM. Plus calibrated ensemble.
Evaluation: log loss vs market, ROI by edge bucket, bootstrap significance,
Kelly bankroll evolution.
"""
import os
import numpy as np
import pandas as pd
import psycopg2
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.metrics import log_loss, brier_score_loss
from sklearn.isotonic import IsotonicRegression
import xgboost as xgb
import lightgbm as lgb

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
STAKE = 100.0
RNG = np.random.default_rng(42)


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else abs(ml) / (abs(ml) + 100.0)


def ml_payout(ml, won):
    if not won: return -STAKE
    return STAKE * (ml / 100.0) if ml > 0 else STAKE * (100.0 / abs(ml))


def load_data():
    pg = psycopg2.connect(**PG)
    df = pd.read_sql("""
        SELECT *,
               EXTRACT(YEAR FROM game_date)::int AS year
        FROM mlb_features_v2
        WHERE ml_home_close IS NOT NULL AND ml_away_close IS NOT NULL
          AND h_p_ip_10 IS NOT NULL AND a_p_ip_10 IS NOT NULL
          AND h_rdiff_30 IS NOT NULL AND a_rdiff_30 IS NOT NULL
          AND h_bp_ip_14 IS NOT NULL AND a_bp_ip_14 IS NOT NULL
        ORDER BY game_date
    """, pg)
    pg.close()
    return df


def engineer(df):
    def per9(num, ip):
        return np.where((ip > 0) & ip.notna(), num * 9.0 / ip, np.nan)

    # Starter rate stats (rolling 10 starts)
    df["h_p_era_10"] = per9(df["h_p_er_10"], df["h_p_ip_10"])
    df["h_p_k9_10"]  = per9(df["h_p_k_10"],  df["h_p_ip_10"])
    df["h_p_bb9_10"] = per9(df["h_p_bb_10"], df["h_p_ip_10"])
    df["h_p_hr9_10"] = per9(df["h_p_hr_10"], df["h_p_ip_10"])
    df["h_p_ipgs_10"] = df["h_p_ip_10"] / df["h_p_starts_10"]
    df["a_p_era_10"] = per9(df["a_p_er_10"], df["a_p_ip_10"])
    df["a_p_k9_10"]  = per9(df["a_p_k_10"],  df["a_p_ip_10"])
    df["a_p_bb9_10"] = per9(df["a_p_bb_10"], df["a_p_ip_10"])
    df["a_p_hr9_10"] = per9(df["a_p_hr_10"], df["a_p_ip_10"])
    df["a_p_ipgs_10"] = df["a_p_ip_10"] / df["a_p_starts_10"]

    # Season-to-date pitcher
    df["h_p_era_sd"] = per9(df["h_p_er_sd"], df["h_p_ip_sd"])
    df["h_p_k9_sd"]  = per9(df["h_p_k_sd"],  df["h_p_ip_sd"])
    df["h_p_bb9_sd"] = per9(df["h_p_bb_sd"], df["h_p_ip_sd"])
    df["h_p_hr9_sd"] = per9(df["h_p_hr_sd"], df["h_p_ip_sd"])
    df["a_p_era_sd"] = per9(df["a_p_er_sd"], df["a_p_ip_sd"])
    df["a_p_k9_sd"]  = per9(df["a_p_k_sd"],  df["a_p_ip_sd"])
    df["a_p_bb9_sd"] = per9(df["a_p_bb_sd"], df["a_p_ip_sd"])
    df["a_p_hr9_sd"] = per9(df["a_p_hr_sd"], df["a_p_ip_sd"])

    # Bullpen rate (14 days)
    df["h_bp_era_14"] = per9(df["h_bp_er_14"], df["h_bp_ip_14"])
    df["h_bp_k9_14"]  = per9(df["h_bp_k_14"],  df["h_bp_ip_14"])
    df["h_bp_bb9_14"] = per9(df["h_bp_bb_14"], df["h_bp_ip_14"])
    df["a_bp_era_14"] = per9(df["a_bp_er_14"], df["a_bp_ip_14"])
    df["a_bp_k9_14"]  = per9(df["a_bp_k_14"],  df["a_bp_ip_14"])
    df["a_bp_bb9_14"] = per9(df["a_bp_bb_14"], df["a_bp_ip_14"])

    # Bullpen fatigue (IP last 3 days as fraction of 14d total)
    df["h_bp_fatigue"] = df["h_bp_ip_3"] / df["h_bp_ip_14"].replace(0, np.nan)
    df["a_bp_fatigue"] = df["a_bp_ip_3"] / df["a_bp_ip_14"].replace(0, np.nan)

    # Market probs
    df["p_home_close_raw"] = df["ml_home_close"].apply(american_to_prob)
    df["p_away_close_raw"] = df["ml_away_close"].apply(american_to_prob)
    df["overround_close"] = df["p_home_close_raw"] + df["p_away_close_raw"]
    df["p_home_fair"] = df["p_home_close_raw"] / df["overround_close"]
    df["p_away_fair"] = df["p_away_close_raw"] / df["overround_close"]
    df["mkt_logit"] = np.log(df["p_home_fair"] / (1 - df["p_home_fair"]))

    # Opening line + movement
    df["p_home_open_raw"] = df["ml_home_open"].apply(american_to_prob)
    df["p_away_open_raw"] = df["ml_away_open"].apply(american_to_prob)
    df["overround_open"] = df["p_home_open_raw"] + df["p_away_open_raw"]
    df["p_home_open_fair"] = df["p_home_open_raw"] / df["overround_open"]
    df["open_logit"] = np.log(df["p_home_open_fair"] / (1 - df["p_home_open_fair"]))
    df["line_move"] = df["mkt_logit"] - df["open_logit"]   # +ve = home line moved toward home

    # Differential features (often more useful than raw)
    df["d_pyth"] = df["h_pyth"] - df["a_pyth"]
    df["d_wpct_7"] = df["h_wpct_7"] - df["a_wpct_7"]
    df["d_rdiff_30"] = df["h_rdiff_30"] - df["a_rdiff_30"]
    df["d_starter_era_sd"] = df["a_p_era_sd"] - df["h_p_era_sd"]  # +ve favors home
    df["d_starter_k9_sd"]  = df["h_p_k9_sd"] - df["a_p_k9_sd"]
    df["d_starter_bb9_sd"] = df["a_p_bb9_sd"] - df["h_p_bb9_sd"]
    df["d_bp_era_14"]      = df["a_bp_era_14"] - df["h_bp_era_14"]
    df["d_bp_fatigue"]     = df["a_bp_fatigue"] - df["h_bp_fatigue"]
    df["d_wpct_home_away"] = df["h_wpct_home"] - df["a_wpct_away"]

    df["is_night"] = df["is_night"].astype(float)
    return df


FEATS_STATS = [
    "is_night", "park_rpg", "series_game_number",
    "h_dayafternight", "a_dayafternight",
    # Team form
    "h_wpct_7", "h_rdiff_14", "h_rdiff_30", "h_rs_30", "h_ra_30",
    "a_wpct_7", "a_rdiff_14", "a_rdiff_30", "a_rs_30", "a_ra_30",
    "h_pyth", "a_pyth", "h_wpct_home", "a_wpct_away",
    # Starter (10-start window)
    "h_p_era_10", "h_p_k9_10", "h_p_bb9_10", "h_p_hr9_10", "h_p_ipgs_10",
    "h_p_rest", "h_p_starts_10",
    "a_p_era_10", "a_p_k9_10", "a_p_bb9_10", "a_p_hr9_10", "a_p_ipgs_10",
    "a_p_rest", "a_p_starts_10",
    # Starter (season-to-date)
    "h_p_era_sd", "h_p_k9_sd", "h_p_bb9_sd", "h_p_hr9_sd", "h_p_starts_sd",
    "a_p_era_sd", "a_p_k9_sd", "a_p_bb9_sd", "a_p_hr9_sd", "a_p_starts_sd",
    # Bullpen
    "h_bp_era_14", "h_bp_k9_14", "h_bp_bb9_14", "h_bp_fatigue", "h_bp_ip_14",
    "a_bp_era_14", "a_bp_k9_14", "a_bp_bb9_14", "a_bp_fatigue", "a_bp_ip_14",
    # Differentials
    "d_pyth", "d_wpct_7", "d_rdiff_30",
    "d_starter_era_sd", "d_starter_k9_sd", "d_starter_bb9_sd",
    "d_bp_era_14", "d_bp_fatigue", "d_wpct_home_away",
]
FEATS_PLUS_MKT = FEATS_STATS + ["mkt_logit", "open_logit", "line_move"]


def evaluate(name, y, p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    ll = log_loss(y, p)
    br = brier_score_loss(y, p)
    print(f"  {name:<34} log_loss={ll:.4f}  brier={br:.4f}  n={len(p)}")
    return ll, br


def backtest_row(df, prob_col, thr):
    bets = []
    for _, r in df.iterrows():
        eh = r[prob_col] - r["p_home_fair"]
        ea = (1 - r[prob_col]) - r["p_away_fair"]
        if eh > thr:
            bets.append((r["ml_home_close"], r["y"] == 1, "home"))
        elif ea > thr:
            bets.append((r["ml_away_close"], r["y"] == 0, "away"))
    return bets


def summarize(label, bets):
    if not bets:
        return None
    n = len(bets)
    wins = sum(1 for _, w, _ in bets if w)
    profit = sum(ml_payout(ml, w) for ml, w, _ in bets)
    avg_ml = np.mean([ml for ml, _, _ in bets])
    wpct = 100 * wins / n
    roi = 100 * profit / (n * STAKE)
    return n, wins, wpct, avg_ml, profit, roi


def backtest_table(df, prob_col, thresholds=(0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)):
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8}")
    for thr in thresholds:
        bets = backtest_row(df, prob_col, thr)
        s = summarize("", bets)
        if s is None: continue
        n, w, wp, ml, p, roi = s
        print(f"  {thr:>6.2f} {n:>6} {w:>5} {wp:>6.1f}% {ml:>+8.0f} {p:>+10.0f} {roi:>+7.2f}%")


def bootstrap_pvalue(bets, n_iter=4000):
    if not bets: return None
    pls = np.array([ml_payout(ml, w) for ml, w, _ in bets])
    n = len(pls)
    roi = pls.mean() / STAKE
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1) / STAKE
    return roi, n, boots.mean(), boots.std(), float((boots <= 0).mean())


def fit_models(Xtr, ytr, Xva, yva):
    # Imputation
    imp = SimpleImputer(strategy="median").fit(Xtr)
    Xtr_i = imp.transform(Xtr); Xva_i = imp.transform(Xva)
    sc = StandardScaler().fit(Xtr_i)
    Xtr_s = sc.transform(Xtr_i); Xva_s = sc.transform(Xva_i)

    lr = LogisticRegression(C=0.3, max_iter=4000).fit(Xtr_s, ytr)
    p_lr = lr.predict_proba(Xva_s)[:, 1]

    gbm = GradientBoostingClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.04,
        subsample=0.8, random_state=42).fit(Xtr_i, ytr)
    p_gbm = gbm.predict_proba(Xva_i)[:, 1]

    xgbm = xgb.XGBClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.03,
        subsample=0.85, colsample_bytree=0.85, reg_lambda=1.0,
        objective="binary:logistic", eval_metric="logloss",
        random_state=42, n_jobs=4, tree_method="hist").fit(Xtr_i, ytr)
    p_xgb = xgbm.predict_proba(Xva_i)[:, 1]

    lgbm = lgb.LGBMClassifier(
        n_estimators=400, max_depth=-1, num_leaves=31, learning_rate=0.03,
        subsample=0.85, colsample_bytree=0.85, reg_lambda=1.0,
        random_state=42, n_jobs=4, verbose=-1).fit(Xtr_i, ytr)
    p_lgb = lgbm.predict_proba(Xva_i)[:, 1]

    p_ens = (p_lr + p_gbm + p_xgb + p_lgb) / 4
    return dict(LR=p_lr, GBM=p_gbm, XGB=p_xgb, LGB=p_lgb, ENS=p_ens), \
           dict(LR=lr, GBM=gbm, XGB=xgbm, LGB=lgbm, IMP=imp, SC=sc)


def main():
    df = load_data()
    df = engineer(df)
    print(f"Total rows with full features + odds: {len(df)}")
    print(f"  with open line: {df['ml_home_open'].notna().sum()}")

    train = df[df["year"] <= 2023].copy()
    test  = df[df["year"] == 2024].copy()
    hold  = df[df["year"] == 2025].copy()
    print(f"Train (2021-2023): {len(train)}")
    print(f"Test  (2024):       {len(test)}")
    print(f"Hold  (2025):       {len(hold)}")

    print("\n" + "="*60)
    print("VARIANT A: stats only (no market info)")
    print("="*60)
    Xtr = train[FEATS_STATS].values; ytr = train["y"].astype(int).values
    Xte = test[FEATS_STATS].values;  yte = test["y"].astype(int).values
    Xho = hold[FEATS_STATS].values;  yho = hold["y"].astype(int).values

    probs_te, _ = fit_models(Xtr, ytr, Xte, yte)
    probs_ho, _ = fit_models(Xtr, ytr, Xho, yho)

    print("\n--- TEST (2024) ---")
    evaluate("Market (closing)", yte, test["p_home_fair"].values)
    for name, p in probs_te.items():
        evaluate(name, yte, p)
    print("\n  Backtest ENS on TEST 2024:")
    test["p_ens"] = probs_te["ENS"]
    backtest_table(test, "p_ens")

    print("\n--- HOLDOUT (2025) ---")
    evaluate("Market (closing)", yho, hold["p_home_fair"].values)
    for name, p in probs_ho.items():
        evaluate(name, yho, p)
    print("\n  Backtest ENS on HOLDOUT 2025:")
    hold["p_ens"] = probs_ho["ENS"]
    backtest_table(hold, "p_ens")

    print("\n" + "="*60)
    print("VARIANT B: stats + market (residual learner)")
    print("="*60)
    Xtr = train[FEATS_PLUS_MKT].values
    Xte = test[FEATS_PLUS_MKT].values
    Xho = hold[FEATS_PLUS_MKT].values
    # Filter rows where opening line missing
    valid_te = test["mkt_logit"].notna().values & test["open_logit"].notna().values
    valid_ho = hold["mkt_logit"].notna().values & hold["open_logit"].notna().values
    valid_tr = train["mkt_logit"].notna().values & train["open_logit"].notna().values

    probs_te, _ = fit_models(Xtr[valid_tr], ytr[valid_tr], Xte[valid_te], yte[valid_te])
    probs_ho, _ = fit_models(Xtr[valid_tr], ytr[valid_tr], Xho[valid_ho], yho[valid_ho])

    test_v = test.iloc[valid_te].copy()
    hold_v = hold.iloc[valid_ho].copy()

    print(f"\nTrain valid: {valid_tr.sum()} / {len(train)}")
    print(f"Test valid:  {valid_te.sum()} / {len(test)}")
    print(f"Hold valid:  {valid_ho.sum()} / {len(hold)}")

    print("\n--- TEST (2024) with market features ---")
    evaluate("Market (closing)", yte[valid_te], test_v["p_home_fair"].values)
    for name, p in probs_te.items():
        evaluate(name, yte[valid_te], p)
    test_v["p_ens"] = probs_te["ENS"]
    print("\n  Backtest ENS on TEST 2024:")
    backtest_table(test_v, "p_ens")

    print("\n--- HOLDOUT (2025) with market features ---")
    evaluate("Market (closing)", yho[valid_ho], hold_v["p_home_fair"].values)
    for name, p in probs_ho.items():
        evaluate(name, yho[valid_ho], p)
    hold_v["p_ens"] = probs_ho["ENS"]
    print("\n  Backtest ENS on HOLDOUT 2025:")
    backtest_table(hold_v, "p_ens")

    # Combined test+hold result for any positive-edge slice
    print("\n=== Combined TEST+HOLD significance (Variant B, ENS, thr=0.03) ===")
    combined = pd.concat([test_v.assign(p_ens=probs_te["ENS"]),
                          hold_v.assign(p_ens=probs_ho["ENS"])], ignore_index=True)
    for thr in [0.02, 0.03, 0.04, 0.05]:
        bets = backtest_row(combined, "p_ens", thr)
        s = summarize("", bets)
        if s is None: continue
        n, w, wp, ml, p, roi = s
        res = bootstrap_pvalue(bets)
        if res:
            r, nb, mu, sd, p_le_0 = res
            print(f"  thr={thr:.2f}  bets={nb}  ROI={100*r:+.2f}%  std={100*sd:.2f}%  P(true ROI<=0)={p_le_0:.3f}")


if __name__ == "__main__":
    main()
