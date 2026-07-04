"""V6 with feature selection: pick top-K features by XGB gain on training data only,
then train final model on the selection. Rolling-origin CV across 3 folds.

For each fold:
  1) Train a "screener" XGB on (train, val=val) to estimate feature gains
  2) Pick the top K features
  3) Train final LR + XGB + LGB ensemble on those K only
  4) Evaluate on TEST and HOLD
  5) Compare ROI at thr=0.06, 0.08 to v5
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
TOP_K = 50  # number of features to keep after selection


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
        FROM mlb_features_v6
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

    diffs = {
        "d_pyth": ("h_pyth", "a_pyth"),
        "d_wpct_7": ("h_wpct_7", "a_wpct_7"),
        "d_rdiff_30": ("h_rdiff_30", "a_rdiff_30"),
        "d_starter_era_sd": ("a_p_era_sd", "h_p_era_sd"),
        "d_starter_k9_sd": ("h_p_k9_sd", "a_p_k9_sd"),
        "d_starter_bb9_sd": ("a_p_bb9_sd", "h_p_bb9_sd"),
        "d_bp_era_14": ("a_bp_era_14", "h_bp_era_14"),
        "d_bp_fatigue": ("a_bp_fatigue", "h_bp_fatigue"),
        "d_wpct_home_away": ("h_wpct_home", "a_wpct_away"),
        "d_p_ff_velo": ("h_p_ff_velo", "a_p_ff_velo"),
        "d_p_velo": ("h_p_velo", "a_p_velo"),
        "d_p_spin": ("h_p_spin", "a_p_spin"),
        "d_p_whiff": ("h_p_whiff_rate", "a_p_whiff_rate"),
        "d_p_strike": ("h_p_strike_rate", "a_p_strike_rate"),
        "d_bat_woba": ("h_bat_woba", "a_bat_woba"),
        "d_bat_iso": ("h_bat_iso", "a_bat_iso"),
        "d_bat_k": ("a_bat_k", "h_bat_k"),
        "d_bat_bb": ("h_bat_bb", "a_bat_bb"),
        "d_bat_hr": ("h_bat_hr", "a_bat_hr"),
        "d_lineup_woba": ("h_lineup_woba", "a_lineup_woba"),
        "d_lineup_iso": ("h_lineup_iso", "a_lineup_iso"),
        "d_lineup_k": ("a_lineup_k_rate", "h_lineup_k_rate"),
        "d_lineup_bb": ("h_lineup_bb_rate", "a_lineup_bb_rate"),
        "d_lineup_woba_vsh": ("h_lineup_woba_vs_hand", "a_lineup_woba_vs_hand"),
        "d_l10_wpct": ("h_l10_wpct", "a_l10_wpct"),
        "d_l10_rdiff": ("h_l10_rdiff", "a_l10_rdiff"),
        "d_streak": ("h_streak", "a_streak"),
        "d_api_streak": ("h_api_streak", "a_api_streak"),
        "d_gb": ("a_gb", "h_gb"),
        "d_pct_ff": ("h_p_pct_ff", "a_p_pct_ff"),
        "d_pct_breaking": ("h_p_pct_breaking", "a_p_pct_breaking"),
        "d_pct_offspeed": ("h_p_pct_offspeed", "a_p_pct_offspeed"),
    }
    for k, (a, b) in diffs.items():
        df[k] = df[a] - df[b]

    df["mq_h_pitch_vs_a_bat"] = df["h_p_whiff_rate"] - df["a_bat_woba"]
    df["mq_a_pitch_vs_h_bat"] = df["a_p_whiff_rate"] - df["h_bat_woba"]
    df["mq_h_p_vs_a_lineup"] = df["h_p_whiff_rate"] - df["a_lineup_woba"]
    df["mq_a_p_vs_h_lineup"] = df["a_p_whiff_rate"] - df["h_lineup_woba"]
    df["mq_h_p_vs_a_lineup_vsh"] = df["h_p_whiff_rate"] - df["a_lineup_woba_vs_hand"]
    df["mq_a_p_vs_h_lineup_vsh"] = df["a_p_whiff_rate"] - df["h_lineup_woba_vs_hand"]

    df["wind_x_helps_hitter"] = df["wind_helps_hitter"] * df["wind_mph"]
    df["wind_x_helps_pitcher"] = df["wind_helps_pitcher"] * df["wind_mph"]
    df["cold_temp"] = (df["temp_f"] < 50).astype(float)
    df["hot_temp"]  = (df["temp_f"] > 85).astype(float)
    df["is_night"] = df["is_night"].astype(float)
    return df


ALL_FEATURES = [
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
    # v6
    "h_lineup_woba_vs_hand","a_lineup_woba_vs_hand","d_lineup_woba_vsh",
    "h_p_pct_ff","h_p_pct_si","h_p_pct_sl","h_p_pct_offspeed","h_p_pct_breaking","h_p_pct_fc",
    "a_p_pct_ff","a_p_pct_si","a_p_pct_sl","a_p_pct_offspeed","a_p_pct_breaking","a_p_pct_fc",
    "d_pct_ff","d_pct_breaking","d_pct_offspeed",
    "h_p_home_era","h_p_away_era","a_p_home_era","a_p_away_era",
    "h_l10_wpct","h_l10_rdiff","a_l10_wpct","a_l10_rdiff",
    "h_streak","a_streak","d_l10_wpct","d_l10_rdiff","d_streak",
    "h2h_n","h2h_home_wpct",
    "h_gb","a_gb","h_api_streak","a_api_streak","d_gb","d_api_streak",
    "mq_h_p_vs_a_lineup_vsh","mq_a_p_vs_h_lineup_vsh",
    "mkt_logit","open_logit","line_move",
]


def split_fold(df, fold_id):
    d = df["game_date"]
    if fold_id == "A":
        return (df[d.dt.year <= 2022].copy(),
                df[(d.dt.year == 2023) & (d.dt.month <= 6)].copy(),
                df[(d.dt.year == 2023) & (d.dt.month >= 7)].copy(),
                df[d.dt.year >= 2024].copy())
    if fold_id == "B":
        return (df[(d.dt.year <= 2022) | ((d.dt.year == 2023) & (d.dt.month <= 6))].copy(),
                df[(d.dt.year == 2023) & (d.dt.month >= 7)].copy(),
                df[(d.dt.year == 2024) & (d.dt.month <= 6)].copy(),
                df[((d.dt.year == 2024) & (d.dt.month >= 7)) | (d.dt.year == 2025)].copy())
    if fold_id == "C":
        return (df[d.dt.year <= 2023].copy(),
                df[(d.dt.year == 2024) & (d.dt.month <= 6)].copy(),
                df[(d.dt.year == 2024) & (d.dt.month >= 7)].copy(),
                df[d.dt.year == 2025].copy())


def select_top_features(Xtr, ytr, Xva, yva, features, k):
    """Use a quick XGB on train+val to pick top-k features by gain importance.
    To avoid leakage we fit only on train, evaluate on val for early stopping."""
    imp = SimpleImputer(strategy="median").fit(Xtr)
    Xtr_i = imp.transform(Xtr); Xva_i = imp.transform(Xva)
    screener = xgb.XGBClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.85, colsample_bytree=0.7,
        early_stopping_rounds=30, eval_metric="logloss",
        random_state=42, n_jobs=4, tree_method="hist")
    screener.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], verbose=False)
    gain = screener.feature_importances_
    ranked = sorted(zip(features, gain), key=lambda x: -x[1])
    return [name for name, _ in ranked[:k]], ranked


def fit_ens(Xtr, ytr, Xva, yva, Xte_dict):
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
        min_child_weight=8, eval_metric="logloss",
        early_stopping_rounds=40, random_state=42, n_jobs=4, tree_method="hist")
    xgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], verbose=False)
    lgbm = lgb.LGBMClassifier(
        n_estimators=1000, num_leaves=31, learning_rate=0.02,
        subsample=0.85, colsample_bytree=0.7, reg_lambda=2.0, reg_alpha=0.5,
        min_child_samples=20, random_state=42, n_jobs=4, verbose=-1)
    lgbm.fit(Xtr_i, ytr, eval_set=[(Xva_i, yva)], callbacks=[lgb.early_stopping(40)])
    out = {}
    for sk, vi, vs in [("val", Xva_i, Xva_s)] + [(k, Xte_i[k], Xte_s[k]) for k in Xte_dict]:
        out[sk] = (lr.predict_proba(vs)[:,1] + xgbm.predict_proba(vi)[:,1] + lgbm.predict_proba(vi)[:,1]) / 3
    iso = IsotonicRegression(out_of_bounds="clip").fit(out["val"], yva)
    return {sk: iso.transform(p) for sk, p in out.items()}


def collect_bets(df, prob_col, thr):
    bets = []
    p = df[prob_col].values; ph = df["p_home_fair"].values; pa = df["p_away_fair"].values
    y = df["y"].values; mh = df["ml_home_close"].values; ma = df["ml_away_close"].values
    for i in range(len(df)):
        eh = p[i] - ph[i]; ea = (1-p[i]) - pa[i]
        if eh > thr: bets.append((mh[i], y[i] == 1))
        elif ea > thr: bets.append((ma[i], y[i] == 0))
    return bets


def boot_roi(bets, n_iter=4000):
    if not bets: return None
    pls = np.array([ml_payout(ml, w) for ml, w in bets])
    n = len(pls); roi = pls.mean()/STAKE
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1)/STAKE
    return n, 100*roi, float((boots <= 0).mean())


def main():
    df = load(); df = engineer(df)
    print(f"Total v6 games: {len(df)}")

    summary = []  # (fold, thr, ROI on T+H)

    for fold in ("A", "B", "C"):
        print("\n" + "="*72); print(f"FOLD {fold} (top-{TOP_K} feature selection)"); print("="*72)
        train, val, test, hold = split_fold(df, fold)
        print(f"  train={len(train)} val={len(val)} test={len(test)} hold={len(hold)}")

        # Feature selection on train + early stopping on val
        Xt = train[ALL_FEATURES].values
        Xv = val[ALL_FEATURES].values
        yt = train["y"].astype(int).values
        yv = val["y"].astype(int).values
        selected, ranked = select_top_features(Xt, yt, Xv, yv, ALL_FEATURES, TOP_K)
        print(f"  Top {TOP_K} features selected. Top 10 by gain:")
        for name, gain in ranked[:10]:
            print(f"    {name:<30} {gain:.4f}")

        # Train final ensemble on selected features
        Xt_s = train[selected].values
        Xv_s = val[selected].values
        Xte_s = {"test": test[selected].values, "hold": hold[selected].values}
        probs = fit_ens(Xt_s, yt, Xv_s, yv, Xte_s)
        test["p"] = probs["test"]; hold["p"] = probs["hold"]

        # Log loss
        def ll(y, p): p = np.clip(p, 1e-4, 1-1e-4); return log_loss(y, p)
        print(f"\n  TEST log loss: market={ll(test['y'].values, test['p_home_fair'].values):.4f}  "
              f"model={ll(test['y'].values, test['p']):.4f}")
        print(f"  HOLD log loss: market={ll(hold['y'].values, hold['p_home_fair'].values):.4f}  "
              f"model={ll(hold['y'].values, hold['p']):.4f}")

        # Backtest at thresholds
        print(f"\n  Backtest results:")
        print(f"    {'Set':<8}{'Thr':>6}{'Bets':>6}{'ROI':>9}{'P<=0':>7}")
        for tag, dset in [("TEST", test), ("HOLD", hold), ("T+H", pd.concat([test, hold]))]:
            for thr in (0.04, 0.06, 0.08, 0.10):
                bets = collect_bets(dset, "p", thr)
                r = boot_roi(bets)
                if not r: continue
                n, roi, p = r
                print(f"    {tag:<8}{thr:>6.2f}{n:>6}{roi:>+8.2f}% {p:>6.3f}")
                if tag == "T+H":
                    summary.append((fold, thr, n, roi, p))

    print("\n" + "="*72)
    print(f"v6-selected (top-{TOP_K}) CROSS-FOLD T+H ROI vs v5 baseline")
    print("="*72)
    print(f"{'Thr':>6} {'v6 Fold A':>11} {'v6 Fold B':>11} {'v6 Fold C':>11} {'v6 avg':>9}  v5 avg (T+H, from prev run)")
    v5_th_avg = {0.04: -2.69, 0.06: -1.58, 0.08: +1.35, 0.10: +2.14}
    for thr in (0.04, 0.06, 0.08, 0.10):
        rois = [r[3] for r in summary if r[1] == thr]
        avg = np.mean(rois) if rois else float('nan')
        cells = [f"{r:+10.2f}%" for r in rois]
        while len(cells) < 3:
            cells.append("       --")
        print(f"  {thr:>4.2f} {cells[0]:>11} {cells[1]:>11} {cells[2]:>11} {avg:>+8.2f}%  "
              f"({v5_th_avg.get(thr, 0):+5.2f}%)")


if __name__ == "__main__":
    main()
