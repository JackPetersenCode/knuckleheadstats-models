"""Database connection + helpers for the sportsedge project.

New, self-contained DB (`sportsedge`) — does NOT touch the legacy hoop_scoop DB.
"""
import os
from pathlib import Path
import psycopg2
import psycopg2.extras

DB = dict(
    host=os.environ.get("SPORTSEDGE_PGHOST", "localhost"),
    user=os.environ.get("SPORTSEDGE_PGUSER", "postgres"),
    dbname=os.environ.get("SPORTSEDGE_PGDB", "sportsedge"),
    password=os.environ.get("SPORTSEDGE_PGPASS", os.environ.get("PGPASSWORD", "")),
)
HERE = Path(__file__).resolve().parent


def connect():
    return psycopg2.connect(**DB)


def init_schema():
    sql = (HERE / "schema.sql").read_text()
    con = connect()
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute(sql)
    con.close()
    print("schema applied to", DB["dbname"])


def init_schema_v3(con=None):
    """Apply schema_v3.sql (multi-bet-type value engine; additive + idempotent)."""
    own = con is None
    if own:
        con = connect()
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute((HERE / "schema_v3.sql").read_text())
    if own:
        con.close()
    print("schema_v3 applied to", DB["dbname"])


def upsert(con, table, rows, conflict_cols, update=True):
    """Bulk upsert a list of dicts. All dicts must share the same keys.
    `conflict_cols` is a list of PK columns for ON CONFLICT."""
    if not rows:
        return 0
    # dedupe within the batch on the conflict key (keep last) — Postgres ON CONFLICT
    # cannot update the same target row twice in one statement.
    seen = {}
    for r in rows:
        seen[tuple(r[c] for c in conflict_cols)] = r
    rows = list(seen.values())
    cols = list(rows[0].keys())
    collist = ",".join(cols)
    pholder = "(" + ",".join(["%s"] * len(cols)) + ")"
    if update:
        setcols = [c for c in cols if c not in conflict_cols]
        action = "DO UPDATE SET " + ",".join(f"{c}=EXCLUDED.{c}" for c in setcols) if setcols else "DO NOTHING"
    else:
        action = "DO NOTHING"
    sql = (f"INSERT INTO {table} ({collist}) VALUES %s "
           f"ON CONFLICT ({','.join(conflict_cols)}) {action}")
    vals = [tuple(r[c] for c in cols) for r in rows]
    with con.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, vals, template=pholder, page_size=500)
    return len(rows)


def log_run(con, job, sport, target_date, n_games, n_rows, status, message=""):
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO collect_log (job,sport,target_date,n_games,n_rows,status,message) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (job, sport, target_date, n_games, n_rows, status, message[:500]),
        )


if __name__ == "__main__":
    init_schema()
    con = connect()
    with con.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        print("tables:", [r[0] for r in cur.fetchall()])
    con.close()
