"""Stats collection orchestrator.

collect_date(sport, date):
  1. scoreboard -> upsert teams + games
  2. for each completed game not yet loaded -> boxscore -> upsert players + box rows
     -> mark game.boxscore_loaded

Sources: nba/nfl via ESPN; mlb via statsapi; nhl via api-web.
Box contract: each source's fetch_box(gid, meta) -> (players[], {table_name: rows[]}).
"""
import time, random, datetime, argparse
import db
import espn

COLS = {
    "nba_player_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "starter", "min", "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
        "reb", "ast", "stl", "blk", "tov", "pf", "plus_minus"],
    "nfl_player_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int", "pass_sacked", "qbr", "pass_rtg",
        "rush_att", "rush_yds", "rush_td", "rush_long",
        "rec", "rec_tgts", "rec_yds", "rec_td", "rec_long", "fum", "fum_lost"],
    "mlb_batting_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "batting_order", "ab", "r", "h", "doubles", "triples", "hr", "rbi", "bb", "k",
        "sb", "cs", "hbp", "tb", "lob"],
    "mlb_pitching_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "started", "ip", "h", "r", "er", "bb", "k", "hr", "bf", "pitches", "strikes"],
    "nhl_skater_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "position", "goals", "assists", "points", "shots", "plus_minus", "pim", "hits", "blocks",
        "giveaways", "takeaways", "toi", "ppg", "faceoff_pct"],
    "nhl_goalie_box": ["game_id", "player_id", "game_date", "team_id", "opp_team_id", "is_home",
        "shots_against", "saves", "goals_against", "save_pct", "toi", "decision"],
}


def _norm(rows, cols):
    return [{c: r.get(c) for c in cols} for r in rows]


def _list_games(sport, date):
    if sport in ("nba", "nfl"):
        return espn.parse_games(sport, espn.scoreboard(sport, date.strftime("%Y%m%d")))
    if sport == "mlb":
        import mlb_src; return mlb_src.list_games(date)
    if sport == "nhl":
        import nhl_src; return nhl_src.list_games(date)
    raise ValueError(sport)


def _fetch_box(sport, gid, meta):
    """-> (players[], {table: rows[]})"""
    if sport in ("nba", "nfl"):
        rows, players = espn.parse_box(sport, espn.summary(sport, gid), meta)
        return players, {f"{sport}_player_box": rows}
    if sport == "mlb":
        import mlb_src; return mlb_src.parse_box(gid, meta)
    if sport == "nhl":
        import nhl_src; return nhl_src.parse_box(gid, meta)


def collect_date(sport, date, con, load_box=True):
    games, teams = _list_games(sport, date)
    db.upsert(con, "team", teams, ["sport", "team_id"])
    db.upsert(con, "game", games, ["sport", "game_id"])
    con.commit()

    n_box = 0
    if load_box:
        gmeta = {g["game_id"]: g for g in games}
        ids = [g["game_id"] for g in games if g["status"] == "final"]
        if ids:
            with con.cursor() as cur:
                cur.execute("SELECT game_id FROM game WHERE sport=%s AND game_id=ANY(%s) AND boxscore_loaded",
                            (sport, ids))
                done = {r[0] for r in cur.fetchall()}
            for gid in [i for i in ids if i not in done]:
                try:
                    players, tbls = _fetch_box(sport, gid, gmeta[gid])
                except Exception as e:
                    print(f"   ! box {sport} {gid} failed: {repr(e)[:80]}")
                    continue
                if players:
                    db.upsert(con, "player", players, ["sport", "player_id"])
                for table, rows in tbls.items():
                    if rows:
                        db.upsert(con, table, _norm(rows, COLS[table]), ["game_id", "player_id"])
                        n_box += len(rows)
                with con.cursor() as cur:
                    cur.execute("UPDATE game SET boxscore_loaded=true WHERE sport=%s AND game_id=%s", (sport, gid))
                con.commit()
                time.sleep(0.35 + random.random() * 0.35)
    return len(games), n_box


SPORTS = ["nba", "nfl", "mlb", "nhl"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="all")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD anchor (default 2026-06-03)")
    ap.add_argument("--days", type=int, default=1, help="days back from anchor (inclusive)")
    ap.add_argument("--no-box", action="store_true")
    args = ap.parse_args()

    base = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    sports = SPORTS if args.sport == "all" else [args.sport]
    con = db.connect()
    for sp in sports:
        tot_g = tot_b = 0
        for d in range(args.days):
            day = base - datetime.timedelta(days=d)
            try:
                ng, nb = collect_date(sp, day, con, load_box=not args.no_box)
                tot_g += ng; tot_b += nb
                if ng or nb:
                    print(f"{sp} {day}: {ng} games, {nb} box rows")
                db.log_run(con, "stats", sp, day, ng, nb, "ok")
            except Exception as e:
                con.rollback()
                print(f"{sp} {day}: ERROR {repr(e)[:120]}")
                try:
                    db.log_run(con, "stats", sp, day, 0, 0, "error", repr(e))
                except Exception:
                    con.rollback()
            con.commit()
        print(f"== {sp} TOTAL: {tot_g} games, {tot_b} box rows ==")
    con.close()


if __name__ == "__main__":
    main()
