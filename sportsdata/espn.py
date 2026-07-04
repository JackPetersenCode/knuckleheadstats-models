"""ESPN source — used for NBA and NFL (official stats.nba.com is firewalled here;
NFL has no open official API). Also provides the universal scoreboard.

Endpoints (no key needed):
  scoreboard: site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates=YYYYMMDD
  summary:    .../summary?event={id}
"""
from http_util import get_json, to_int, to_num, split_made_att

PATH = {"nba": "basketball/nba", "nfl": "football/nfl"}
SEASON_TYPE = {1: "preseason", 2: "regular", 3: "postseason", 4: "offseason"}

BASE = "https://site.api.espn.com/apis/site/v2/sports"


def scoreboard(sport, yyyymmdd):
    return get_json(f"{BASE}/{PATH[sport]}/scoreboard?dates={yyyymmdd}&limit=400")


def summary(sport, event_id):
    return get_json(f"{BASE}/{PATH[sport]}/summary?event={event_id}")


def parse_games(sport, sb):
    """scoreboard json -> (games[], teams[])."""
    games, teams = [], {}
    for ev in sb.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        season = ev.get("season", {})
        home = away = None
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            teams[t.get("id")] = dict(
                sport=sport, team_id=t.get("id"), source="espn",
                name=t.get("name"), abbrev=t.get("abbreviation"),
                location=t.get("location"), display_name=t.get("displayName"),
                conference=None, division=None,
            )
            if c.get("homeAway") == "home":
                home = (t.get("id"), to_int(c.get("score")))
            else:
                away = (t.get("id"), to_int(c.get("score")))
        st = comp.get("status", ev.get("status", {})).get("type", {})
        status = "final" if st.get("completed") else ("in" if st.get("state") == "in" else "scheduled")
        games.append(dict(
            sport=sport, game_id=ev.get("id"), source="espn",
            season=season.get("year"), season_type=SEASON_TYPE.get(season.get("type")),
            game_date=ev.get("date", "")[:10] or None, start_ts=ev.get("date"),
            home_team_id=home[0] if home else None, away_team_id=away[0] if away else None,
            home_score=home[1] if home else None, away_score=away[1] if away else None,
            status=status, venue=(comp.get("venue", {}) or {}).get("fullName"),
        ))
    return games, list(teams.values())


# label -> column maps
_NBA = {"MIN": "min", "PTS": "pts", "REB": "reb", "AST": "ast", "TO": "tov",
        "STL": "stl", "BLK": "blk", "OREB": "oreb", "DREB": "dreb", "PF": "pf", "+/-": "plus_minus"}
_NBA_SPLIT = {"FG": ("fgm", "fga"), "3PT": ("fg3m", "fg3a"), "FT": ("ftm", "fta")}

# NFL: stat group name -> {label: column}. Plain-int labels only here;
# C/ATT ("25/38") and SACKS ("2-14") are split specially in parse_box.
_NFL = {
    "passing": {"YDS": "pass_yds", "TD": "pass_td", "INT": "pass_int", "QBR": "qbr", "RTG": "pass_rtg"},
    "rushing": {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td", "LONG": "rush_long"},
    "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td", "LONG": "rec_long", "TGTS": "rec_tgts"},
    "fumbles": {"FUM": "fum", "LOST": "fum_lost"},
}
_NFL_NUM = {"qbr", "pass_rtg"}


def parse_box(sport, summ, game_meta):
    """summary json -> list of player-box dicts (sport-specific cols) + players[]."""
    box = summ.get("boxscore", {})
    blocks = box.get("players", [])
    if not blocks:
        return [], []
    # map team id -> opp id / is_home from game_meta
    home_id, away_id = game_meta["home_team_id"], game_meta["away_team_id"]
    rows, players = {}, {}

    for blk in blocks:
        team_id = blk.get("team", {}).get("id")
        is_home = team_id == home_id
        opp = away_id if is_home else home_id
        for grp in blk.get("statistics", []):
            labels = grp.get("labels", [])
            gname = (grp.get("name") or "").lower()
            for a in grp.get("athletes", []):
                ath = a.get("athlete", {})
                pid = ath.get("id")
                if not pid:
                    continue
                players[pid] = dict(sport=sport, player_id=pid, source="espn",
                                    full_name=ath.get("displayName"),
                                    position=(ath.get("position") or {}).get("abbreviation"),
                                    current_team_id=team_id)
                r = rows.setdefault(pid, dict(
                    game_id=game_meta["game_id"], player_id=pid, game_date=game_meta["game_date"],
                    team_id=team_id, opp_team_id=opp, is_home=is_home))
                stats = a.get("stats", [])
                if not stats:
                    continue
                vals = dict(zip(labels, stats))
                if sport == "nba":
                    r["starter"] = a.get("starter", False)
                    for lab, col in _NBA.items():
                        if lab in vals:
                            r[col] = to_num(vals[lab]) if col == "min" else to_int(vals[lab])
                    for lab, (m, at) in _NBA_SPLIT.items():
                        if lab in vals:
                            mm, aa = split_made_att(vals[lab])
                            r[m], r[at] = mm, aa
                elif sport == "nfl":
                    cmap = _NFL.get(gname, {})
                    for lab, col in cmap.items():
                        if lab in vals:
                            r[col] = to_num(vals[lab]) if col in _NFL_NUM else to_int(vals[lab])
                    if gname == "passing":
                        if "C/ATT" in vals and "/" in str(vals["C/ATT"]):
                            c, a = str(vals["C/ATT"]).split("/", 1)
                            r["pass_cmp"], r["pass_att"] = to_int(c), to_int(a)
                        if "SACKS" in vals:  # 'sacks-yardsLost' -> take sacks
                            r["pass_sacked"] = to_int(str(vals["SACKS"]).split("-", 1)[0])
    return list(rows.values()), list(players.values())
