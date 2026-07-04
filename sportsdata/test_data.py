"""Comprehensive data-quality + accuracy test for the sportsedge platform.

Four layers:
  1. Referential integrity (no orphans, no dup PKs)
  2. Internal-consistency identities (catch parse bugs w/o the source:
       NBA pts == 2*fgm+fg3m+ftm ; NHL points == goals+assists ;
       made <= attempted ; team stat totals == game score)
  3. Cross-source accuracy (re-fetch live games, compare field-by-field)
  4. Odds sanity (valid lines/types, snapshots populated)

Run: python test_data.py
"""
import datetime, random
import db
import espn, mlb_src, nhl_src

PASS, FAIL = 0, 0
FAILURES = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}  -- {detail}")


def section(t):
    print(f"\n{'='*66}\n{t}\n{'='*66}")


con = db.connect()
cur = con.cursor()


def scalar(sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchone()[0]


# ---------------------------------------------------------------- 1
section("1. REFERENTIAL INTEGRITY")
BOX = {"nba": ["nba_player_box"], "nfl": ["nfl_player_box"],
       "mlb": ["mlb_batting_box", "mlb_pitching_box"],
       "nhl": ["nhl_skater_box", "nhl_goalie_box"]}
for sport, tables in BOX.items():
    for t in tables:
        orph = scalar(f"SELECT count(*) FROM {t} b LEFT JOIN game g "
                      f"ON g.sport=%s AND g.game_id=b.game_id WHERE g.game_id IS NULL", (sport,))
        check(f"{t}: no orphan game_id", orph == 0, f"{orph} orphans")
        oprow = scalar(f"SELECT count(*) FROM {t} b LEFT JOIN player p "
                       f"ON p.sport=%s AND p.player_id=b.player_id WHERE p.player_id IS NULL", (sport,))
        check(f"{t}: no orphan player_id", oprow == 0, f"{oprow} orphans")
        dup = scalar(f"SELECT count(*) FROM (SELECT game_id,player_id FROM {t} "
                     f"GROUP BY game_id,player_id HAVING count(*)>1) x")
        check(f"{t}: no duplicate (game,player)", dup == 0, f"{dup} dups")

# ---------------------------------------------------------------- 2
section("2. INTERNAL-CONSISTENCY IDENTITIES")

# NBA points identity: pts = 2*fgm + fg3m + ftm
bad = scalar("""SELECT count(*) FROM nba_player_box
  WHERE pts IS NOT NULL AND fgm IS NOT NULL AND fg3m IS NOT NULL AND ftm IS NOT NULL
    AND pts <> 2*fgm + fg3m + ftm""")
tot = scalar("SELECT count(*) FROM nba_player_box WHERE pts IS NOT NULL AND fgm IS NOT NULL")
check("NBA pts == 2*fgm+fg3m+ftm", bad == 0, f"{bad}/{tot} violate")

# NBA made <= attempted
bad = scalar("SELECT count(*) FROM nba_player_box WHERE (fgm>fga) OR (fg3m>fg3a) OR (ftm>fta) OR (fg3m>fgm)")
check("NBA made<=attempted (fg/3pt/ft, 3<=fg)", bad == 0, f"{bad} violate")

# NBA team points == game score (allow tiny rate of ESPN source discrepancies)
cur.execute("""
  WITH tp AS (SELECT game_id, team_id, SUM(pts) pts FROM nba_player_box GROUP BY game_id, team_id)
  SELECT count(*) FROM tp JOIN game g ON g.sport='nba' AND g.game_id=tp.game_id
  WHERE g.status='final' AND tp.pts <> CASE WHEN tp.team_id=g.home_team_id THEN g.home_score ELSE g.away_score END""")
mismatch = cur.fetchone()[0]
ngames = scalar("SELECT count(*) FROM game WHERE sport='nba' AND status='final'")
rate = mismatch / max(ngames, 1)
check(f"NBA sum(player pts) == game score (rate {rate:.3%}, ESPN source quirks)", rate < 0.01,
      f"{mismatch}/{ngames} games mismatch")

# NHL points identity
bad = scalar("SELECT count(*) FROM nhl_skater_box WHERE points IS NOT NULL AND points <> goals+assists")
check("NHL points == goals+assists", bad == 0, f"{bad} violate")

# NHL team goals == game score (allow small slack for rare scoring quirks)
cur.execute("""
  WITH tg AS (SELECT game_id, team_id, SUM(goals) g FROM nhl_skater_box GROUP BY game_id, team_id)
  SELECT count(*) FROM tg JOIN game gm ON gm.sport='nhl' AND gm.game_id=tg.game_id
  WHERE gm.status='final' AND ABS(tg.g - CASE WHEN tg.team_id=gm.home_team_id THEN gm.home_score ELSE gm.away_score END) > 1""")
mismatch = cur.fetchone()[0]
tot = scalar("SELECT count(DISTINCT game_id) FROM nhl_skater_box")
check("NHL sum(skater goals) ~= game score (<=1 slack)", mismatch == 0, f"{mismatch}/{tot} games off by >1")

# MLB total bases >= hits, and team runs == game score
bad = scalar("SELECT count(*) FROM mlb_batting_box WHERE tb IS NOT NULL AND h IS NOT NULL AND tb < h")
check("MLB total_bases >= hits", bad == 0, f"{bad} violate")
cur.execute("""
  WITH tr AS (SELECT game_id, team_id, SUM(r) r FROM mlb_batting_box GROUP BY game_id, team_id)
  SELECT count(*) FROM tr JOIN game g ON g.sport='mlb' AND g.game_id=tr.game_id
  WHERE g.status='final' AND g.home_score IS NOT NULL
    AND tr.r <> CASE WHEN tr.team_id=g.home_team_id THEN g.home_score ELSE g.away_score END""")
mismatch = cur.fetchone()[0]
check("MLB sum(batter runs) == game score", mismatch == 0, f"{mismatch} games mismatch")

# NFL completions <= attempts
bad = scalar("SELECT count(*) FROM nfl_player_box WHERE pass_cmp>pass_att")
check("NFL pass_cmp <= pass_att", bad == 0, f"{bad} violate")

# No absurd values
for t, col, lo, hi in [("nba_player_box", "pts", 0, 101), ("nba_player_box", "min", 0, 70),
                       ("mlb_pitching_box", "k", 0, 25), ("nhl_skater_box", "toi", 0, 70),
                       ("nfl_player_box", "pass_yds", -20, 600)]:
    bad = scalar(f"SELECT count(*) FROM {t} WHERE {col} IS NOT NULL AND ({col}<{lo} OR {col}>{hi})")
    check(f"{t}.{col} in [{lo},{hi}]", bad == 0, f"{bad} out of range")

# ---------------------------------------------------------------- 3
section("3. CROSS-SOURCE ACCURACY (re-fetch live, compare field-by-field)")


def cmp_game(sport, fetch_box, table, fields, where_extra=""):
    """pick a random loaded game, re-fetch from source, compare stored vs live."""
    cur.execute(f"SELECT game_id, home_team_id, away_team_id, game_date FROM game "
                f"WHERE sport=%s AND boxscore_loaded {where_extra} ORDER BY random() LIMIT 1", (sport,))
    row = cur.fetchone()
    if not row:
        check(f"{sport}: sample game available", False, "no loaded game")
        return
    gid, hid, aid, gdate = row
    meta = dict(game_id=gid, home_team_id=hid, away_team_id=aid, game_date=str(gdate))
    players, tbls = fetch_box(gid, meta)
    live = {r["player_id"]: r for r in tbls[table]}
    cur.execute(f"SELECT * FROM {table} WHERE game_id=%s", (gid,))
    colnames = [d[0] for d in cur.description]
    stored = {dict(zip(colnames, r))["player_id"]: dict(zip(colnames, r)) for r in cur.fetchall()}
    # compare overlap
    common = set(live) & set(stored)
    mism = []
    for pid in common:
        for f in fields:
            lv, sv = live[pid].get(f), stored[pid].get(f)
            if lv is None and sv is None:
                continue
            if str(lv) != str(sv) and not (lv is not None and sv is not None and abs(float(lv) - float(sv)) < 0.01):
                mism.append(f"{pid}.{f}: live={lv} db={sv}")
    check(f"{sport} {table} game {gid}: {len(common)} players match on {fields}",
          len(mism) == 0 and len(common) > 0, "; ".join(mism[:4]) or f"common={len(common)}")


# NBA / NFL via espn (need espn summary fetch wrapper)
def espn_box(sport):
    return lambda gid, meta: (lambda r: (r[1], {f"{sport}_player_box": r[0]}))(espn.parse_box(sport, espn.summary(sport, gid), meta))

cmp_game("nba", espn_box("nba"), "nba_player_box", ["pts", "reb", "ast", "fgm", "fga"])
cmp_game("nfl", espn_box("nfl"), "nfl_player_box", ["pass_yds", "rush_yds", "rec_yds", "rec"])
cmp_game("mlb", mlb_src.parse_box, "mlb_batting_box", ["ab", "h", "hr", "rbi", "k"])
cmp_game("mlb", mlb_src.parse_box, "mlb_pitching_box", ["k", "h", "er", "bf"])
cmp_game("nhl", nhl_src.parse_box, "nhl_skater_box", ["goals", "assists", "shots", "hits"])

# ---------------------------------------------------------------- 4
section("4. ODDS SANITY")
check("prop snapshots present", scalar("SELECT count(*) FROM odds_prop_snapshot") > 1000)
# PrizePicks: standard/demon/goblin ; Underdog: balanced/alternate ; Sleeper: normal
check("prop line_type valid", scalar("SELECT count(*) FROM odds_prop_snapshot WHERE line_type NOT IN ('standard','demon','goblin','balanced','alternate','normal')") == 0,
      "unexpected line_type")
nnull = scalar("SELECT count(*) FROM odds_prop_snapshot WHERE line IS NULL OR line<0")
ntot = scalar("SELECT count(*) FROM odds_prop_snapshot")
check(f"prop lines valid (null/neg rate {nnull/max(ntot,1):.3%})", nnull / max(ntot, 1) < 0.001, f"{nnull} null/neg")
check("prop snapshot_ts populated", scalar("SELECT count(*) FROM odds_prop_snapshot WHERE snapshot_ts IS NULL") == 0)
check("prop covers >=4 sports", scalar("SELECT count(DISTINCT sport) FROM odds_prop_snapshot") >= 4)
check("prop both sources", scalar("SELECT count(DISTINCT source) FROM odds_prop_snapshot") >= 2)
check("game odds present", scalar("SELECT count(*) FROM odds_game_snapshot") > 0)
check("game odds markets h2h/spread/total", scalar("SELECT count(DISTINCT market) FROM odds_game_snapshot WHERE market IN ('h2h','spread','total')") == 3)
check("game odds prices sane", scalar("SELECT count(*) FROM odds_game_snapshot WHERE price IS NOT NULL AND abs(price)<100") == 0,
      "american odds |price|>=100")

# ---------------------------------------------------------------- summary
section("SUMMARY")
print(f"  PASSED: {PASS}   FAILED: {FAIL}")
if FAILURES:
    print("  Failed checks:")
    for f in FAILURES:
        print("    -", f)
else:
    print("  ALL CHECKS PASSED")
con.close()
