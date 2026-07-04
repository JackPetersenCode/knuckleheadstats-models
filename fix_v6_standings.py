"""Patch v6: re-apply standings join using snapshot from game_date - 1 (no leak).

Also clears the previous (leaky) values first.
"""
import os
import psycopg2

PG = dict(host="localhost", user="postgres", dbname="hoop_scoop", password=os.environ.get("PGPASSWORD", ""))

SQL = """
UPDATE mlb_features_v6
SET h_gb = NULL, a_gb = NULL, h_api_streak = NULL, a_api_streak = NULL;

UPDATE mlb_features_v6 f
SET h_gb = sh.games_back,
    h_api_streak = CASE
      WHEN sh.streak LIKE 'W%' THEN substr(sh.streak,2)::int
      WHEN sh.streak LIKE 'L%' THEN -substr(sh.streak,2)::int
      ELSE NULL END,
    a_gb = sa.games_back,
    a_api_streak = CASE
      WHEN sa.streak LIKE 'W%' THEN substr(sa.streak,2)::int
      WHEN sa.streak LIKE 'L%' THEN -substr(sa.streak,2)::int
      ELSE NULL END
FROM mlb_team_standings sh
JOIN mlb_team_standings sa
  ON sa.snapshot_date = sh.snapshot_date
WHERE sh.snapshot_date = f.game_date - INTERVAL '1 day'
  AND sh.team_id = f.home_team_id
  AND sa.team_id = f.away_team_id;
"""

pg = psycopg2.connect(**PG)
with pg.cursor() as c:
    c.execute(SQL)
    c.execute("SELECT COUNT(*), COUNT(h_api_streak) FROM mlb_features_v6")
    print(c.fetchone())
pg.commit()
pg.close()
