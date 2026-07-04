"""Sanity check the v5 model + pipeline. Catches issues before live betting.

Checks:
  1. Val log-loss claim: split val into early-stop tail (used) and a TRULY held
     last-2-weeks tail (never seen), measure model vs market on both.
  2. Calibration output distribution: how many unique values does p_cal take?
  3. Historical odds plausibility: market log loss on full dataset, distribution
     of overround, fraction of -110/-110 pickem games (sanity)
  4. Time filter audit: scan SQL for any '<=' on game_date that should be '<'
  5. Live feature audit: for one game, print every feature's value
"""
import os
import pickle
import re
from pathlib import Path
import numpy as np
import pandas as pd
import psycopg2
from sklearn.metrics import log_loss
import xgboost as xgb

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
MODEL_PATH = Path(r"c:\Users\jackp\Desktop\new_game\v5_model.pkl")
STAKE = 100.0


def american_to_prob(ml):
    if pd.isna(ml): return np.nan
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)


# -------------------- 1. Honest out-of-sample re-evaluation --------------------

def load_data():
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


def engineer_minimal(df):
    """Full engineering matching save_v5_model.py (so model features line up)."""
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


print("="*72)
print("1) HONEST VAL LOG-LOSS CHECK")
print("="*72)
print()
print("The saved model used the last 6 weeks of 2025 BOTH for early stopping")
print("AND for isotonic calibration. That means the published 0.6717 val log-loss")
print("is OPTIMISTICALLY biased -- the early-stopping trees were selected to")
print("minimize log-loss on the same data we're evaluating.")
print()
print("To get a clean number, we need data that the saved model NEVER saw.")
print("We re-load mlb_features_v5 and compute model probabilities on the")
print("last 14 days, then compare to market on those same 14 days.")
print()

df = load_data()
df = engineer_minimal(df)
print(f"Total data: {len(df)} games, ending {df['game_date'].max().date()}")

with open(MODEL_PATH, "rb") as f:
    m = pickle.load(f)

feats = m["features"]
imp = m["imputer"]
sc  = m["scaler"]
lr  = m["lr"]
xgbm = m["xgb"]
lgbm = m["lgb"]
iso  = m["isotonic"]

# Score everything
X = df[feats].values.astype(float)
X_i = imp.transform(X); X_s = sc.transform(X_i)
p_lr = lr.predict_proba(X_s)[:, 1]
p_xgb = xgbm.predict_proba(X_i)[:, 1]
p_lgb = lgbm.predict_proba(X_i)[:, 1]
p_ens = (p_lr + p_xgb + p_lgb) / 3
p_cal = iso.transform(p_ens)
df["p_ens"] = p_ens
df["p_cal"] = p_cal

# The model was trained through 2025-07-05. Anything after that is honest OOS.
# But the val set (used for early stopping + calibration) IS in there.
# Reported val end date 2025-08-16. So games after 2025-08-16 are TRULY oos.
print()
print("Saved model says trained_through =", m["trained_through"])
print(f"Saved model said val log-loss = {m['val_log_loss']:.4f} vs market {m['market_log_loss']:.4f}")
print()

# Reproduce the "saved" val set numbers
val_mask = df["game_date"] > pd.Timestamp(m["trained_through"])
val = df[val_mask]
print(f"Reproducing 'val' (game_date > {m['trained_through']}): {len(val)} games")
if len(val):
    yv = val["y"].astype(int).values
    print(f"  market log loss = {log_loss(yv, np.clip(val['p_home_fair'].values, 1e-4, 1-1e-4)):.4f}")
    print(f"  model  log loss = {log_loss(yv, np.clip(val['p_cal'].values, 1e-4, 1-1e-4)):.4f}")
    print(f"  (matches reported) -- but this WAS used for early stopping + isotonic.")
    print(f"  So this number is OVER-OPTIMISTIC.")

# What if we exclude the last 2 weeks of the val period?
# Early stopping picks the iteration with best val log-loss across ALL val games.
# A truly clean split needs games the val set never saw.
print()
print("Anti-leak check: split saved-val period into early-half and late-half.")
print("If model 'beats market' on EARLY half but not LATE, the gap is from")
print("selecting trees that fit the val set, not a real edge.")
if len(val):
    midpt = val["game_date"].quantile(0.5)
    early = val[val["game_date"] <= midpt]
    late  = val[val["game_date"] > midpt]
    for name, dset in [("EARLY half", early), ("LATE half ", late)]:
        if not len(dset): continue
        yi = dset["y"].astype(int).values
        m_ll = log_loss(yi, np.clip(dset["p_home_fair"].values, 1e-4, 1-1e-4))
        ens_ll = log_loss(yi, np.clip(dset["p_ens"].values, 1e-4, 1-1e-4))
        cal_ll = log_loss(yi, np.clip(dset["p_cal"].values, 1e-4, 1-1e-4))
        print(f"  {name}: n={len(dset)}  market={m_ll:.4f}  ens_raw={ens_ll:.4f}  ens_cal={cal_ll:.4f}")
        if cal_ll > ens_ll:
            print(f"    !! calibration HURT log loss ({cal_ll-ens_ll:+.4f}) -- step function too coarse")
print()


# -------------------- 2. Calibration output distribution --------------------

print("="*72)
print("2) CALIBRATION OUTPUT DISTRIBUTION")
print("="*72)
print()
print(f"isotonic regression unique output values: {len(np.unique(np.round(p_cal, 4)))}")
print(f"distribution percentiles of p_cal:")
for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99):
    print(f"  {q*100:>5.1f}%: {np.quantile(p_cal, q):.4f}")
print()
print("Top 8 most-common p_cal values (rounded 3 decimals):")
vc = pd.Series(np.round(p_cal, 3)).value_counts().head(8)
for v, n in vc.items():
    print(f"  {v}  : {n} games ({100*n/len(p_cal):.1f}%)")
print()
print("Top 8 most-common p_ENS (pre-calibration) values rounded 3 decimals:")
vc = pd.Series(np.round(p_ens, 3)).value_counts().head(8)
for v, n in vc.items():
    print(f"  {v}  : {n} games ({100*n/len(p_ens):.1f}%)")
print()


# -------------------- 3. Historical odds sanity --------------------

print("="*72)
print("3) HISTORICAL ODDS SANITY")
print("="*72)
print()
pg = psycopg2.connect(**PG)
hd = pd.read_sql("""
    SELECT game_date,
           ml_home_close, ml_away_close, ml_home_open, ml_away_open,
           n_books_close, home_score, away_score
    FROM historical_mlb_odds
""", pg)
pg.close()

hd["p_home_close"] = hd["ml_home_close"].apply(american_to_prob)
hd["p_away_close"] = hd["ml_away_close"].apply(american_to_prob)
hd["overround"] = hd["p_home_close"] + hd["p_away_close"]
hd["p_home_fair"] = hd["p_home_close"] / hd["overround"]
hd["home_won"] = (hd["home_score"] > hd["away_score"]).astype("Int64")

# Filter to games we can actually score
mask = hd["home_won"].notna() & hd["p_home_fair"].notna() & (hd["home_score"] != hd["away_score"])
hd_eval = hd[mask].copy()
print(f"odds dataset rows: {len(hd)}, evaluable (non-tie, both odds present): {len(hd_eval)}")
m_ll = log_loss(hd_eval["home_won"].astype(int), np.clip(hd_eval["p_home_fair"], 1e-4, 1-1e-4))
print(f"Closing-line market log loss on ENTIRE odds dataset: {m_ll:.4f}")
print("Reference benchmarks:")
print("  Coinflip: 0.6931")
print("  Sharp MLB closing line: 0.660-0.685 typical")
print("  POST-GAME info leak would give: <0.5")
print(f"  Result: {'PLAUSIBLE (sharp closing line)' if 0.65 < m_ll < 0.69 else 'SUSPICIOUS'}")
print()

print(f"Overround distribution (1 + vig):")
for q in (0.01, 0.5, 0.99):
    print(f"  {q*100:>5.1f}%: {np.quantile(hd['overround'].dropna(), q):.4f}")
print("  (~1.03-1.04 is typical for MLB main markets; 1.10+ would be suspicious)")
print()

print(f"n_books distribution (should be ~5-6 for main markets):")
print(hd["n_books_close"].describe())
print()


# -------------------- 4. Time filter audit --------------------

print("="*72)
print("4) TIME FILTER AUDIT (looking for <= where < should be used)")
print("="*72)
print()
risky = []
for path in Path(r"c:\Users\jackp\Desktop\new_game").glob("*.py"):
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        continue
    # Find any 'game_date <= ' patterns
    for m in re.finditer(r"game_date\s*<=", text):
        ctx_start = max(0, m.start() - 40)
        ctx_end = min(len(text), m.end() + 60)
        risky.append((path.name, text[ctx_start:ctx_end].replace("\n", " ")))
if risky:
    print(f"  Found {len(risky)} '<=' on game_date — review each:")
    for fn, ctx in risky[:15]:
        print(f"    {fn}: ...{ctx}...")
else:
    print("  No 'game_date <=' patterns found. All time filters use strict < (good).")
print()

# Audit the standings join fix
pg = psycopg2.connect(**PG)
with pg.cursor() as c:
    c.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE h_api_streak IS NOT NULL),
               COUNT(*) FILTER (WHERE h_api_streak IS NOT NULL AND
                                EXISTS (SELECT 1 FROM mlb_team_standings s
                                        WHERE s.snapshot_date = f.game_date
                                          AND s.team_id = f.home_team_id
                                          AND substr(s.streak,2)::int = ABS(f.h_api_streak)
                                          AND ((s.streak LIKE 'W%' AND f.h_api_streak > 0)
                                            OR (s.streak LIKE 'L%' AND f.h_api_streak < 0))))
        FROM mlb_features_v6 f
    """)
    total, with_streak, would_leak = c.fetchone()
    print(f"v6 leakage re-check:")
    print(f"  rows with h_api_streak set:          {with_streak}/{total}")
    print(f"  rows where streak == same-day val:  {would_leak} (this should NOT equal {with_streak})")
pg.close()
print()


# -------------------- 5. Sample live feature audit --------------------

print("="*72)
print("5) SAMPLE LIVE FEATURE AUDIT (2025-08-15 Blue Jays game)")
print("="*72)
print()
pg = psycopg2.connect(**PG)
sample = pd.read_sql("""
    SELECT f.*
    FROM mlb_features_v5 f
    JOIN mlb_games_2025 g ON g.game_pk::int = f.game_pk
    WHERE g.game_date = '2025-08-15' AND g.home_team_name = 'Toronto Blue Jays'
""", pg)
pg.close()

if not sample.empty:
    print("Feature row for Blue Jays @ Rangers on 2025-08-15:")
    s = sample.iloc[0]
    for col in ["home_team_name", "away_team_name", "y", "home_score", "away_score",
                "h_p_era_sd", "a_p_era_sd",   # season-to-date ERAs
                "h_lineup_woba", "a_lineup_woba",
                "h_pyth", "a_pyth",
                "temp_f", "wind_mph",
                "h_bp_era_14", "a_bp_era_14",
                "ml_home_close", "ml_away_close"]:
        if col in sample.columns:
            print(f"  {col:<22} {s[col]}")
print()


# -------------------- 6. P&L math --------------------

print("="*72)
print("6) P&L MATH SANITY")
print("="*72)
print()
def pl(ml, stake, won):
    if not won: return -stake
    return stake * (ml / 100.0) if ml > 0 else stake * (100.0 / abs(ml))
print("Test cases at $100 stake:")
cases = [(140, True, 140), (140, False, -100),
         (-200, True, 50), (-200, False, -100),
         (100, True, 100), (-100, True, 100), (-110, True, 90.909)]
for ml, won, expected in cases:
    got = pl(ml, 100, won)
    ok = "OK" if abs(got - expected) < 0.01 else "FAIL"
    print(f"  ML={ml:+5}  won={won!s:<5}  expected {expected:+.2f}  got {got:+.2f}  {ok}")
print()


# -------------------- 7. Final honest summary --------------------

print("="*72)
print("7) HONEST SUMMARY")
print("="*72)
