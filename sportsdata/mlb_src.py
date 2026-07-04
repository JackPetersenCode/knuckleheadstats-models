"""MLB source — official statsapi.mlb.com (no key).
schedule: /api/v1/schedule?sportId=1&date=YYYY-MM-DD
boxscore: /api/v1/game/{gamePk}/boxscore
"""
from http_util import get_json, to_int, to_num

BASE = "https://statsapi.mlb.com/api/v1"
_TEAMS = {}  # id(str) -> dict


def _load_teams():
    if _TEAMS:
        return
    d = get_json(f"{BASE}/teams?sportId=1")
    for t in d.get("teams", []):
        _TEAMS[str(t["id"])] = dict(abbrev=t.get("abbreviation"), name=t.get("name"),
                                    location=t.get("locationName"), display=t.get("name"),
                                    division=(t.get("division") or {}).get("name"))


def _season_type(gt):
    if gt == "R":
        return "regular"
    if gt == "S":
        return "preseason"
    return "postseason"  # F,D,L,W,C,P


def _ip_to_innings(ip):
    """'5.2' -> 5 + 2/3 outs = 5.667 true innings."""
    if ip in (None, ""):
        return None
    s = str(ip)
    if "." in s:
        whole, frac = s.split(".", 1)
        return round(int(whole) + int(frac[0]) / 3.0, 3)
    return float(s)


def list_games(date):
    _load_teams()
    d = get_json(f"{BASE}/schedule?sportId=1&date={date.isoformat()}")
    games, teams = [], {}
    for dt in d.get("dates", []):
        for g in dt.get("games", []):
            h, a = g["teams"]["home"], g["teams"]["away"]
            hid, aid = str(h["team"]["id"]), str(a["team"]["id"])
            for tid, side in ((hid, h), (aid, a)):
                ti = _TEAMS.get(tid, {})
                teams[tid] = dict(sport="mlb", team_id=tid, source="mlb",
                                  name=ti.get("name"), abbrev=ti.get("abbrev"),
                                  location=ti.get("location"), display_name=ti.get("display"),
                                  conference=None, division=ti.get("division"))
            state = g["status"]["detailedState"]
            status = "final" if state in ("Final", "Game Over", "Completed Early") else \
                     ("in" if "In Progress" in state or state == "Manager Challenge" else "scheduled")
            games.append(dict(sport="mlb", game_id=str(g["gamePk"]), source="mlb",
                              season=g.get("season"), season_type=_season_type(g.get("gameType")),
                              game_date=g.get("officialDate"), start_ts=g.get("gameDate"),
                              home_team_id=hid, away_team_id=aid,
                              home_score=h.get("score"), away_score=a.get("score"),
                              status=status, venue=(g.get("venue") or {}).get("name")))
    return games, list(teams.values())


def parse_box(gamePk, meta):
    bx = get_json(f"{BASE}/game/{gamePk}/boxscore")
    players, batting, pitching = [], [], []
    hid, aid = meta["home_team_id"], meta["away_team_id"]
    gd = meta["game_date"]
    for side in ("home", "away"):
        tm = bx["teams"][side]
        team_id = str(tm["team"]["id"])
        is_home = team_id == hid
        opp = aid if is_home else hid
        for _, P in tm["players"].items():
            pid = str(P["person"]["id"])
            players.append(dict(sport="mlb", player_id=pid, source="mlb",
                                full_name=P["person"]["fullName"],
                                position=(P.get("position") or {}).get("abbreviation"),
                                current_team_id=team_id))
            st = P.get("stats", {})
            b = st.get("batting", {})
            if b.get("atBats") is not None or b.get("plateAppearances"):
                order = P.get("battingOrder")
                batting.append(dict(game_id=str(gamePk), player_id=pid, game_date=gd,
                    team_id=team_id, opp_team_id=opp, is_home=is_home,
                    batting_order=int(order) // 100 if order else None,
                    ab=b.get("atBats"), r=b.get("runs"), h=b.get("hits"),
                    doubles=b.get("doubles"), triples=b.get("triples"), hr=b.get("homeRuns"),
                    rbi=b.get("rbi"), bb=b.get("baseOnBalls"), k=b.get("strikeOuts"),
                    sb=b.get("stolenBases"), cs=b.get("caughtStealing"), hbp=b.get("hitByPitch"),
                    tb=b.get("totalBases"), lob=b.get("leftOnBase")))
            p = st.get("pitching", {})
            if p.get("inningsPitched") is not None:
                pitching.append(dict(game_id=str(gamePk), player_id=pid, game_date=gd,
                    team_id=team_id, opp_team_id=opp, is_home=is_home,
                    started=(p.get("gamesStarted", 0) or 0) > 0,
                    ip=_ip_to_innings(p.get("inningsPitched")), h=p.get("hits"), r=p.get("runs"),
                    er=p.get("earnedRuns"), bb=p.get("baseOnBalls"), k=p.get("strikeOuts"),
                    hr=p.get("homeRuns"), bf=p.get("battersFaced"),
                    pitches=p.get("numberOfPitches"), strikes=p.get("strikes")))
    return players, {"mlb_batting_box": batting, "mlb_pitching_box": pitching}
