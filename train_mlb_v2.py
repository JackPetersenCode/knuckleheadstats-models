"""V2: stronger evaluation including market-residual model and full holdout baselines.

Adds:
  * Market-implied prob as input feature (best chance to beat market)
  * GB with market features included
  * All baselines on both test (2024) and holdout (2025)
  * Bootstrap significance on holdout ROI
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


def load_and_engineer():
    pg = psycopg2.connect(**PG)
    df = pd.read_sql("""
        SELECT game_pk, game_date, home_team_name, away_team_name, y,
               is_night, park_rpg,
               h_wpct, h_rs, h_ra, a_wpct, a_rs, a_ra,
               h_p_ip, h_p_er, h_p_k, h_p_bb, h_p_hr, h_p_starts, h_p_rest,
               a_p_ip, a_p_er, a_p_k, a_p_bb, a_p_hr, a_p_starts, a_p_rest,
               ml_home_close, ml_away_close
        FROM mlb_features
        WHERE ml_home_close IS NOT NULL AND ml_away_close IS NOT NULL
        ORDER BY game_date
    """, pg)
    pg.close()

    def per9(num, ip):
        return np.where((ip > 0) & ip.notna(), num * 9.0 / ip, np.nan)
    df["h_p_era"]  = per9(df["h_p_er"], df["h_p_ip"])
    df["h_p_k9"]   = per9(df["h_p_k"],  df["h_p_ip"])
    df["h_p_bb9"]  = per9(df["h_p_bb"], df["h_p_ip"])
    df["h_p_hr9"]  = per9(df["h_p_hr"], df["h_p_ip"])
    df["h_p_ipgs"] = df["h_p_ip"] / df["h_p_starts"]
    df["a_p_era"]  = per9(df["a_p_er"], df["a_p_ip"])
    df["a_p_k9"]   = per9(df["a_p_k"],  df["a_p_ip"])
    df["a_p_bb9"]  = per9(df["a_p_bb"], df["a_p_ip"])
    df["a_p_hr9"]  = per9(df["a_p_hr"], df["a_p_ip"])
    df["a_p_ipgs"] = df["a_p_ip"] / df["a_p_starts"]

    df["p_home_raw"] = df["ml_home_close"].apply(american_to_prob)
    df["p_away_raw"] = df["ml_away_close"].apply(american_to_prob)
    df["overround"]  = df["p_home_raw"] + df["p_away_raw"]
    df["p_home_fair"] = df["p_home_raw"] / df["overround"]
    df["p_away_fair"] = df["p_away_raw"] / df["overround"]
    # logit of market prob (better input than raw prob for tree splits)
    df["mkt_logit"] = np.log(df["p_home_fair"] / (1 - df["p_home_fair"]))

    df["year"] = pd.to_datetime(df["game_date"]).dt.year
    return df


FEATURES_BASE = [
    "is_night", "park_rpg",
    "h_wpct", "h_rs", "h_ra", "a_wpct", "a_rs", "a_ra",
    "h_p_era", "h_p_k9", "h_p_bb9", "h_p_hr9", "h_p_ipgs", "h_p_rest", "h_p_starts",
    "a_p_era", "a_p_k9", "a_p_bb9", "a_p_hr9", "a_p_ipgs", "a_p_rest", "a_p_starts",
]
FEATURES_PLUS_MKT = FEATURES_BASE + ["mkt_logit"]


def evaluate(name, y, p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    ll = log_loss(y, p)
    br = brier_score_loss(y, p)
    print(f"  {name:<32} log_loss={ll:.4f}  brier={br:.4f}  n={len(p)}")
    return ll, br


def backtest(df, prob_col, thresholds=(0.0, 0.02, 0.04, 0.06, 0.08)):
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'Win%':>7} {'AvgML':>8} {'Profit':>10} {'ROI%':>8}")
    for thr in thresholds:
        bets = []
        for _, r in df.iterrows():
            edge_home = r[prob_col] - r["p_home_fair"]
            edge_away = (1 - r[prob_col]) - r["p_away_fair"]
            if edge_home > thr:
                bets.append((r["ml_home_close"], r["y"] == 1))
            elif edge_away > thr:
                bets.append((r["ml_away_close"], r["y"] == 0))
        if not bets:
            continue
        n = len(bets)
        wins = sum(w for _, w in bets)
        profit = sum(ml_payout(ml, w) for ml, w in bets)
        wpct = 100.0 * wins / n
        roi = 100.0 * profit / (n * STAKE)
        avg_ml = np.mean([ml for ml, _ in bets])
        print(f"  {thr:>6.2f} {n:>6} {wins:>5} {wpct:>6.1f}% {avg_ml:>+8.0f} {profit:>+10.0f} {roi:>+7.2f}%")


def bootstrap_roi_pvalue(df, prob_col, thr, n_iter=2000):
    """Bootstrap: what is the probability of observing >= this ROI by chance,
    if the true ROI were the vig (-4.5%)?"""
    actual_bets = []
    for _, r in df.iterrows():
        eh = r[prob_col] - r["p_home_fair"]
        ea = (1 - r[prob_col]) - r["p_away_fair"]
        if eh > thr:
            actual_bets.append((r["ml_home_close"], r["y"] == 1))
        elif ea > thr:
            actual_bets.append((r["ml_away_close"], r["y"] == 0))
    if not actual_bets:
        return None
    pls = np.array([ml_payout(ml, w) for ml, w in actual_bets])
    actual_roi = pls.mean() / STAKE
    n = len(pls)
    # bootstrap distribution of mean P&L under sampling with replacement
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1) / STAKE
    return actual_roi, n, boots.mean(), boots.std(), float((boots <= 0).mean())


def run_set(label, train, evalset, feature_cols, holdout=None):
    Xtr, ytr = train[feature_cols].values, train["y"].values
    Xev, yev = evalset[feature_cols].values, evalset["y"].values

    lr = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("lr", LogisticRegression(C=0.3, max_iter=3000)),
    ])
    lr.fit(Xtr, ytr)

    gb = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("gb", GradientBoostingClassifier(
            n_estimators=300, max_depth=3, learning_rate=0.04,
            subsample=0.8, random_state=42)),
    ])
    gb.fit(Xtr, ytr)

    evalset = evalset.copy()
    evalset["lr_p"] = lr.predict_proba(Xev)[:, 1]
    evalset["gb_p"] = gb.predict_proba(Xev)[:, 1]

    print(f"\n=== {label} ===")
    evaluate("Market (closing, vig-free)", yev, evalset["p_home_fair"].values)
    evaluate("Logistic regression",        yev, evalset["lr_p"].values)
    evaluate("Gradient boosting",          yev, evalset["gb_p"].values)

    print("\n  Backtest LR vs market:")
    backtest(evalset, "lr_p")
    print("\n  Backtest GB vs market:")
    backtest(evalset, "gb_p")

    # baselines
    yev_int = evalset["y"].astype(int).values
    home_pl = [ml_payout(r["ml_home_close"], r["y"] == 1) for _, r in evalset.iterrows()]
    fav_pl = []
    for _, r in evalset.iterrows():
        if r["ml_home_close"] < r["ml_away_close"]:
            fav_pl.append(ml_payout(r["ml_home_close"], r["y"] == 1))
        else:
            fav_pl.append(ml_payout(r["ml_away_close"], r["y"] == 0))
    n = len(home_pl)
    print(f"\n  'Always home'      n={n} profit={sum(home_pl):+,.0f}   ROI {100*sum(home_pl)/(n*STAKE):+.2f}%")
    print(f"  'Always favorite'  n={n} profit={sum(fav_pl):+,.0f}   ROI {100*sum(fav_pl)/(n*STAKE):+.2f}%")

    # significance on GB at thr=0
    res = bootstrap_roi_pvalue(evalset, "gb_p", 0.0)
    if res:
        roi, nb, mu, sd, p_le_0 = res
        print(f"\n  Bootstrap (GB @ thr=0): bets={nb}  ROI={100*roi:+.2f}%  "
              f"sample-mean={100*mu:+.2f}%  sample-std={100*sd:.2f}%  P(true ROI <= 0)={p_le_0:.3f}")
    return evalset


def main():
    df = load_and_engineer()
    df = df.dropna(subset=FEATURES_BASE + ["y", "p_home_fair", "ml_home_close", "ml_away_close"])
    print(f"Total games with full features + odds: {len(df)}")

    train = df[df["year"] <= 2023].copy()
    test  = df[df["year"] == 2024].copy()
    hold  = df[df["year"] == 2025].copy()
    print(f"Train (2021-2023): {len(train)}")
    print(f"Test  (2024):       {len(test)}")
    print(f"Hold  (2025):       {len(hold)}")

    # ----- Model variant A: stats only
    test_a = run_set("VARIANT A: stats only -> TEST 2024", train, test, FEATURES_BASE)
    run_set("VARIANT A: stats only -> HOLDOUT 2025", train, hold, FEATURES_BASE)

    # ----- Model variant B: stats + market (residual learner)
    test_b = run_set("VARIANT B: stats + market -> TEST 2024", train, test, FEATURES_PLUS_MKT)
    run_set("VARIANT B: stats + market -> HOLDOUT 2025", train, hold, FEATURES_PLUS_MKT)


if __name__ == "__main__":
    main()
