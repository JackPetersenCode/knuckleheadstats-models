"""Grade collected props against actual box-score outcomes + compute CLV.

Unit of grading = (sport, source, source_player_id, stat_type, line_type, game_date).
Within that unit, snapshots over time give the line movement:
   open_line  = line at first snapshot   close_line = line at last snapshot
   clv        = close_line - open_line   (raw movement; directional CLV in analysis)
Result (over/under/push) is judged vs the CLOSE line (what you'd bet at game time).

Only props whose game has been played (a matching box row exists within +-1 day of
the prop's start date) are graded; the rest are skipped (pending).
"""
import datetime
import db
import stat_map
import xwalk as X


def _pdate(start_ts, fallback):
    if start_ts:
        try:
            return datetime.datetime.fromisoformat(str(start_ts)).date()
        except Exception:
            pass
    return fallback


def grade(con):
    cur = con.cursor()
    # 1. xwalk -> player_id
    cur.execute("SELECT sport, source, source_player_id, player_id FROM player_xwalk WHERE player_id IS NOT NULL")
    xw = {(s, src, sp): pid for s, src, sp, pid in cur.fetchall()}

    # 2. collapse snapshots -> one row per grading unit (open/close lines)
    cur.execute("""
      SELECT sport, source, source_player_id, player_name, stat_type, line_type,
             COALESCE(start_ts::date, snapshot_ts::date) AS gdate,
             (array_agg(line ORDER BY snapshot_ts))[1]                       AS open_line,
             (array_agg(line ORDER BY snapshot_ts DESC))[1]                  AS close_line,
             (array_agg(over_mult ORDER BY snapshot_ts DESC))[1]            AS over_mult,
             (array_agg(under_mult ORDER BY snapshot_ts DESC))[1]          AS under_mult,
             max(snapshot_ts) AS close_ts,
             max(start_ts)    AS start_ts
      FROM odds_prop_snapshot
      WHERE source_player_id IS NOT NULL AND line IS NOT NULL
      GROUP BY sport, source, source_player_id, player_name, stat_type, line_type, gdate
    """)
    units = cur.fetchall()

    # 3. cache box rows lazily per (sport, table)
    boxcache = {}

    def box_row(sport, src_tbl, player_id, pdate):
        key = (sport, src_tbl)
        if key not in boxcache:
            table = stat_map.TABLE[src_tbl]
            cur.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            idx = {}
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                idx.setdefault((d["player_id"], d["game_date"]), d)
            boxcache[key] = idx
        idx = boxcache[key]
        for dd in (0, 1, -1):  # +-1 day window for UTC/local date skew
            r = idx.get((player_id, pdate + datetime.timedelta(days=dd)))
            if r:
                return r
        return None

    graded, skipped_unmatched, skipped_ungrade, skipped_pending = [], 0, 0, 0
    for (sport, source, spid, pname, stat_type, line_type, gdate,
         open_line, close_line, over_mult, under_mult, close_ts, start_ts) in units:
        pid = xw.get((sport, source, spid))
        if not pid:
            skipped_unmatched += 1; continue
        src_tbl, fn = stat_map.lookup(sport, stat_type)
        if not src_tbl:
            skipped_ungrade += 1; continue
        pdate = _pdate(start_ts, gdate)
        row = box_row(sport, src_tbl, pid, pdate)
        if row is None:
            skipped_pending += 1; continue
        actual = fn(row)
        if actual is None:
            skipped_pending += 1; continue
        actual = float(actual)
        cl = float(close_line)
        result = "over" if actual > cl else ("under" if actual < cl else "push")
        clv = (float(close_line) - float(open_line)) if open_line is not None else None
        graded.append(dict(sport=sport, source=source, source_player_id=spid, player_id=pid,
            player_name=pname, stat_type=stat_type, box_stat=src_tbl, line=close_line,
            line_type=line_type, over_mult=over_mult, under_mult=under_mult, game_date=pdate,
            actual=actual, result=result, open_line=open_line, close_line=close_line,
            clv=clv, snapshot_ts=close_ts))

    if graded:
        db.upsert(con, "prop_graded", graded,
                  ["sport", "source", "source_player_id", "stat_type", "line_type", "game_date"])
        con.commit()
    print(f"graded: {len(graded)}  |  unmatched player: {skipped_unmatched}  "
          f"ungradeable stat: {skipped_ungrade}  pending(no box yet): {skipped_pending}")
    return len(graded)


if __name__ == "__main__":
    con = db.connect()
    grade(con)
    con.close()
