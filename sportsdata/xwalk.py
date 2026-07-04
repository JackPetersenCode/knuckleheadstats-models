"""Player crosswalk: map DFS prop players (PrizePicks/Underdog) -> our player_id.

Steps:
  1. Enrich NHL player names from current rosters (NHL boxscore gives only
     'J. Staal'; rosters give full first/last) so names are clean + matchable.
  2. For each distinct DFS (sport, source, source_player_id, name):
       - skip combo props (name contains ' + ')
       - exact normalized match -> player_id
       - else first-initial+lastname fallback (unique only)
       - else unmatched (logged)
  Writes player_xwalk. Re-runnable.
"""
import unicodedata, re
import db
from http_util import get_json


def norm(n):
    if not n:
        return ""
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def initial_last(n):
    p = norm(n).split()
    return f"{p[0][0]} {p[-1]}" if len(p) >= 2 else None


def enrich_nhl_names(con):
    cur = con.cursor()
    cur.execute("SELECT abbrev FROM team WHERE sport='nhl' AND abbrev IS NOT NULL")
    abbrevs = [r[0] for r in cur.fetchall()]
    updates = []
    for ab in abbrevs:
        try:
            r = get_json(f"https://api-web.nhle.com/v1/roster/{ab}/current")
        except Exception:
            continue
        for grp in ("forwards", "defensemen", "goalies"):
            for p in r.get(grp, []):
                fn = (p.get("firstName") or {}).get("default", "")
                ln = (p.get("lastName") or {}).get("default", "")
                full = f"{fn} {ln}".strip()
                if full and p.get("id"):
                    updates.append((full, str(p["id"])))
    for full, pid in updates:
        cur.execute("UPDATE player SET full_name=%s WHERE sport='nhl' AND player_id=%s AND full_name LIKE '%%. %%'",
                    (full, pid))
    con.commit()
    print(f"  NHL name enrichment: {len(updates)} roster names applied")


def build(con):
    cur = con.cursor()
    # player lookup maps per sport
    exact, byinit = {}, {}
    cur.execute("SELECT sport, player_id, full_name FROM player")
    for sport, pid, nm in cur.fetchall():
        exact.setdefault(sport, {}).setdefault(norm(nm), []).append(pid)
        il = initial_last(nm)
        if il:
            byinit.setdefault(sport, {}).setdefault(il, []).append(pid)

    cur.execute("""SELECT DISTINCT sport, source, source_player_id, player_name
                   FROM odds_prop_snapshot WHERE source_player_id IS NOT NULL""")
    dfs = cur.fetchall()
    rows, stats = [], {}
    for sport, source, spid, name in dfs:
        method, pid, mname = "unmatched", None, None
        if name and " + " in name:
            method = "combo"
        else:
            cands = exact.get(sport, {}).get(norm(name), [])
            if len(cands) == 1:
                method, pid = "exact", cands[0]
            elif len(cands) > 1:
                method, pid = "exact", cands[0]  # ambiguous -> take first; flagged by count elsewhere
            else:
                il = initial_last(name or "")
                ic = byinit.get(sport, {}).get(il, []) if il else []
                if len(ic) == 1:
                    method, pid = "initial_last", ic[0]
            if pid:
                mname = name
        rows.append(dict(sport=sport, source=source, source_player_id=spid, dfs_name=name,
                         player_id=pid, matched_name=mname, method=method))
        stats.setdefault(sport, {}).setdefault(method, 0)
        stats[sport][method] += 1

    db.upsert(con, "player_xwalk", rows, ["sport", "source", "source_player_id"])
    con.commit()

    print("  crosswalk coverage by sport:")
    for sport in sorted(stats):
        s = stats[sport]
        tot = sum(s.values())
        gradeable = s.get("exact", 0) + s.get("initial_last", 0)
        non_combo = tot - s.get("combo", 0)
        print(f"    {sport}: {gradeable}/{non_combo} single-player matched "
              f"({gradeable/max(non_combo,1):.0%}) | {dict(s)}")
    return stats


if __name__ == "__main__":
    con = db.connect()
    print("enriching NHL names...")
    enrich_nhl_names(con)
    print("building crosswalk...")
    build(con)
    con.close()
