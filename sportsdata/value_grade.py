"""Grade logged value plays -> realized W/L, ROI, and CLV. Prints the running
track record. This is the auditable proof that the +EV-vs-sharp method works
(or doesn't) before any real money / audience claims.

Outcome: the player's actual stat computed DIRECTLY from the box-score tables
(via stat_map + a player-name index), so EVERY logged play settles regardless of
whether a DFS book also offered it. No box row / DNP => void. ROI per play:
win->offered_mult-1, push/void->0, loss->-1.  CLV: the sharp anchor (sgo_fair,
fallback bovada) de-vigged fair P(side) at last snapshot minus at log time;
>0 means the sharp line moved toward our pick (the real edge signal).
"""
import re, unicodedata, datetime
import db
import stat_map

def norm(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()

def initial_last(n):
    p = norm(n).split()
    return f"{p[0][0]} {p[-1]}" if len(p) >= 2 else None

def am_to_prob(a):
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else (-a) / (-a + 100.0)


def grade(con):
    cur = con.cursor()
    today = datetime.date.today()

    # player name -> player_id index (exact + first-initial-last fallback)
    cur.execute("SELECT sport, player_id, full_name FROM player")
    name2id, init2id = {}, {}
    for sport, pid, nm in cur.fetchall():
        name2id.setdefault((sport, norm(nm)), pid)
        il = initial_last(nm)
        if il:
            init2id.setdefault((sport, il), []).append(pid)

    # lazily-loaded box indexes: box_source -> {(player_id, game_date): row}
    boxes = {}
    def box_idx(box_source):
        if box_source not in boxes:
            c2 = con.cursor(); c2.execute(f"SELECT * FROM {stat_map.TABLE[box_source]}")
            cols = [d[0] for d in c2.description]; idx = {}
            for r in c2.fetchall():
                d = dict(zip(cols, r)); idx.setdefault((d["player_id"], d["game_date"]), d)
            boxes[box_source] = idx
        return boxes[box_source]

    def actual_for(sport, pname, stat, gd):
        box_source, fn = stat_map.lookup(sport, stat)
        if not box_source:
            return None
        pid = name2id.get((sport, norm(pname)))
        if not pid:
            cands = init2id.get((sport, initial_last(pname) or ""), [])
            pid = cands[0] if len(cands) == 1 else None
        if not pid:
            return None
        idx = box_idx(box_source)
        for dd in (0, 1, -1):   # +-1 day for UTC/local date skew
            row = idx.get((pid, gd + datetime.timedelta(days=dd)))
            if row:
                v = fn(row)
                return None if v is None else float(v)
        return None

    # last sharp-anchor line per key for CLV (prefer sgo_fair over bovada). sgo_fair
    # rows carry the OPEN odds in raw, so CLV = close_fairP - open_fairP (robust to our
    # snapshot frequency), falling back to (close - log-time fair) if no open stored.
    import json as _json
    cur.execute("""
      SELECT DISTINCT ON (book, sport, player_name, stat_type, line)
             book, sport, player_name, stat_type, line, over_american, under_american, raw
      FROM odds_book_prop_snapshot WHERE book IN ('sgo_fair','bovada')
      ORDER BY book, sport, player_name, stat_type, line, snapshot_ts DESC
    """)
    closefair, cf_src, pref = {}, {}, {"sgo_fair": 0, "bovada": 1}
    for book, sport, pname, stat, line, oa, ua, raw in cur.fetchall():
        if oa is None or ua is None:
            continue
        k = (sport, norm(pname), stat, float(line))
        if k in cf_src and pref[cf_src[k]] <= pref[book]:
            continue
        qo, qu = am_to_prob(oa), am_to_prob(ua); t = qo + qu
        opo = opu = None
        if raw:
            try:
                rj = raw if isinstance(raw, dict) else _json.loads(raw)
                oo, ou = rj.get("open_over"), rj.get("open_under")
                if oo is not None and ou is not None:
                    a, b = am_to_prob(oo), am_to_prob(ou); s = a + b
                    opo, opu = a / s, b / s
            except Exception:
                pass
        closefair[k] = (qo / t, qu / t, opo, opu); cf_src[k] = book

    cur.execute("""SELECT id, game_date, sport, player_name, stat_type, line, side,
                          offered_mult, fair_prob FROM value_play
                   WHERE graded_ts IS NULL AND game_date < %s
                     AND COALESCE(market_type,'prop')='prop'""", (today,))
    todo = cur.fetchall()
    graded = 0
    for vid, gd, sport, pname, stat, line, side, mult, fair_prob in todo:
        actual = actual_for(sport, pname, stat, gd)
        line = float(line)
        if actual is None:
            result = "void"
        elif actual > line:
            result = "win" if side == "OVER" else "loss"
        elif actual < line:
            result = "win" if side == "UNDER" else "loss"
        else:
            result = "push"
        ck = (sport, norm(pname), stat, line)
        clv = None; cfp = None
        if ck in closefair:
            po, pu, opo, opu = closefair[ck]
            cfp = po if side == "OVER" else pu
            openp = (opo if side == "OVER" else opu)        # SGO's tracked open
            base = openp if openp is not None else float(fair_prob)
            clv = cfp - base
        cur.execute("""UPDATE value_play SET actual=%s, result=%s, close_fair_prob=%s,
                       clv=%s, graded_ts=now() WHERE id=%s""",
                    (actual, result, cfp, clv, vid))
        graded += 1
    con.commit()
    print(f"value_grade: graded {graded} plays")

    # ---- running track record ----
    def payout(mult, result):
        return {"win": float(mult) - 1, "loss": -1.0, "push": 0.0, "void": 0.0}[result]

    cur.execute("""SELECT recommended, ev, offered_mult, result, clv FROM value_play
                   WHERE result IS NOT NULL""")
    rows = cur.fetchall()
    if not rows:
        print("no graded plays yet (need games to finish + grade.py to run)"); return

    def summarize(label, sub):
        if not sub: return
        n = len(sub)
        wins = sum(1 for r in sub if r[3] == "win")
        losses = sum(1 for r in sub if r[3] == "loss")
        voids = sum(1 for r in sub if r[3] in ("void", "push"))
        roi = sum(payout(r[2], r[3]) for r in sub) / max(n - voids, 1)
        clvs = [r[4] for r in sub if r[4] is not None]
        avgclv = sum(clvs) / len(clvs) if clvs else float("nan")
        pos_clv = sum(1 for c in clvs if c > 0) / len(clvs) if clvs else float("nan")
        print(f"  {label:22} n={n:4d} (W{wins}-L{losses}-V{voids})  ROI={roi:+6.1%}  "
              f"avgCLV={avgclv:+.3f}  CLV>0={pos_clv:.0%}")

    print("\n=== VALUE-PLAY TRACK RECORD (settled) ===")
    summarize("ALL matched", rows)
    summarize("RECOMMENDED (+EV)", [r for r in rows if r[0]])
    for lo, hi in [(-0.10, 0.0), (0.0, 0.03), (0.03, 0.06), (0.06, 1.0)]:
        summarize(f"EV [{lo:+.0%},{hi:+.0%})", [r for r in rows if lo <= float(r[1]) < hi])
    print("  (ROI excludes voids/pushes from the denominator; CLV>0 share is the "
          "leading indicator — if it's not >50%, the method has no edge.)")


def _game_closefair(con):
    """(sport,event_ref,market,outcome,line) -> (close_fairP, open_fairP), de-vigged."""
    import json as _json
    cur = con.cursor()
    cur.execute("""
      SELECT DISTINCT ON (sport, event_ref, market, outcome, line)
             sport, event_ref, market, outcome, line, price, raw
      FROM odds_game_snapshot WHERE source='sgo' AND book='sgo_fair' AND price IS NOT NULL
      ORDER BY sport, event_ref, market, outcome, line, snapshot_ts DESC
    """)
    pair = {}   # (sport,eref,market,line) -> {outcome:(close_am, open_am)}
    for sport, eref, mkt, outcome, line, price, raw in cur.fetchall():
        L = float(line) if line is not None else 0.0
        oa = None
        if raw:
            rj = raw if isinstance(raw, dict) else _json.loads(raw)
            oa = rj.get("open_odds")
        pair.setdefault((sport, eref, mkt, L), {})[outcome] = (price, oa)
    out = {}
    for (sport, eref, mkt, L), outs in pair.items():
        if len(outs) < 2:
            continue
        qc = {o: am_to_prob(am) for o, (am, _) in outs.items()}; tc = sum(qc.values())
        opens = {o: op for o, (_, op) in outs.items() if op is not None}
        qo = {o: am_to_prob(am) for o, am in opens.items()}; to = sum(qo.values()) if qo else 0
        for o in outs:
            cp = qc[o] / tc
            op = (qo[o] / to) if (len(opens) == len(outs) and to) else None
            out[(sport, eref, mkt, o, L)] = (cp, op)
    return out


def grade_games(con):
    """Settle h2h/spread/total value plays from final scores; compute game CLV."""
    cur = con.cursor(); today = datetime.date.today()
    cur.execute("SELECT sport, abbrev, team_id FROM team WHERE abbrev IS NOT NULL")
    abbr = {(s, a): str(t) for s, a, t in cur.fetchall()}
    cur.execute("""SELECT sport, game_date, home_team_id, away_team_id, home_score, away_score
                   FROM game WHERE status='final' AND home_score IS NOT NULL""")
    gidx = {(s, gd, str(h), str(a)): (hs, asc) for s, gd, h, a, hs, asc in cur.fetchall()}
    closefair = _game_closefair(con)

    cur.execute("""SELECT id, game_date, sport, market_type, side, line, offered_mult, fair_prob,
                          event_ref, home_team, away_team
                   FROM value_play
                   WHERE graded_ts IS NULL AND market_type IN ('h2h','spread','total')
                     AND game_date < %s""", (today,))
    todo = cur.fetchall(); graded = unmatched = 0
    for vid, gd, sport, mkt, side, line, mult, fair_prob, eref, ht, at in todo:
        hid, aid = abbr.get((sport, ht)), abbr.get((sport, at))
        final = None
        if hid and aid:
            for dd in (0, 1, -1):
                final = gidx.get((sport, gd + datetime.timedelta(days=dd), hid, aid))
                if final:
                    break
        if not final:
            unmatched += 1
            result, actual = "void", None
        else:
            hs, asc = final; margin = hs - asc; L = float(line or 0)
            if mkt == "h2h":
                actual = margin
                win = (margin > 0) if side == "HOME" else (margin < 0)
                result = "win" if win else "loss"
            elif mkt == "total":
                actual = hs + asc; d = actual - L
                result = "push" if d == 0 else ("win" if (d > 0) == (side == "OVER") else "loss")
            else:  # spread
                actual = margin
                cover = (margin + L) if side == "HOME" else (-margin + L)
                result = "push" if cover == 0 else ("win" if cover > 0 else "loss")
        clv = cfp = None
        ck = (sport, eref, mkt, side.lower(), float(line or 0))
        if ck in closefair:
            cfp, op = closefair[ck]
            base = op if op is not None else (float(fair_prob) if fair_prob is not None else None)
            clv = (cfp - base) if base is not None else None
        cur.execute("""UPDATE value_play SET actual=%s, result=%s, close_fair_prob=%s,
                       clv=%s, graded_ts=now() WHERE id=%s""", (actual, result, cfp, clv, vid))
        graded += 1
    con.commit()
    print(f"value_grade: graded {graded} game plays ({unmatched} unmatched -> void)")


def refresh_category_stats(con):
    """Per (sport, market_type, bet_book) realized ROI/CLV -> value_category_stats (ranker tiers)."""
    cur = con.cursor()
    cur.execute("""
      SELECT sport, market_type, bet_book, COUNT(*) n,
             SUM(CASE result WHEN 'win' THEN offered_mult-1 WHEN 'loss' THEN -1 ELSE 0 END)
               / NULLIF(SUM((result IN ('win','loss'))::int),0) roi,
             AVG(clv) avg_clv,
             AVG((clv>0)::int)::numeric clv_pos
      FROM value_play WHERE result IS NOT NULL AND recommended
      GROUP BY sport, market_type, bet_book""")
    rows = [dict(sport=r[0], market_type=r[1], bet_book=r[2], n_graded=r[3],
                 roi=r[4], avg_clv=r[5], clv_pos_share=r[6]) for r in cur.fetchall()]
    if rows:
        db.upsert(con, "value_category_stats", rows, ["sport", "market_type", "bet_book"])
        con.commit()
    print(f"value_grade: refreshed {len(rows)} category-stat rows")


if __name__ == "__main__":
    con = db.connect()
    grade(con)
    grade_games(con)
    refresh_category_stats(con)
    con.close()
