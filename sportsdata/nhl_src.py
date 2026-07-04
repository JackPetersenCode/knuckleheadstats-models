"""NHL source — official api-web.nhle.com (no key).
schedule: /v1/score/YYYY-MM-DD
boxscore: /v1/gamecenter/{id}/boxscore
"""
from http_util import get_json, to_int, to_num

BASE = "https://api-web.nhle.com/v1"


def _toi(s):
    """'19:12' -> 19.2 minutes."""
    if not s or ":" not in str(s):
        return to_num(s)
    m, sec = str(s).split(":", 1)
    return round(int(m) + int(sec) / 60.0, 2)


def _season_type(gt):
    return {1: "preseason", 2: "regular", 3: "postseason"}.get(gt, "regular")


def _name(p):
    n = p.get("name")
    return n.get("default") if isinstance(n, dict) else n


def list_games(date):
    d = get_json(f"{BASE}/score/{date.isoformat()}")
    games, teams = [], {}
    for g in d.get("games", []):
        h, a = g["homeTeam"], g["awayTeam"]
        hid, aid = str(h["id"]), str(a["id"])
        for tid, t in ((hid, h), (aid, a)):
            teams[tid] = dict(sport="nhl", team_id=tid, source="nhl",
                              name=_name(t) if t.get("name") else t.get("abbrev"),
                              abbrev=t.get("abbrev"), location=t.get("placeName", {}).get("default") if isinstance(t.get("placeName"), dict) else None,
                              display_name=_name(t) if t.get("name") else t.get("abbrev"),
                              conference=None, division=None)
        state = g.get("gameState")
        status = "final" if state in ("OFF", "FINAL") else ("in" if state in ("LIVE", "CRIT") else "scheduled")
        season = g.get("season")  # 20252026
        games.append(dict(sport="nhl", game_id=str(g["id"]), source="nhl",
                          season=int(str(season)[4:]) if season else None,
                          season_type=_season_type(g.get("gameType")),
                          game_date=g.get("gameDate"), start_ts=g.get("startTimeUTC"),
                          home_team_id=hid, away_team_id=aid,
                          home_score=h.get("score"), away_score=a.get("score"),
                          status=status, venue=(g.get("venue") or {}).get("default") if isinstance(g.get("venue"), dict) else None))
    return games, list(teams.values())


def parse_box(gid, meta):
    bx = get_json(f"{BASE}/gamecenter/{gid}/boxscore")
    pbg = bx.get("playerByGameStats", {})
    hid, aid = meta["home_team_id"], meta["away_team_id"]
    gd = meta["game_date"]
    players, skaters, goalies = [], [], []
    for side, team_id in (("homeTeam", hid), ("awayTeam", aid)):
        is_home = side == "homeTeam"
        opp = aid if is_home else hid
        grp = pbg.get(side, {})
        for sk in grp.get("forwards", []) + grp.get("defense", []):
            pid = str(sk["playerId"])
            players.append(dict(sport="nhl", player_id=pid, source="nhl", full_name=_name(sk),
                                position=sk.get("position"), current_team_id=team_id))
            skaters.append(dict(game_id=str(gid), player_id=pid, game_date=gd,
                team_id=team_id, opp_team_id=opp, is_home=is_home, position=sk.get("position"),
                goals=sk.get("goals"), assists=sk.get("assists"), points=sk.get("points"),
                shots=sk.get("sog"), plus_minus=sk.get("plusMinus"), pim=sk.get("pim"),
                hits=sk.get("hits"), blocks=sk.get("blockedShots"),
                giveaways=sk.get("giveaways"), takeaways=sk.get("takeaways"),
                toi=_toi(sk.get("toi")), ppg=sk.get("powerPlayGoals"),
                faceoff_pct=sk.get("faceoffWinningPctg")))
        for gl in grp.get("goalies", []):
            pid = str(gl["playerId"])
            players.append(dict(sport="nhl", player_id=pid, source="nhl", full_name=_name(gl),
                                position="G", current_team_id=team_id))
            goalies.append(dict(game_id=str(gid), player_id=pid, game_date=gd,
                team_id=team_id, opp_team_id=opp, is_home=is_home,
                shots_against=gl.get("shotsAgainst"), saves=gl.get("saves"),
                goals_against=gl.get("goalsAgainst"), save_pct=to_num(gl.get("savePctg")),
                toi=_toi(gl.get("toi")), decision=gl.get("decision")))
    return players, {"nhl_skater_box": skaters, "nhl_goalie_box": goalies}
