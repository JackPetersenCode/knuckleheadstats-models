"""+EV scanner — combine leak-free projections with Sleeper's live multipliers.

Sleeper offers near-two-way odds with a modest (~8%) hold (e.g. 2.08x over /
1.67x under on a normal line), unlike PrizePicks/Underdog (parlay-only, ~40% hold).
So a single-bet +EV test applies directly:
    EV_over  = P(over)  * over_mult  - 1
    EV_under = P(under) * under_mult - 1
Flag props where the model's best side clears a threshold.

THIS IS A CANDIDATE GENERATOR, NOT A PROVEN MONEY MACHINE. The projection model
is simple and unvalidated on enough data. Treat output as paper-trade candidates;
the daily collector logs the lines so realized ROI / CLV can confirm or refute.
"""
import sys, datetime, bisect
import db
from proj_backtest import STATS, nstat, project, pois_sf, norm_cdf, load_box

# Empirical calibration map (model P(over) -> realized over-rate), fit on the
# graded sample. Model is OVERCONFIDENT at the extremes, so shrink toward truth.
# (bin centers -> actual rate, from calibration run 2026-06-06)
_CAL_X = [0.046, 0.148, 0.250, 0.352, 0.453, 0.551, 0.649, 0.747, 0.848, 0.925]
_CAL_Y = [0.120, 0.203, 0.274, 0.409, 0.439, 0.543, 0.568, 0.612, 0.690, 0.737]


def calibrate(p):
    """Map raw model P(over) to a realistic probability via the empirical curve."""
    if p <= _CAL_X[0]:
        return _CAL_Y[0] * (p / _CAL_X[0]) if _CAL_X[0] > 0 else _CAL_Y[0]
    if p >= _CAL_X[-1]:
        return _CAL_Y[-1]
    i = bisect.bisect_right(_CAL_X, p) - 1
    x0, x1, y0, y1 = _CAL_X[i], _CAL_X[i + 1], _CAL_Y[i], _CAL_Y[i + 1]
    return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


def recent_games(series, pid, gdate, days=24):
    cut = gdate - datetime.timedelta(days=days)
    return sum(1 for r in series.get(pid, []) if cut <= r["game_date"] < gdate)


def main(target_date=None, min_ev=0.05, min_games=8, min_recent=6):
    con = db.connect()
    cur = con.cursor()
    target_date = target_date or datetime.date.today()

    # latest snapshot per Sleeper grading-unit for the target date, with multipliers
    cur.execute("""
      SELECT sport, source_player_id, player_name, stat_type, line_type,
             (array_agg(line ORDER BY snapshot_ts DESC))[1]       AS line,
             (array_agg(over_mult ORDER BY snapshot_ts DESC))[1]  AS over_mult,
             (array_agg(under_mult ORDER BY snapshot_ts DESC))[1] AS under_mult
      FROM odds_prop_snapshot
      WHERE source='sleeper' AND over_mult IS NOT NULL AND under_mult IS NOT NULL
        AND snapshot_ts::date = %s
      GROUP BY sport, source_player_id, player_name, stat_type, line_type
    """, (target_date,))
    props = cur.fetchall()

    # xwalk sleeper player -> our player_id
    cur.execute("SELECT sport, source_player_id, player_id FROM player_xwalk WHERE source='sleeper' AND player_id IS NOT NULL")
    xw = {(s, sp): pid for s, sp, pid in cur.fetchall()}

    boxes = {}
    def get_box(t):
        if t not in boxes:
            boxes[t] = load_box(con, t)
        return boxes[t]

    plays = []
    for sport, spid, pname, stat_type, lt, line, over_mult, under_mult in props:
        key = (sport, nstat(stat_type))
        if key not in STATS:
            continue
        pid = xw.get((sport, str(spid)))
        if not pid:
            continue
        table, col, dist = STATS[key]
        series = get_box(table)
        # playing-time guard: skip fringe players (low projections -> void-risk artifacts)
        if recent_games(series, pid, target_date) < min_recent:
            continue
        proj = project(series, pid, col, target_date, min_games=min_games)
        if proj is None:
            continue
        mu, sd, n = proj
        line = float(line)
        p_over_raw = pois_sf(line, mu) if dist == "pois" else 1 - norm_cdf(line, mu, sd)
        p_over = calibrate(p_over_raw)          # shrink toward realized rates
        p_under = 1 - p_over
        ev_o = p_over * float(over_mult) - 1
        ev_u = p_under * float(under_mult) - 1
        if ev_o >= ev_u:
            side, ev, p, mult = "OVER", ev_o, p_over, float(over_mult)
        else:
            side, ev, p, mult = "UNDER", ev_u, p_under, float(under_mult)
        if ev >= min_ev:
            plays.append((ev, sport, pname, stat_type, line, side, mult, p, mu, n))

    plays.sort(reverse=True)
    print(f"=== Sleeper +EV candidates for {target_date}  (min_ev={min_ev:.0%}, model min {min_games} games) ===")
    print(f"{'EV':>6} {'SPORT':5} {'PLAYER':22} {'STAT':18} {'LINE':>6} {'SIDE':5} {'MULT':>5} {'P':>5} {'PROJ':>6} g")
    for ev, sport, pname, stat_type, line, side, mult, p, mu, n in plays[:40]:
        print(f"{ev:+6.1%} {sport:5} {(pname or '?')[:22]:22} {stat_type[:18]:18} "
              f"{line:6.1f} {side:5} {mult:5.2f} {p:5.2f} {mu:6.1f} {n}")
    print(f"\n{len(plays)} candidate +EV plays. (paper-trade; results logged for CLV/ROI validation)")
    con.close()


if __name__ == "__main__":
    d = datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    main(d)
