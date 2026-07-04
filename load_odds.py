"""Load historical NBA closing moneylines from kyleskom's OddsData.sqlite into Postgres.

Creates table public.historical_nba_odds(season, game_date, home_team, away_team,
ml_home, ml_away, ou, spread).
"""
import os
import sqlite3
import psycopg2
from psycopg2.extras import execute_values

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))
SQLITE_PATH = r"c:\Users\jackp\Desktop\new_game\odds_data\OddsData.sqlite"

SEASON_TABLES = {
    "2016-17": "odds_2016-17_new",
    "2017-18": "odds_2017-18_new",
    "2018-19": "odds_2018-19_new",
    "2019-20": "odds_2019-20_new",
    "2020-21": "odds_2020-21_new",
    "2021-22": "odds_2021-22_new",
    "2022-23": "odds_2022-23_new",
    "2023-24": "2023-24",
    "2024-25": "2024-25",
}

def main():
    def clean_num(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            s = str(v).strip().upper()
            if s in {"PK", "PICK", ""}:
                return 0.0
            return None

    def clean_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    sc = sqlite3.connect(SQLITE_PATH)
    rows = []
    for season, tbl in SEASON_TABLES.items():
        cur = sc.execute(
            f'SELECT Date, Home, Away, ML_Home, ML_Away, OU, Spread FROM "{tbl}"'
        )
        for date, home, away, mlh, mla, ou, sp in cur.fetchall():
            rows.append((
                season, date, home, away,
                clean_int(mlh), clean_int(mla),
                clean_num(ou), clean_num(sp),
            ))
        print(f"  {season} ({tbl}): {sum(1 for x in rows if x[0]==season)} rows")
    sc.close()

    pg = psycopg2.connect(**PG)
    pg.autocommit = False
    with pg.cursor() as c:
        c.execute("DROP TABLE IF EXISTS historical_nba_odds")
        c.execute("""
            CREATE TABLE historical_nba_odds (
                season       varchar(10) NOT NULL,
                game_date    date        NOT NULL,
                home_team    varchar(50) NOT NULL,
                away_team    varchar(50) NOT NULL,
                ml_home      integer,
                ml_away      integer,
                ou           numeric,
                spread       numeric,
                PRIMARY KEY (game_date, home_team)
            )
        """)
        execute_values(
            c,
            "INSERT INTO historical_nba_odds "
            "(season, game_date, home_team, away_team, ml_home, ml_away, ou, spread) "
            "VALUES %s ON CONFLICT (game_date, home_team) DO NOTHING",
            rows,
        )
        c.execute("CREATE INDEX idx_hno_season ON historical_nba_odds (season)")
        c.execute("SELECT COUNT(*) FROM historical_nba_odds")
        total = c.fetchone()[0]
    pg.commit()
    pg.close()
    print(f"\nInserted total: {total} rows into historical_nba_odds")

if __name__ == "__main__":
    main()
