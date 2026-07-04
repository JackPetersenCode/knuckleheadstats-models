"""MLB per-game context — weather, umpires, probable pitchers, attendance, day/night —
plus batter/pitcher handedness backfilled into the player dimension.

Source: statsapi live feed /api/v1.1/game/{gamePk}/feed/live (no key).
One row per game in mlb_game_context; player.bat_side / throw_hand updated in place.
"""
from http_util import get_json, to_int
import db

FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"


def _parse_wind(w):
    """'8 mph, Out To CF' -> (8, 'Out To CF'); '0 mph, None' -> (0, None)."""
    if not w:
        return None, None
    mph, _, rest = w.partition(",")
    speed = to_int(mph.replace("mph", "").strip())
    direction = rest.strip() or None
    if direction and direction.lower() == "none":
        direction = None
    return speed, direction


def fetch_context(game_pk):
    """Return (context_row dict, players[] handedness dicts) for one game."""
    d = get_json(FEED.format(pk=game_pk))
    gd = d.get("gameData", {})
    ld = d.get("liveData", {})

    weather = gd.get("weather", {})
    wind_mph, wind_dir = _parse_wind(weather.get("wind"))
    officials = {o.get("officialType"): o.get("official", {}).get("fullName")
                 for o in ld.get("boxscore", {}).get("officials", [])}
    pp = gd.get("probablePitchers", {})

    attendance = None
    for item in ld.get("boxscore", {}).get("info", []):
        if item.get("label") == "Att":
            attendance = to_int((item.get("value") or "").replace(",", "").rstrip("."))

    ctx = dict(
        game_id=str(game_pk),
        game_date=gd.get("datetime", {}).get("officialDate"),
        venue=(gd.get("venue") or {}).get("name"),
        day_night=gd.get("datetime", {}).get("dayNight"),
        attendance=attendance,
        weather_cond=weather.get("condition"),
        temp_f=to_int(weather.get("temp")),
        wind_mph=wind_mph,
        wind_dir=wind_dir,
        ump_home=officials.get("Home Plate"),
        ump_1b=officials.get("First Base"),
        ump_2b=officials.get("Second Base"),
        ump_3b=officials.get("Third Base"),
        home_prob_pitcher_id=str(pp.get("home", {}).get("id")) if pp.get("home") else None,
        away_prob_pitcher_id=str(pp.get("away", {}).get("id")) if pp.get("away") else None,
        home_prob_pitcher=pp.get("home", {}).get("fullName"),
        away_prob_pitcher=pp.get("away", {}).get("fullName"),
    )

    players = []
    for p in gd.get("players", {}).values():
        players.append(dict(
            sport="mlb", player_id=str(p.get("id")), source="mlb",
            full_name=p.get("fullName"),
            position=(p.get("primaryPosition") or {}).get("abbreviation"),
            bat_side=(p.get("batSide") or {}).get("code"),
            throw_hand=(p.get("pitchHand") or {}).get("code"),
        ))
    return ctx, players


def collect_game(game_pk, con):
    ctx, players = fetch_context(game_pk)
    db.upsert(con, "mlb_game_context", [ctx], ["game_id"])
    if players:
        # upsert handedness without clobbering current_team_id (only set provided cols)
        db.upsert(con, "player", players, ["sport", "player_id"])
    con.commit()
    return 1


def collect_pending(con, limit=None):
    """Fill context for finalized MLB games missing a context row."""
    with con.cursor() as cur:
        cur.execute("""
            SELECT g.game_id FROM game g
            LEFT JOIN mlb_game_context c ON c.game_id = g.game_id
            WHERE g.sport='mlb' AND g.status='final' AND c.game_id IS NULL
            ORDER BY g.game_date DESC """ + (f"LIMIT {int(limit)}" if limit else ""))
        ids = [r[0] for r in cur.fetchall()]
    n = 0
    for gid in ids:
        try:
            collect_game(gid, con)
            n += 1
        except Exception as e:
            con.rollback()
            print(f"  ctx {gid} ERR {repr(e)[:80]}")
    return n


if __name__ == "__main__":
    import sys
    con = db.connect()
    if len(sys.argv) > 1 and sys.argv[1].isdigit() and len(sys.argv[1]) > 4:
        print("context:", collect_game(sys.argv[1], con))
    else:
        lim = int(sys.argv[1]) if len(sys.argv) > 1 else 20
        print(f"filled {collect_pending(con, lim)} game contexts")
    con.close()
