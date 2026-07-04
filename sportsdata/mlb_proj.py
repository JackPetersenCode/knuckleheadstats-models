"""Matchup-aware MLB player-prop projection -> model_fair P(over/under).

Independent fair-probability source (no SGO API cost; prices lines SGO doesn't cover).
Designed to FIX the old EWMA "compress-to-mean / can't rank" failure:

  - per-PA multinomial over {1B,2B,3B,HR,BB,K,OUT}
  - matchup combine by log5 / odds-ratio: rate = batter * pitcher / league  (restores
    spread — a star bat vs a weak pitcher multiplies instead of averaging to the mean)
  - Dirichlet-multinomial shrinkage in PA-space: (count + lg*TAU)/(PA + TAU)  (stars keep
    their rate; small samples shrink)
  - roll over a per-game PA distribution -> count / total-base distribution -> P(over line)

Leak-free: every rate uses only games strictly before the target date. Used by the
backtest (proj_mlb_backtest.py) and, once it beats naive on ranking, as an extra anchor
in ev_vs_sharp.
"""
import collections
import numpy as np

import db

EVENTS = ["1B", "2B", "3B", "HR", "BB", "K", "OUT"]
TB = {"1B": 1, "2B": 2, "3B": 3, "HR": 4, "BB": 0, "K": 0, "OUT": 0}
HIT = {"1B", "2B", "3B", "HR"}
TAU = 200.0           # shrinkage strength (PA-equivalent prior weight)
PA_BY_SLOT = {1: 4.55, 2: 4.45, 3: 4.35, 4: 4.25, 5: 4.12, 6: 4.0, 7: 3.88, 8: 3.76, 9: 3.62}


# ----------------------------- rate building -----------------------------
def _batter_events(row):
    """(events dict, PA) from a batting box row."""
    ab, h = row.get("ab") or 0, row.get("h") or 0
    bb, hbp, k = row.get("bb") or 0, row.get("hbp") or 0, row.get("k") or 0
    d2, d3, hr = row.get("doubles") or 0, row.get("triples") or 0, row.get("hr") or 0
    pa = ab + bb + hbp
    if pa <= 0:
        return None, 0
    s1 = max(h - d2 - d3 - hr, 0)
    walks = bb + hbp
    out = pa - s1 - d2 - d3 - hr - walks - k
    return {"1B": s1, "2B": d2, "3B": d3, "HR": hr, "BB": walks, "K": k, "OUT": max(out, 0)}, pa


def batter_rates(con, as_of, lookback_days=400):
    """player_id -> (rates dict, PA, pa_per_game) from regular-season games before as_of."""
    cur = con.cursor()
    cur.execute("""SELECT b.player_id, b.ab,b.h,b.doubles,b.triples,b.hr,b.bb,b.k,b.hbp
                   FROM mlb_batting_box b JOIN game g ON g.sport='mlb' AND g.game_id=b.game_id
                   WHERE g.season_type='regular' AND g.game_date < %s
                     AND g.game_date >= %s::date - %s""", (as_of, as_of, lookback_days))
    cols = ["player_id", "ab", "h", "doubles", "triples", "hr", "bb", "k", "hbp"]
    agg = collections.defaultdict(lambda: {e: 0 for e in EVENTS})
    pa_tot, games = collections.Counter(), collections.Counter()
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        ev, pa = _batter_events(d)
        if not ev:
            continue
        for e in EVENTS:
            agg[d["player_id"]][e] += ev[e]
        pa_tot[d["player_id"]] += pa
        games[d["player_id"]] += 1
    out = {}
    for pid, counts in agg.items():
        pa = pa_tot[pid]
        rates = {e: counts[e] / pa for e in EVENTS} if pa else None
        out[pid] = (rates, pa, pa_tot[pid] / max(games[pid], 1))
    return out


def pitcher_rates(con, as_of, lookback_days=500):
    """player_id -> (per-batter rates dict, BF, bf_per_start) allowed, before as_of."""
    cur = con.cursor()
    cur.execute("""SELECT b.player_id, SUM(b.bf) bf, SUM(b.h) h, SUM(b.bb) bb, SUM(b.k) k,
                          SUM(b.hr) hr, COUNT(*) g, SUM(CASE WHEN b.started THEN 1 ELSE 0 END) gs
                   FROM mlb_pitching_box b JOIN game g ON g.sport='mlb' AND g.game_id=b.game_id
                   WHERE g.season_type='regular' AND g.game_date < %s
                     AND g.game_date >= %s::date - %s
                   GROUP BY b.player_id""", (as_of, as_of, lookback_days))
    out = {}
    for pid, bf, h, bb, k, hr, g, gs in cur.fetchall():
        bf = bf or 0
        if bf < 1:
            continue
        # split allowed hits into 1B/2B/3B by league proportions later; store raw allowed rates
        out[pid] = (dict(H=(h or 0) / bf, HR=(hr or 0) / bf, BB=(bb or 0) / bf, K=(k or 0) / bf),
                    bf, (bf / gs) if gs else (bf / max(g, 1)))
    return out


def league_rates(con, as_of, lookback_days=400):
    cur = con.cursor()
    cur.execute("""SELECT SUM(ab) ab,SUM(h) h,SUM(doubles) d2,SUM(triples) d3,SUM(hr) hr,
                          SUM(bb) bb,SUM(k) k,SUM(hbp) hbp
                   FROM mlb_batting_box b JOIN game g ON g.sport='mlb' AND g.game_id=b.game_id
                   WHERE g.season_type='regular' AND g.game_date < %s
                     AND g.game_date >= %s::date - %s""", (as_of, as_of, lookback_days))
    ab, h, d2, d3, hr, bb, k, hbp = [x or 0 for x in cur.fetchone()]
    pa = ab + bb + hbp
    s1 = max(h - d2 - d3 - hr, 0); walks = bb + hbp
    out = pa - s1 - d2 - d3 - hr - walks - k
    lg = {"1B": s1, "2B": d2, "3B": d3, "HR": hr, "BB": walks, "K": k, "OUT": max(out, 0)}
    return {e: lg[e] / pa for e in EVENTS}, pa


# ----------------------------- shrink + combine -----------------------------
def _shrink(counts_rates, pa, lg):
    """Dirichlet-multinomial shrink toward league in PA-space."""
    return {e: (counts_rates[e] * pa + lg[e] * TAU) / (pa + TAU) for e in EVENTS}


def _pitcher_event_rates(prate_raw, lg):
    """Expand pitcher H-allowed into per-event rates using league hit-type shares; shrink in BF."""
    nonhr_lg = lg["1B"] + lg["2B"] + lg["3B"]
    h_nonhr = max(prate_raw["H"] - prate_raw["HR"], 0)
    share = {e: (lg[e] / nonhr_lg) for e in ("1B", "2B", "3B")}
    pr = {"1B": h_nonhr * share["1B"], "2B": h_nonhr * share["2B"], "3B": h_nonhr * share["3B"],
          "HR": prate_raw["HR"], "BB": prate_raw["BB"], "K": prate_raw["K"]}
    pr["OUT"] = max(1 - sum(pr.values()), 1e-6)
    return pr


def matchup_pa_probs(brate, prate, lg):
    """log5 odds-ratio combine of batter & pitcher per-PA rates -> normalized pmf."""
    raw = {e: brate[e] * prate[e] / max(lg[e], 1e-9) for e in EVENTS}
    s = sum(raw.values()) or 1.0
    return {e: raw[e] / s for e in EVENTS}


# ----------------------------- distributions -----------------------------
def _pa_dist(mu):
    lo = int(np.floor(mu)); frac = mu - lo
    return {lo: 1 - frac, lo + 1: frac} if frac > 1e-6 else {lo: 1.0}


def _count_pmf(p_success, pa_dist):
    """pmf over number of successes (per-PA Bernoulli p), N ~ pa_dist."""
    maxN = max(pa_dist)
    out = np.zeros(maxN + 1)
    for N, pn in pa_dist.items():
        ks = np.arange(N + 1)
        from scipy.stats import binom
        out[:N + 1] += pn * binom.pmf(ks, N, p_success)
    return out


def _tb_pmf(perpa, pa_dist):
    base = np.zeros(5)
    for e, p in perpa.items():
        base[TB[e]] += p
    maxN = max(pa_dist)
    out = np.zeros(4 * maxN + 1)
    for N, pn in pa_dist.items():
        conv = np.array([1.0])
        for _ in range(N):
            conv = np.convolve(conv, base)
        out[:len(conv)] += pn * conv
    return out


def _p_over(pmf, line):
    """P(X > line) for a half-integer line (over wins if X >= ceil(line))."""
    need = int(np.ceil(line))
    return float(pmf[need:].sum()) if need < len(pmf) else 0.0


# stat -> which per-PA success prob, or 'tb' for the total-base convolution
def project_batter(perpa, mu_pa, stat, line):
    p = None
    if stat in ("hits",):
        p = sum(perpa[e] for e in HIT)
    elif stat == "singles":
        p = perpa["1B"]
    elif stat in ("batter walks", "walks"):
        p = perpa["BB"]
    elif stat in ("home runs", "hr"):
        p = perpa["HR"]
    if p is not None:
        pmf = _count_pmf(p, _pa_dist(mu_pa))
        po = _p_over(pmf, line)
        return po, 1 - po
    if stat in ("total bases", "total_bases"):
        pmf = _tb_pmf(perpa, _pa_dist(mu_pa))
        po = _p_over(pmf, line)
        return po, 1 - po
    if stat in ("hits+runs+rbis", "rbis", "runs"):
        # team-context dependent -> coarse proxy via reaching base / power; LOW CONFIDENCE
        reach = perpa["1B"] + perpa["2B"] + perpa["3B"] + perpa["HR"] + perpa["BB"]
        pmf = _count_pmf(reach * 0.55, _pa_dist(mu_pa))   # crude scaling
        po = _p_over(pmf, line)
        return po, 1 - po
    return None, None


LOW_CONF = {"rbis", "runs", "hits+runs+rbis"}


def project_pitcher_k(prate, lg, bf_per_start, opp_lineup_k=None, line=5.5):
    """K over/under: per-batter K prob (pitcher x opposing lineup / league), N=batters faced."""
    pk = prate["K"]
    if opp_lineup_k:
        pk = pk * opp_lineup_k / max(lg["K"], 1e-9)
    pk = min(max(pk, 0.02), 0.6)
    from scipy.stats import binom
    N = int(round(bf_per_start))
    ks = np.arange(N + 1)
    pmf = binom.pmf(ks, N, pk)
    need = int(np.ceil(line))
    po = float(pmf[need:].sum()) if need < len(pmf) else 0.0
    return po, 1 - po
