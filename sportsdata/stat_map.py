"""Map DFS prop stat_type -> actual value from a box-score row.

Each entry: normalized_stat -> (box_table, fn(row)->value).
box_table picks which table to read (mlb batting vs pitching; nhl skater vs goalie).
Stats not here (period/quarter splits, 'first 3 minutes', fantasy score,
NFL season-long futures, double-double, first-scorer) are intentionally ungradeable.
"""
import re


def nstat(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def g(col):
    return lambda r: r.get(col)


NBA = {
    "points": ("nba", g("pts")), "rebounds": ("nba", g("reb")), "assists": ("nba", g("ast")),
    "steals": ("nba", g("stl")), "blocked shots": ("nba", g("blk")), "blocks": ("nba", g("blk")),
    "turnovers": ("nba", g("tov")),
    "3-pt made": ("nba", g("fg3m")), "3-pointers made": ("nba", g("fg3m")), "3s made": ("nba", g("fg3m")),
    "3-pt attempted": ("nba", g("fg3a")), "3s attempted": ("nba", g("fg3a")),
    "fg made": ("nba", g("fgm")), "fg attempted": ("nba", g("fga")),
    "ft made": ("nba", g("ftm")), "free throws made": ("nba", g("ftm")),
    "ft attempted": ("nba", g("fta")), "free throws attempted": ("nba", g("fta")),
    "offensive rebounds": ("nba", g("oreb")), "defensive rebounds": ("nba", g("dreb")),
    "personal fouls": ("nba", g("pf")),
    "two pointers made": ("nba", lambda r: _sub(r, "fgm", "fg3m")),
    "two pointers attempted": ("nba", lambda r: _sub(r, "fga", "fg3a")),
    "pts+rebs": ("nba", lambda r: _sum(r, "pts", "reb")), "points + rebounds": ("nba", lambda r: _sum(r, "pts", "reb")),
    "pts+asts": ("nba", lambda r: _sum(r, "pts", "ast")), "points + assists": ("nba", lambda r: _sum(r, "pts", "ast")),
    "rebs+asts": ("nba", lambda r: _sum(r, "reb", "ast")), "rebounds + assists": ("nba", lambda r: _sum(r, "reb", "ast")),
    "pts+rebs+asts": ("nba", lambda r: _sum(r, "pts", "reb", "ast")),
    "pts + rebs + asts": ("nba", lambda r: _sum(r, "pts", "reb", "ast")),
    "blks+stls": ("nba", lambda r: _sum(r, "blk", "stl")), "blocks + steals": ("nba", lambda r: _sum(r, "blk", "stl")),
}
MLB = {
    "total bases": ("bat", g("tb")), "hits": ("bat", g("h")), "runs": ("bat", g("r")),
    "rbis": ("bat", g("rbi")), "home runs": ("bat", g("hr")), "doubles": ("bat", g("doubles")),
    "triples": ("bat", g("triples")), "walks": ("bat", g("bb")), "batter walks": ("bat", g("bb")),
    "stolen bases": ("bat", g("sb")), "hitter strikeouts": ("bat", g("k")), "batter strikeouts": ("bat", g("k")),
    "singles": ("bat", lambda r: _sub(r, "h", "doubles", "triples", "hr")),
    "hits+runs+rbis": ("bat", lambda r: _sum(r, "h", "r", "rbi")),
    "hits + runs + rbis": ("bat", lambda r: _sum(r, "h", "r", "rbi")),
    "pitcher strikeouts": ("pit", g("k")), "strikeouts": ("pit", g("k")),
    "earned runs allowed": ("pit", g("er")), "hits allowed": ("pit", g("h")),
    "walks allowed": ("pit", g("bb")), "pitches thrown": ("pit", g("pitches")),
    "pitching outs": ("pit", lambda r: None if r.get("ip") is None else round(float(r["ip"]) * 3)),
}
NHL = {
    "shots on goal": ("sk", g("shots")), "points": ("sk", g("points")), "assists": ("sk", g("assists")),
    "goals": ("sk", g("goals")), "blocked shots": ("sk", g("blocks")), "hits": ("sk", g("hits")),
    "plus minus": ("sk", g("plus_minus")), "plus/minus": ("sk", g("plus_minus")), "time on ice": ("sk", g("toi")),
    "goalie saves": ("gl", g("saves")), "saves": ("gl", g("saves")),
    "goals against": ("gl", g("goals_against")), "goals allowed": ("gl", g("goals_against")),
}
NFL = {
    "receiving yards": ("nfl", g("rec_yds")), "rush yards": ("nfl", g("rush_yds")),
    "pass yards": ("nfl", g("pass_yds")),
    "rush+rec tds": ("nfl", lambda r: _sum(r, "rush_td", "rec_td")),
}
MAPS = {"nba": NBA, "mlb": MLB, "nhl": NHL, "nfl": NFL}

# which physical table each logical box-source reads from
TABLE = {"nba": "nba_player_box", "nfl": "nfl_player_box",
         "bat": "mlb_batting_box", "pit": "mlb_pitching_box",
         "sk": "nhl_skater_box", "gl": "nhl_goalie_box"}


def _sum(r, *cols):
    vals = [r.get(c) for c in cols]
    return sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None


def _sub(r, a, *rest):
    if r.get(a) is None:
        return None
    return r[a] - sum(r.get(c) or 0 for c in rest)


def lookup(sport, stat_type):
    """-> (box_source, fn) or (None, None) if ungradeable."""
    return MAPS.get(sport, {}).get(nstat(stat_type), (None, None))
