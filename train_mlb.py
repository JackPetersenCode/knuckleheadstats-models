"""Train MLB game-winner models and honestly evaluate against closing lines.

Pipeline:
  1. Pull mlb_features from Postgres.
  2. Engineer pitcher rate features (ERA, K/9, BB/9, HR/9 from rolling sums).
  3. Time-series split: train 2021-2023, test 2024, holdout 2025.
  4. Fit logistic regression and gradient boosting.
  5. Compare model probabilities to vig-free closing-line implied probabilities.
  6. Backtest betting strategies: only bet when model_prob - market_prob > edge_threshold.
  7. Report ROI, win rate, CLV, calibration.
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


def american_to_prob(ml):
    if pd.isna(ml):
        return np.nan
    ml = float(ml)
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


def ml_payout(ml, won):
    if not won:
        return -STAKE
    if ml > 0:
        return STAKE * (ml / 100.0)
    return STAKE * (100.0 / abs(ml))


def load_data():
    pg = psycopg2.connect(**PG)
    df = pd.read_sql("""
        SELECT game_pk, game_date,
               home_team_name, away_team_name, y,
               is_night, park_rpg,
               h_wpct, h_rs, h_ra,
               a_wpct, a_rs, a_ra,
               h_p_ip, h_p_er, h_p_k, h_p_bb, h_p_hr, h_p_starts, h_p_rest,
               a_p_ip, a_p_er, a_p_k, a_p_bb, a_p_hr, a_p_starts, a_p_rest,
               ml_home_close, ml_away_close
        FROM mlb_features
        WHERE ml_home_close IS NOT NULL
          AND ml_away_close IS NOT NULL
        ORDER BY game_date
    """, pg)
    pg.close()
    return df


def engineer(df):
    # Pitcher rate features (handle zero IP)
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

    # Market-implied probabilities (with vig)
    df["p_home_raw"] = df["ml_home_close"].apply(american_to_prob)
    df["p_away_raw"] = df["ml_away_close"].apply(american_to_prob)
    df["overround"]  = df["p_home_raw"] + df["p_away_raw"]
    # Vig-free fair probability (proportional method)
    df["p_home_fair"] = df["p_home_raw"] / df["overround"]
    df["p_away_fair"] = df["p_away_raw"] / df["overround"]

    # Year
    df["year"] = pd.to_datetime(df["game_date"]).dt.year
    return df


FEATURES = [
    "is_night", "park_rpg",
    "h_wpct", "h_rs", "h_ra",
    "a_wpct", "a_rs", "a_ra",
    "h_p_era", "h_p_k9", "h_p_bb9", "h_p_hr9", "h_p_ipgs", "h_p_rest", "h_p_starts",
    "a_p_era", "a_p_k9", "a_p_bb9", "a_p_hr9", "a_p_ipgs", "a_p_rest", "a_p_starts",
]


def evaluate(df, prob_col, name):
    """Print log loss / Brier for the predicted probabilities vs y."""
    mask = df[prob_col].notna() & df["y"].notna()
    p = np.clip(df.loc[mask, prob_col].astype(float), 1e-4, 1 - 1e-4)
    y = df.loc[mask, "y"].astype(int)
    ll = log_loss(y, p)
    br = brier_score_loss(y, p)
    print(f"  {name:<20} log_loss={ll:.4f}  brier={br:.4f}  n={len(p)}")
    return ll, br


def backtest(df, prob_col, edge_thresholds=(0.0, 0.02, 0.04, 0.06)):
    """For each edge threshold, bet home/away when model edge > threshold.
    Use ACTUAL closing moneyline for payout. Report win%, ROI."""
    results = []
    for thr in edge_thresholds:
        home_bet = df[prob_col] - df["p_home_fair"] > thr
        away_bet = (1 - df[prob_col]) - df["p_away_fair"] > thr
        bets = []
        for _, r in df[home_bet].iterrows():
            won = r["y"] == 1
            bets.append((r["ml_home_close"], won, "home"))
        for _, r in df[away_bet].iterrows():
            won = r["y"] == 0
            bets.append((r["ml_away_close"], won, "away"))
        if not bets:
            results.append((thr, 0, 0, 0, 0.0, 0.0))
            continue
        n = len(bets)
        wins = sum(1 for _, w, _ in bets if w)
        profit = sum(ml_payout(ml, w) for ml, w, _ in bets)
        risked = n * STAKE
        roi = 100.0 * profit / risked
        wpct = 100.0 * wins / n
        results.append((thr, n, wins, n - wins, wpct, roi))
    return results


def clv_check(df, prob_col):
    """Closing line value: % of bets where model's edge direction agreed with the
    *closing* line (since opening to closing line movement is the market's update,
    not relevant here; instead we check if model 'beats' the market by predicting
    the side that the market is undervaluing, i.e., the closing implied prob is
    consistent with model prediction direction)."""
    # Simpler: distribution of model edges
    edges = df[prob_col] - df["p_home_fair"]
    print(f"  Edge distribution (model - market on HOME):")
    print(f"    mean={edges.mean():+.4f}  std={edges.std():.4f}")
    print(f"    pct positive: {100.0 * (edges > 0).mean():.1f}%")
    print(f"    edges > +5pp: {(edges > 0.05).sum()}  edges < -5pp: {(edges < -0.05).sum()}")


def main():
    print("Loading data...")
    df = load_data()
    df = engineer(df)
    print(f"  total rows with odds: {len(df)}")
    df = df.dropna(subset=FEATURES + ["y", "p_home_fair", "ml_home_close", "ml_away_close"])
    print(f"  rows with full features: {len(df)}")

    train = df[df["year"] <= 2023].copy()
    test  = df[df["year"] == 2024].copy()
    holdout = df[df["year"] == 2025].copy()
    print(f"\n  Train: {len(train)} games (2021-2023)")
    print(f"  Test : {len(test)} games (2024)")
    print(f"  Hold : {len(holdout)} games (2025 partial)")

    Xtr, ytr = train[FEATURES].values, train["y"].values
    Xte, yte = test[FEATURES].values, test["y"].values
    Xho, yho = holdout[FEATURES].values, holdout["y"].values

    # === Model 1: Logistic regression with scaling
    print("\n--- Logistic Regression (scaled, L2) ---")
    lr = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("lr", LogisticRegression(C=1.0, max_iter=2000)),
    ])
    lr.fit(Xtr, ytr)
    train["lr_p"] = lr.predict_proba(Xtr)[:, 1]
    test["lr_p"]  = lr.predict_proba(Xte)[:, 1]
    holdout["lr_p"] = lr.predict_proba(Xho)[:, 1]

    # === Model 2: Gradient boosting (early-stoppable on validation set)
    print("\n--- Gradient Boosting ---")
    gb = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("gb", GradientBoostingClassifier(
            n_estimators=400, max_depth=3, learning_rate=0.03,
            subsample=0.8, random_state=42,
        )),
    ])
    gb.fit(Xtr, ytr)
    train["gb_p"] = gb.predict_proba(Xtr)[:, 1]
    test["gb_p"]  = gb.predict_proba(Xte)[:, 1]
    holdout["gb_p"] = gb.predict_proba(Xho)[:, 1]

    # === Compare predictions on test set
    print("\n=== Performance on TEST (2024) ===")
    print("  Predictor probability quality:")
    evaluate(test, "p_home_fair", "Market (closing)")
    evaluate(test, "lr_p", "Logistic regression")
    evaluate(test, "gb_p", "Gradient boosting")

    print("\n  Backtest LR vs market:")
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'L':>5} {'Win%':>7} {'ROI%':>8}")
    for thr, n, w, l, wp, roi in backtest(test, "lr_p"):
        print(f"  {thr:>6.2f} {n:>6} {w:>5} {l:>5} {wp:>6.1f}% {roi:>+7.2f}%")
    print("\n  Backtest GB vs market:")
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'L':>5} {'Win%':>7} {'ROI%':>8}")
    for thr, n, w, l, wp, roi in backtest(test, "gb_p"):
        print(f"  {thr:>6.2f} {n:>6} {w:>5} {l:>5} {wp:>6.1f}% {roi:>+7.2f}%")

    print("\n  Edge analysis (LR):")
    clv_check(test, "lr_p")
    print("\n  Edge analysis (GB):")
    clv_check(test, "gb_p")

    # === Final holdout test
    print("\n=== Performance on HOLDOUT (2025 partial) ===")
    print("  Predictor probability quality:")
    evaluate(holdout, "p_home_fair", "Market (closing)")
    evaluate(holdout, "lr_p", "Logistic regression")
    evaluate(holdout, "gb_p", "Gradient boosting")
    print("\n  Backtest GB vs market on HOLDOUT:")
    print(f"  {'Thr':>6} {'Bets':>6} {'W':>5} {'L':>5} {'Win%':>7} {'ROI%':>8}")
    for thr, n, w, l, wp, roi in backtest(holdout, "gb_p"):
        print(f"  {thr:>6.2f} {n:>6} {w:>5} {l:>5} {wp:>6.1f}% {roi:>+7.2f}%")

    # === Trivial baselines
    print("\n=== Trivial baselines (TEST 2024) ===")
    test["always_home"] = 1.0
    test["market_only"] = test["p_home_fair"]
    print("  'Always bet home' ROI:")
    p_always = []
    for _, r in test.iterrows():
        p_always.append(ml_payout(r["ml_home_close"], r["y"] == 1))
    print(f"    n={len(p_always)} profit={sum(p_always):+.0f} ROI={100*sum(p_always)/(len(p_always)*STAKE):+.2f}%")

    # 'Always favorite' ROI:
    fav = []
    for _, r in test.iterrows():
        if r["ml_home_close"] < r["ml_away_close"]:
            fav.append(ml_payout(r["ml_home_close"], r["y"] == 1))
        else:
            fav.append(ml_payout(r["ml_away_close"], r["y"] == 0))
    print(f"  'Always favorite' ROI:")
    print(f"    n={len(fav)} profit={sum(fav):+.0f} ROI={100*sum(fav)/(len(fav)*STAKE):+.2f}%")

    # Feature importances
    print("\n=== Top GB feature importances ===")
    gbm = gb.named_steps["gb"]
    fi = sorted(zip(FEATURES, gbm.feature_importances_), key=lambda x: -x[1])
    for name, imp in fi[:12]:
        print(f"  {name:<14} {imp:.4f}")


if __name__ == "__main__":
    main()
