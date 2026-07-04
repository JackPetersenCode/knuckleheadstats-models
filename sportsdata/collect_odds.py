"""Unified daily odds driver — player props (DFS) + game lines (ESPN).
Run several times/day (Task Scheduler) to capture open->close movement for CLV.
"""
import db
import odds_props
import odds_props_sleeper
import odds_bovada
import odds_game
import odds_theoddsapi


def main():
    con = db.connect()
    print("== player props (PrizePicks + Underdog) ==")
    np = odds_props.collect_props(con)
    db.log_run(con, "odds_props", "all", None, 0, np, "ok")
    print("== player props (Sleeper, explicit multipliers) ==")
    ns = odds_props_sleeper.collect_sleeper(con)
    db.log_run(con, "odds_props_sleeper", "all", None, 0, ns, "ok")
    print("== sportsbook props (Bovada — free, frequent sharp ref) ==")
    nb = odds_bovada.collect_bovada(con)
    db.log_run(con, "odds_bovada", "all", None, 0, nb, "ok")
    print("== SportsGameOdds (consensus fair odds + DK/FD/MGM/Caesars/ESPNBET; throttled ~1x/day) ==")
    try:
        import odds_sgo
        nsg = odds_sgo.collect_sgo(con)   # once/day, evening window (for close-line CLV)
        db.log_run(con, "odds_sgo", "all", None, 0, nsg, "ok")
    except Exception as e:
        print(f"  sgo: ERR {repr(e)[:80]}")
    print("== game lines (ESPN) ==")
    ng = odds_game.collect_game_odds(con)
    db.log_run(con, "odds_game", "all", None, 0, ng, "ok")
    print("== game lines (The Odds API, multi-book; throttled, key-gated) ==")
    na = odds_theoddsapi.collect(con)
    db.log_run(con, "odds_game_api", "all", None, 0, na, "ok")
    con.commit()
    print(f"DONE: {np} PP/UD + {ns} Sleeper props, {nb} Bovada book-props, "
          f"{ng} ESPN game-odds rows, {na} oddsapi rows")
    # log +EV-vs-sharp value plays this cycle (catches transient edges between snapshots)
    print("== value plays (logged for track record) ==")
    try:
        import value_log
        value_log.log_plays(con)
    except Exception as e:
        print(f"  value_log: ERR {repr(e)[:80]}")
    con.commit()
    print("== rank today's best bets (all sports, all bet types) ==")
    try:
        import ranker
        ranker.rank_today(con)
    except Exception as e:
        print(f"  ranker: ERR {repr(e)[:80]}")
    con.commit()
    print("== daily value post (best prices + +EV -> content/) ==")
    try:
        import daily_post
        daily_post.build(con)
    except Exception as e:
        print(f"  daily_post: ERR {repr(e)[:80]}")
    con.close()


if __name__ == "__main__":
    main()
