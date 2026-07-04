"""Parse ArnavSaraogi MLB odds JSON and load closing moneylines into Postgres.

Output table: historical_mlb_odds(game_date, home_team, away_team,
home_score, away_score, ml_home_close, ml_away_close, ml_home_open, ml_away_open,
ml_home_close_pinnacle, n_books).

We average ML across all sportsbooks for stability ("closing market consensus").
"""
import os
import json
import statistics
import psycopg2
from psycopg2.extras import execute_values

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
JSON_PATH = r"c:\Users\jackp\Desktop\new_game\odds_data\mlb_odds_dataset.json"


def american_to_prob(ml):
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


def prob_to_american(p):
    if p is None or p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        return int(round(-100.0 * p / (1 - p)))
    return int(round(100.0 * (1 - p) / p))


def consensus_ml(books_lines, side):
    """Average implied probability across books, return as American."""
    probs = []
    for b in books_lines:
        v = b.get("currentLine", {}).get(side)
        p = american_to_prob(v)
        if p is not None and 0.01 < p < 0.99:
            probs.append(p)
    if not probs:
        return None, None
    avg = statistics.mean(probs)
    return prob_to_american(avg), len(probs)


def main():
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    skipped_no_final = 0
    for date, games in data.items():
        for g in games:
            gv = g.get("gameView", {})
            status = gv.get("gameStatusText", "")
            if not status.startswith("Final"):
                skipped_no_final += 1
                continue
            home = gv.get("homeTeam", {}).get("fullName")
            away = gv.get("awayTeam", {}).get("fullName")
            if not home or not away:
                continue
            home_score = gv.get("homeTeamScore")
            away_score = gv.get("awayTeamScore")
            ml_books = g.get("odds", {}).get("moneyline", []) or []

            close_home, nh = consensus_ml(ml_books, "homeOdds")
            close_away, na = consensus_ml(ml_books, "awayOdds")

            # opening
            open_probs_h, open_probs_a = [], []
            for b in ml_books:
                ph = american_to_prob(b.get("openingLine", {}).get("homeOdds"))
                pa = american_to_prob(b.get("openingLine", {}).get("awayOdds"))
                if ph and 0.01 < ph < 0.99:
                    open_probs_h.append(ph)
                if pa and 0.01 < pa < 0.99:
                    open_probs_a.append(pa)
            open_home = prob_to_american(statistics.mean(open_probs_h)) if open_probs_h else None
            open_away = prob_to_american(statistics.mean(open_probs_a)) if open_probs_a else None

            rows.append((date, home, away, home_score, away_score,
                         close_home, close_away, open_home, open_away,
                         nh or 0))

    print(f"Parsed: {len(rows)} games, skipped non-final: {skipped_no_final}")

    pg = psycopg2.connect(**PG)
    with pg.cursor() as c:
        c.execute("DROP TABLE IF EXISTS historical_mlb_odds")
        c.execute("""
            CREATE TABLE historical_mlb_odds (
                game_date    date NOT NULL,
                home_team    varchar(50) NOT NULL,
                away_team    varchar(50) NOT NULL,
                home_score   integer,
                away_score   integer,
                ml_home_close integer,
                ml_away_close integer,
                ml_home_open  integer,
                ml_away_open  integer,
                n_books_close integer,
                PRIMARY KEY (game_date, home_team)
            )
        """)
        execute_values(
            c,
            "INSERT INTO historical_mlb_odds VALUES %s "
            "ON CONFLICT (game_date, home_team) DO NOTHING",
            rows,
        )
        c.execute("CREATE INDEX idx_hmo_date ON historical_mlb_odds (game_date)")
        c.execute("CREATE INDEX idx_hmo_home ON historical_mlb_odds (home_team)")
        c.execute("SELECT COUNT(*), MIN(game_date), MAX(game_date) FROM historical_mlb_odds")
        print(c.fetchone())
        c.execute(
            "SELECT EXTRACT(YEAR FROM game_date)::int AS yr, COUNT(*) "
            "FROM historical_mlb_odds GROUP BY 1 ORDER BY 1"
        )
        for r in c.fetchall():
            print(f"  {r[0]}: {r[1]} games")
    pg.commit()
    pg.close()


if __name__ == "__main__":
    main()
