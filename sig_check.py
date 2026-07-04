"""Targeted significance check on the slice positive in BOTH holdouts."""
import os
import numpy as np
import pandas as pd
import psycopg2
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
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


# Mirror v3
exec(open(r"c:\Users\jackp\Desktop\new_game\train_mlb_v3.py").read().split("def main():")[0])

# Run abbreviated pipeline
df = load_data(); df = engineer(df)
train = df[df["year"] <= 2023].copy()
test  = df[df["year"] == 2024].copy()
hold  = df[df["year"] == 2025].copy()

Xtr = train[FEATS_PLUS_MKT].values; ytr = train["y"].astype(int).values
Xte = test[FEATS_PLUS_MKT].values;  yte = test["y"].astype(int).values
Xho = hold[FEATS_PLUS_MKT].values;  yho = hold["y"].astype(int).values

probs_te, _ = fit_models(Xtr, ytr, Xte, yte)
probs_ho, _ = fit_models(Xtr, ytr, Xho, yho)
test["p_ens"] = probs_te["ENS"]
hold["p_ens"] = probs_ho["ENS"]

# Combine
combined = pd.concat([test, hold], ignore_index=True)

def collect_bets(df, prob_col, thr):
    bets = []
    for _, r in df.iterrows():
        eh = r[prob_col] - r["p_home_fair"]
        ea = (1 - r[prob_col]) - r["p_away_fair"]
        if eh > thr:
            bets.append((r["ml_home_close"], r["y"] == 1, "home"))
        elif ea > thr:
            bets.append((r["ml_away_close"], r["y"] == 0, "away"))
    return bets

def boot(bets, n_iter=10000):
    if not bets: return None
    pls = np.array([ml_payout(ml, w) for ml, w, _ in bets])
    n = len(pls); roi = pls.mean()/STAKE
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1)/STAKE
    # one-tailed p-value for ROI > 0
    return n, 100*roi, 100*boots.mean(), 100*boots.std(), float((boots <= 0).mean())

print(f"{'Slice':<35} {'N':>5} {'ROI':>8} {'Std':>7} {'P(ROI<=0)':>11}")
print("-"*75)

# Per-holdout
for thr in [0.06, 0.08, 0.10, 0.12]:
    for name, dset in [("2024", test), ("2025", hold), ("COMBINED", combined)]:
        b = collect_bets(dset, "p_ens", thr)
        r = boot(b)
        if r:
            n, roi, mu, sd, p = r
            print(f"  thr={thr:.2f}  {name:<22} {n:>5} {roi:>+7.2f}% {sd:>6.2f}% {p:>10.3f}")

# Side-specific analysis on combined at thr=0.04 (more bets)
print("\n--- Side-specific (combined, thr=0.04) ---")
b04 = collect_bets(combined, "p_ens", 0.04)
home = [x for x in b04 if x[2] == "home"]
away = [x for x in b04 if x[2] == "away"]
print(f"Home bets: {boot(home)}")
print(f"Away bets: {boot(away)}")

# By moneyline bucket on combined at thr=0.04
print("\n--- ML buckets (combined, thr=0.04) ---")
buckets = [("Heavy fav <=-200", lambda m: m <= -200),
           ("Fav -199..-110",   lambda m: -199 <= m <= -110),
           ("Pickem -109..+110",lambda m: -109 <= m <= 110),
           ("Dog +111..+200",   lambda m: 111 <= m <= 200),
           ("Heavy dog +201+",  lambda m: m >= 201)]
for label, pred in buckets:
    sub = [x for x in b04 if pred(x[0])]
    r = boot(sub)
    if r:
        n, roi, mu, sd, p = r
        print(f"  {label:<22} n={n:>4} roi={roi:>+6.2f}% p={p:.3f}")
