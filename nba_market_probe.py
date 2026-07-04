"""Quick, honest NBA game-market efficiency probe.

Question: is the NBA closing line as sharp as MLB's (i.e. no edge), or is there
a no-model inefficiency worth a real model build?

Steps:
 1. Build game-level results from league_games_all (home = 'vs.', away = '@').
 2. Join to historical_nba_odds (ml_home/ml_away/spread/ou) by date + teams.
 3. Closing-line ML log loss (devigged) vs coinflip vs home-always baseline.
 4. No-model angle checks (blind home dogs, blind unders/overs, big favorites)
    with bootstrap P(ROI<=0). These need NO model — pure market-bias tests.
 5. A leak-free rolling-net-rating logistic model, OOF by season, vs closing
    line on log loss + ROI. One honest model pass.
"""
import os
import numpy as np, pandas as pd, psycopg2, warnings
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import log_loss
warnings.filterwarnings('ignore')

PG = dict(host='localhost', user='postgres', dbname='hoop_scoop', password=os.environ.get("PGPASSWORD", ""))
STAKE = 100.0
RNG = np.random.default_rng(42)

def a2p(ml):
    ml = float(ml)
    return 100.0/(ml+100.0) if ml > 0 else abs(ml)/(abs(ml)+100.0)

def payout(ml, won):
    if not won: return -STAKE
    return STAKE*(ml/100.0) if ml > 0 else STAKE*(100.0/abs(ml))

def boot(bets, n_iter=10000):
    if not bets: return None
    pls = np.array([payout(ml, w) for ml, w in bets])
    n = len(pls); roi = 100*pls.mean()/STAKE
    boots = RNG.choice(pls, size=(n_iter, n), replace=True).mean(axis=1)/STAKE
    wins = sum(1 for _, w in bets if w)
    return n, 100*wins/n, roi, float((boots <= 0).mean())

pg = psycopg2.connect(**PG)
g = pd.read_sql("""
    SELECT game_id, game_date, team_name, matchup, pts,
           fga, fg3a, fta, oreb, tov, reb, ast
    FROM league_games_all
""", pg)
odds = pd.read_sql("SELECT * FROM historical_nba_odds", pg)
pg.close()

g['game_date'] = pd.to_datetime(g['game_date'])
g['is_home'] = g['matchup'].str.contains('vs.', regex=False)

home = g[g['is_home']].rename(columns={'team_name':'home_team','pts':'home_pts'})
away = g[~g['is_home']].rename(columns={'team_name':'away_team','pts':'away_pts'})
games = home.merge(away[['game_id','away_team','away_pts']], on='game_id')
games = games[['game_id','game_date','home_team','away_team','home_pts','away_pts',
               'fga','fg3a','fta','oreb','tov']]
games['home_won'] = (games['home_pts'] > games['away_pts']).astype(int)
games['total'] = games['home_pts'] + games['away_pts']
games['margin'] = games['home_pts'] - games['away_pts']

odds['game_date'] = pd.to_datetime(odds['game_date'])
m = games.merge(odds, on=['game_date','home_team','away_team'], how='inner')
print(f"Games with results: {len(games)}  |  joined to odds: {len(m)}")
m = m.dropna(subset=['ml_home','ml_away']).copy()
m['p_home_raw'] = m['ml_home'].apply(a2p)
m['p_away_raw'] = m['ml_away'].apply(a2p)
m['ov'] = m['p_home_raw'] + m['p_away_raw']
m['p_home_fair'] = m['p_home_raw'] / m['ov']
m = m.sort_values('game_date').reset_index(drop=True)
print(f"Usable rows with ML odds: {len(m)}   date range {m.game_date.min().date()}..{m.game_date.max().date()}")
print()

# ---- 1. Closing line efficiency ----
print("="*64)
print("1) NBA CLOSING-LINE MONEYLINE EFFICIENCY")
print("="*64)
y = m['home_won'].values
print(f"  coinflip log loss          : 0.6931")
print(f"  home-always (base rate {y.mean():.3f}): {log_loss(y, np.clip(np.full(len(y), y.mean()),1e-4,1-1e-4)):.4f}")
print(f"  closing line (devig) log loss: {log_loss(y, np.clip(m['p_home_fair'].values,1e-4,1-1e-4)):.4f}")
print(f"  vig (overround) median       : {m['ov'].median():.4f}")
print("  Reference: sharp NBA closing line ~0.64-0.66. Lower => sharper/efficient.")
print()

# ---- 2. No-model market-bias angles ----
print("="*64)
print("2) NO-MODEL ANGLES (pure market bias, bootstrap P(ROI<=0))")
print("="*64)
def report(name, bets):
    r = boot(bets)
    if r is None: print(f"  {name:<34} (no bets)"); return
    n, wp, roi, p = r
    print(f"  {name:<34} n={n:>5} win%={wp:>5.1f} ROI={roi:>+6.2f}% P(<=0)={p:.3f}")

report("blind home (ML)",      [(m.ml_home[i], y[i]==1) for i in range(len(m))])
report("blind away (ML)",      [(m.ml_away[i], y[i]==0) for i in range(len(m))])
report("blind home underdog",  [(m.ml_home[i], y[i]==1) for i in range(len(m)) if m.ml_home[i]>0])
report("blind away underdog",  [(m.ml_away[i], y[i]==0) for i in range(len(m)) if m.ml_away[i]>0])
report("heavy fav <= -300",    [(m.ml_home[i], y[i]==1) for i in range(len(m)) if m.ml_home[i]<=-300]
                              + [(m.ml_away[i], y[i]==0) for i in range(len(m)) if m.ml_away[i]<=-300])

# Totals (-110 both sides assumed)
hasou = m.dropna(subset=['ou']).copy()
ov_bets = [(-110, hasou.total.values[i] > hasou.ou.values[i]) for i in range(len(hasou)) if hasou.total.values[i]!=hasou.ou.values[i]]
un_bets = [(-110, hasou.total.values[i] < hasou.ou.values[i]) for i in range(len(hasou)) if hasou.total.values[i]!=hasou.ou.values[i]]
report("blind OVER (-110)", ov_bets)
report("blind UNDER (-110)", un_bets)

# Spread ATS (-110). spread is home spread (positive = home getting points? check sign)
hasp = m.dropna(subset=['spread']).copy()
# Determine sign convention: if home favored, ml_home<0. Test both interpretations via cover rate.
# Convention guess: 'spread' is the home line magnitude with sign s.t. home_margin + spread*?
# We'll compute home cover under: home covers if (margin + spread) > 0  (spread = points home gives/gets)
fav_home = hasp['ml_home'] < hasp['ml_away']
home_cover_A = (hasp['margin'] + hasp['spread']) > 0   # spread positive = home getting points
report("home ATS (spread as +pts to home)", [(-110, home_cover_A.values[i]) for i in range(len(hasp)) if (hasp['margin'].values[i]+hasp['spread'].values[i])!=0])
print()

# ---- 3. One honest rolling-net-rating model, OOF by season ----
print("="*64)
print("3) ROLLING NET-RATING LOGISTIC MODEL (leak-free, OOF by season)")
print("="*64)
# Build per-team rolling pre-game point differential & pace from league_games (game level both teams)
tg = g[['game_id','game_date','team_name','pts','matchup']].copy()
tg['is_home'] = tg['matchup'].str.contains('vs.', regex=False)
# opp pts via self-join on game_id
opp = tg[['game_id','team_name','pts']].rename(columns={'team_name':'opp','pts':'opp_pts'})
tg = tg.merge(opp, on='game_id'); tg = tg[tg['team_name']!=tg['opp']]
tg['margin'] = tg['pts'] - tg['opp_pts']
tg = tg.sort_values(['team_name','game_date'])
# rolling mean of last 15 games margin & pts, SHIFTED to exclude current game (no leak)
tg['roll_margin'] = tg.groupby('team_name')['margin'].transform(lambda s: s.shift(1).rolling(15, min_periods=5).mean())
tg['roll_pts']    = tg.groupby('team_name')['pts'].transform(lambda s: s.shift(1).rolling(15, min_periods=5).mean())
feat = tg[['game_id','team_name','roll_margin','roll_pts']]

mm = m.merge(feat.rename(columns={'team_name':'home_team','roll_margin':'h_rm','roll_pts':'h_rp'}), on=['game_id','home_team'], how='left')
mm = mm.merge(feat.rename(columns={'team_name':'away_team','roll_margin':'a_rm','roll_pts':'a_rp'}), on=['game_id','away_team'], how='left')
mm['d_rm'] = mm['h_rm'] - mm['a_rm']
mm['d_rp'] = mm['h_rp'] - mm['a_rp']
mm = mm.dropna(subset=['d_rm','d_rp']).copy()
mm['season_yr'] = mm['game_date'].dt.year + (mm['game_date'].dt.month>=9).astype(int)
FEATS = ['d_rm','d_rp']
preds = []
for yr in sorted(mm['season_yr'].unique()):
    tr = mm[mm['season_yr'] < yr]; te = mm[mm['season_yr']==yr]
    if len(tr) < 500 or len(te) < 100: continue
    sc = StandardScaler().fit(tr[FEATS])
    lr = LogisticRegression().fit(sc.transform(tr[FEATS]), tr['home_won'])
    te = te.copy(); te['p_model'] = lr.predict_proba(sc.transform(te[FEATS]))[:,1]
    preds.append(te)
P = pd.concat(preds, ignore_index=True)
yP = P['home_won'].values
print(f"  OOF games: {len(P)}")
print(f"  model log loss : {log_loss(yP, np.clip(P['p_model'],1e-4,1-1e-4)):.4f}")
print(f"  market log loss: {log_loss(yP, np.clip(P['p_home_fair'],1e-4,1-1e-4)):.4f}")
print("  (model should be WORSE; if it BEATS market that's the signal)")
print()
print("  Model-vs-market ROI (bet when model edge > thr):")
print(f"  {'thr':>5} {'bets':>6} {'win%':>6} {'ROI%':>7} {'P(<=0)':>7}")
for thr in (0.02,0.04,0.06,0.08):
    bets=[]
    for _,r in P.iterrows():
        eh = r['p_model']-r['p_home_fair']; ea=(1-r['p_model'])-(1-r['p_home_fair'])
        if eh>thr: bets.append((r['ml_home'], r['home_won']==1))
        elif ea>thr: bets.append((r['ml_away'], r['home_won']==0))
    rb = boot(bets)
    if rb: n,wp,roi,p = rb; print(f"  {thr:>5.2f} {n:>6} {wp:>6.1f} {roi:>+7.2f} {p:>7.3f}")
