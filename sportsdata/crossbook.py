"""Cross-book prop comparison: PrizePicks vs Underdog, EXACT stat alignment.

Earlier naive joins on box_stat were garbage (lumped pitcher Ks with pitch-count).
Here we normalize stat_type to a canonical key and join on
(player_id, sport, game_date, canonical_stat). Reports:
  - line-shop value: how often the two books disagree, by how much
  - middle opportunities: outcome landed strictly between the two lines
"""
import collections
import db

# canonical stat key: maps each book's stat_type string -> shared name
CANON = {
    # MLB batting
    "total bases": "tb", "hits": "h", "runs": "r", "rbis": "rbi",
    "home runs": "hr", "singles": "1b", "doubles": "2b", "triples": "3b",
    "stolen bases": "sb",
    "walks": "bb", "batter walks": "bb",
    "hitter strikeouts": "bk", "batter strikeouts": "bk",
    "hits+runs+rbis": "hrr", "hits + runs + rbis": "hrr",
    # MLB pitching
    "pitcher strikeouts": "pk", "strikeouts": "pk",
    "earned runs allowed": "er", "hits allowed": "ha", "walks allowed": "wa",
    "pitching outs": "po",
    # NBA
    "points": "pts", "rebounds": "reb", "assists": "ast", "steals": "stl",
    "blocked shots": "blk", "blocks": "blk", "turnovers": "tov",
    "3-pt made": "3pm", "3-pointers made": "3pm", "3s made": "3pm",
    "pts+rebs": "pr", "points + rebounds": "pr",
    "pts+asts": "pa", "points + assists": "pa",
    "rebs+asts": "ra", "rebounds + assists": "ra",
    "pts+rebs+asts": "pra", "pts + rebs + asts": "pra",
    "fantasy score": "fs", "fantasy points": "fs",
    # NHL
    "shots on goal": "sog", "goalie saves": "sv", "saves": "sv", "goals": "g",
}


def norm(s):
    import re
    return CANON.get(re.sub(r"\s+", " ", (s or "").lower()).strip())


def main():
    con = db.connect()
    cur = con.cursor()
    # use graded rows: have close_line + actual outcome
    cur.execute("""SELECT sport, source, player_id, stat_type, line_type, close_line, game_date, actual
                   FROM prop_graded WHERE player_id IS NOT NULL AND close_line IS NOT NULL""")
    by = collections.defaultdict(dict)   # (sport,pid,date,canon) -> {source: (line, actual)}
    for sport, source, pid, stat_type, lt, line, gdate, actual in cur.fetchall():
        c = norm(stat_type)
        if not c:
            continue
        key = (sport, pid, gdate, c)
        # prefer standard/balanced line for fair comparison; keep first seen per source
        if source not in by[key]:
            by[key][source] = (float(line), float(actual) if actual is not None else None)

    pairs = [(k, v["prizepicks"], v["underdog"]) for k, v in by.items()
             if "prizepicks" in v and "underdog" in v]
    print(f"=== CROSS-BOOK (exact stat alignment): {len(pairs)} matched player-stat-games ===")
    if not pairs:
        con.close(); return

    bysport = collections.defaultdict(list)
    for (sport, pid, d, c), pp, ud in pairs:
        bysport[sport].append((pp, ud))

    middles = 0
    for sport, lst in sorted(bysport.items()):
        gaps = [abs(pp[0] - ud[0]) for pp, ud in lst]
        ge1 = sum(g >= 1.0 for g in gaps)
        ge15 = sum(g >= 1.5 for g in gaps)
        # middle: actual strictly between the two lines (both an over@low and under@high win)
        mids = 0
        for pp, ud in lst:
            lo, hi = sorted([pp[0], ud[0]])
            a = pp[1]
            if a is not None and lo < a < hi and (hi - lo) >= 1.0:
                mids += 1
        middles += mids
        avg = sum(gaps) / len(gaps)
        print(f"  {sport}: n={len(lst):5d}  avg_line_gap={avg:.2f}  gap>=1.0: {ge1} ({ge1/len(lst)*100:.0f}%)  "
              f"gap>=1.5: {ge15}  realized_middles(gap>=1): {mids} ({mids/len(lst)*100:.1f}%)")

    print("\nNote: DFS payouts are parlay-based, so a 'middle' is not a clean arb; "
          "line-shopping value = always taking the better of the two lines for a "
          "projection-based pick. gap>=1 share = how often shopping matters.")
    con.close()


if __name__ == "__main__":
    main()
