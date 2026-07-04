"""V5 training: adds weather + umpire + lineup-aware batting to v4 features.

Same pipeline as v4 (XGB + LGB + LR ensemble, early stopping on val, isotonic cal).
"""
import os
import numpy as np
import pandas as pd
import psycopg2
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import log_loss, brier_score_loss
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
    return df


def engineer(df):
    def per9(num, ip):
        return np.where((ip > 0) & ip.notna(), num*9.0/ip, np.nan)

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

    df["h_p_era_sd"] = per9(df["h_p_er_sd"], df["h_p_ip_sd"])
    df["h_p_k9_sd"]  = per9(df["h_p_k_sd"],  df["h_p_ip_sd"])
    df["h_p_bb9_sd"] = per9(df["h_p_bb_sd"], df["h_p_ip_sd"])
    df["h_p_hr9_sd"] = per9(df["h_p_hr_sd"], df["h_p_ip_sd"])
    df["a_p_era_sd"] = per9(df["a_p_er_sd"], df["a_p_ip_sd"])
    df["a_p_k9_sd"]  = per9(df["a_p_k_sd"],  df["a_p_ip_sd"])
    df["a_p_bb9_sd"] = per9(df["a_p_bb_sd"], df["a_p_ip_sd"])
    df["a_p_hr9_sd"] = per9(df["a_p_hr_sd"], df["a_p_ip_sd"])

    df["h_bp_era_14"] = per9(df["h_bp_er_14"], df["h_bp_ip_14"])
    df["h_bp_k9_14"]  = per9(df["h_bp_k_14"],  df["h_bp_ip_14"])
    df["h_bp_bb9_14"] = per9(df["h_bp_bb_14"], df["h_bp_ip_14"])
    df["a_bp_era_14"] = per9(df["a_bp_er_14"], df["a_bp_ip_14"])
    df["a_bp_k9_14"]  = per9(df["a_bp_k_14"],  df["a_bp_ip_14"])
    df["a_bp_bb9_14"] = per9(df["a_bp_bb_14"], df["a_bp_ip_14"])

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
    df["mq_h_pitch_vs_a_bat"] = df["h_p_whiff_rate"] - df["a_bat_woba"]
    df["mq_a_pitch_vs_h_bat"] = df["a_p_whiff_rate"] - df["h_bat_woba"]

    # NEW v5 diffs
    df["d_lineup_woba"]  = df["h_lineup_woba"] - df["a_lineup_woba"]
    df["d_lineup_iso"]   = df["h_lineup_iso"] - df["a_lineup_iso"]
    df["d_lineup_k"]     = df["a_lineup_k_rate"] - df["h_lineup_k_rate"]
    df["d_lineup_bb"]    = df["h_lineup_bb_rate"] - df["a_lineup_bb_rate"]
    # Lineup vs starter quality matchups
    df["mq_h_p_vs_a_lineup"] = df["h_p_whiff_rate"] - df["a_lineup_woba"]
    df["mq_a_p_vs_h_lineup"] = df["a_p_whiff_rate"] - df["h_lineup_woba"]
    # Weather interactions
    df["wind_x_helps_hitter"] = df["wind_helps_hitter"] * df["wind_mph"]
    df["wind_x_helps_pitcher"] = df["wind_helps_pitcher"] * df["wind_mph"]
    df["cold_temp"] = (df["temp_f"] < 50).astype(float)
    df["hot_temp"]  = (df["temp_f"] > 85).astype(float)

    df["is_night"] = df["is_night"].astype(float)
    return df


FEATURES = [
    "is_night", "park_rpg", "series_game_number",
    "h_dayafternight", "a_dayafternight",
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
    # NEW v5 weather
    "temp_f","wind_mph","is_dome","wind_helps_hitter","wind_helps_pitcher","weather_clear",
    "wind_x_helps_hitter","wind_x_helps_pitcher","cold_temp","hot_temp",
    # NEW v5 umpire
    "ump_k_rate","ump_bb_rate","ump_runs_pg","ump_n_games",
    # NEW v5 lineup
    "h_lineup_woba","h_lineup_iso","h_lineup_k_rate","h_lineup_bb_rate","h_lineup_n_batters",
    "a_lineup_woba","a_lineup_iso","a_lineup_k_rate","a_lineup_bb_rate","a_lineup_n_batters",
    "d_lineup_woba","d_lineup_iso","d_lineup_k","d_lineup_bb",
    "mq_h_p_vs_a_lineup","mq_a_p_vs_h_lineup",
]
FEATURES_MKT = FEATURES + ["mkt_logit","open_logit","line_move"]


def evaluate(name, y, p):
    p = np.clip(p, 1e-4, 1-1e-4)
    ll = log_loss(y, p); br = brier_score_loss(y, p)
    print(f"  {name:<36} log_loss={ll:.4f}  brier={br:.4f}  n={len(p)}")
    return ll


def collect_bets(df, prob_col, thr):
    bets = []
    p = df[prob_col].values; ph = df["p_home_fair"].values; pa = df["p_away_fair"].values
    y = df["y"].values; mh = df["ml_home_close"].values; ma = df["ml_away_close"].values
    for i in range(len(df)):
        eh = p[i] - ph[i]; ea = (1-p[i]) - pa[i]
        if eh > thr: bets.append((mh[i], y[i] == 1, "home"))
        elif ea > thr: bets.append((ma[i], y[i] == 0, "away"))
    return bets


def boot(bets, n_iter=10000):
    if not bets: return None
    pls = np.array([ml_payout(ml, w) for ml, w, _ in bets])
    n = len(pls); roi = pls.mean()/STAKE
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1)/STAKE
    return n, 100*roi, 100*boots.std(), float((boots <= 0).mean())


def backtest_table(df, prob_col, thresholds=(0.00, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12)):
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'Win%':>7} {'Profit':>10} {'ROI%':>8} {'P<=0':>7}")
    for thr in thresholds:
        bets = collect_bets(df, prob_col, thr)
        r = boot(bets, n_iter=4000)
        if not r: continue
        n, roi, sd, p = r
        wins = sum(1 for _, w, _ in bets if w)
        profit = sum(ml_payout(ml, w) for ml, w, _ in bets)
        wpct = 100*wins/n
        print(f"  {thr:>6.2f} {n:>6} {wins:>5} {wpct:>6.1f}% {profit:>+10.0f} {roi:>+7.2f}% {p:>7.3f}")


def fit_predict(Xtr, ytr, Xva, yva, Xtest_dict):
    imp = SimpleImputer(strategy="median").fit(Xtr)
    Xtr_i = imp.transform(Xtr); Xva_i = imp.transform(Xva)
    Xte_i = {k: imp.transform(v) for k, v in Xtest_dict.items()}
    sc = StandardScaler().fit(Xtr_i)
    Xtr_s = sc.transform(Xtr_i); Xva_s = sc.transform(Xva_i)
    Xte_s = {k: sc.transform(v) for k, v in Xte_i.items()}

    out = {}
    lr = LogisticRegression(C=0.15, max_iter=5000).fit(Xtr_s, ytr)
    out["LR"] = {"val": lr.predict_proba(Xva_s)[:,1]}
    for k, v in Xte_s.items(): out["LR"][k] = lr.predict_proba(v)[:,1]

    xgbm = xgb.XGBClassifier(
        n_estimators=1000, max_depth=4, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_weight=8, objective="binary:logistic", eval_metric="logloss",
        early_stopping_rounds=40, random_state=42, n_jobs=4, tree_method="hist")
    xgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], verbose=False)
    out["XGB"] = {"val": xgbm.predict_proba(Xva_i)[:,1]}
    for k, v in Xte_i.items(): out["XGB"][k] = xgbm.predict_proba(v)[:,1]

    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, max_depth=-1, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], callbacks=[lgb.early_stopping(40)])
    out["LGB"] = {"val": lgbm.predict_proba(Xva_i)[:,1]}
    for k, v in Xte_i.items(): out["LGB"][k] = lgbm.predict_proba(v)[:,1]

    out["ENS"] = {sk: (out["LR"][sk] + out["XGB"][sk] + out["LGB"][sk]) / 3
                  for sk in ["val"] + list(Xtest_dict.keys())}
    iso = IsotonicRegression(out_of_bounds="clip").fit(out["ENS"]["val"], yva)
    out["ENS_CAL"] = {sk: iso.transform(out["ENS"][sk]) for sk in out["ENS"]}
    return out


def main():
    df = load(); df = engineer(df)
    print(f"Total games with full v5 features + odds: {len(df)}")
    df["month"] = pd.to_datetime(df["game_date"]).dt.month
    train = df[df["year"] <= 2023].copy()
    val   = df[(df["year"] == 2024) & (df["month"] <= 6)].copy()
    test  = df[(df["year"] == 2024) & (df["month"] >= 7)].copy()
    hold  = df[df["year"] == 2025].copy()
    print(f"  train={len(train)} val={len(val)} test={len(test)} hold={len(hold)}")

    yt = train["y"].astype(int).values
    yv = val["y"].astype(int).values
    yte = test["y"].astype(int).values
    yh = hold["y"].astype(int).values

    for label, feat_set in [("STATS-ONLY", FEATURES), ("STATS+MARKET", FEATURES_MKT)]:
        print("\n" + "="*72)
        print(f"VARIANT: {label}  ({len(feat_set)} features)")
        print("="*72)
        Xt = train[feat_set].values; Xv = val[feat_set].values
        Xte_dict = {"test": test[feat_set].values, "hold": hold[feat_set].values}

        out = fit_predict(Xt, yt, Xv, yv, Xte_dict)

        print("\nLog loss vs market:")
        evaluate("Market", yte, test["p_home_fair"].values)
        for n in ["LR","XGB","LGB","ENS","ENS_CAL"]:
            evaluate(f"{n} TEST", yte, out[n]["test"])
        print()
        evaluate("Market", yh, hold["p_home_fair"].values)
        for n in ["LR","XGB","LGB","ENS","ENS_CAL"]:
            evaluate(f"{n} HOLD", yh, out[n]["hold"])

        for use_name in ["ENS", "ENS_CAL"]:
            test_w = test.copy(); test_w["p"] = out[use_name]["test"]
            hold_w = hold.copy(); hold_w["p"] = out[use_name]["hold"]

            print(f"\nBacktest {use_name} on TEST 2024 H2:")
            backtest_table(test_w, "p")
            print(f"\nBacktest {use_name} on HOLD 2025:")
            backtest_table(hold_w, "p")

            print(f"\nCombined {use_name}:")
            combined = pd.concat([test_w, hold_w], ignore_index=True)
            backtest_table(combined, "p")


if __name__ == "__main__":
    main()
