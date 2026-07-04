# sportsedge — multi-sport stats + odds data platform

A clean, self-contained data platform for NBA, NFL, MLB, NHL. Collects player &
team stats and betting odds (moneyline / spread / total / **player props**) into a
new Postgres database (`sportsedge`) — **separate from the legacy `hoop_scoop` DB**.

Built for player-prop research: every prop-relevant counting stat is captured, and
odds are stored as append-only snapshots so open→close line movement (Closing Line
Value) is measurable.

## Data sources (all free, no paid keys)

| Sport | Stats source | Why |
|---|---|---|
| MLB | `statsapi.mlb.com` (official) | richest free feed (batting/pitching/fielding) |
| NHL | `api-web.nhle.com` (official) | official, detailed skater/goalie stats |
| NBA | ESPN (`site.api.espn.com`) | official `stats.nba.com` blocks datacenter IPs |
| NFL | ESPN | no open official NFL API exists |

| Odds | Source | Markets |
|---|---|---|
| Player props | PrizePicks + Underdog public endpoints | over/under lines, demon/goblin, multipliers |
| Game lines | ESPN core odds API | moneyline, spread, total (multi-book) |

## Schema (Postgres `sportsedge`)

**Dimensions** (shared, `sport` column): `team`, `player`, `game`.
**Per-sport box scores:** `nba_player_box`, `nfl_player_box`, `mlb_batting_box`,
`mlb_pitching_box`, `nhl_skater_box`, `nhl_goalie_box`.
**Odds (append-only snapshots):** `odds_prop_snapshot`, `odds_game_snapshot`.
**Ops:** `collect_log` (every run logged).

## Files

| File | Purpose |
|---|---|
| `db.py` | connection + schema + upsert helpers |
| `schema.sql` | full DDL (idempotent) |
| `http_util.py` | resilient stdlib JSON HTTP (retries/backoff) |
| `espn.py`, `mlb_src.py`, `nhl_src.py` | per-sport stat sources |
| `collect.py` | stats orchestrator (scoreboard → games → boxscores) |
| `backfill.py` | historical backfill (idempotent, resumable) |
| `odds_props.py` | DFS player-prop collector |
| `odds_game.py` | ESPN game-line collector |
| `collect_odds.py` | unified odds driver |

## Usage

```powershell
python db.py                               # create/refresh schema
python collect.py --sport all --days 3     # update last 3 days, all sports
python backfill.py --sport mlb             # backfill a sport's season windows
python collect_odds.py                     # one odds snapshot (props + game lines)
```

## Automation (Windows Task Scheduler — already registered)

- **SportsEdge_Stats** — daily 6:00 AM → `run_stats.bat` (refresh last 3 days)
- **SportsEdge_Odds** — every 2h, 9 AM–midnight → `run_odds.bat` (CLV snapshots)

Logs land in `logs/`. Each run also writes a row to `collect_log`.

## Notes / conventions

- **Game dates:** ESPN dates games in UTC (a night game may show as the next day);
  MLB/NHL use official local date. Align odds↔games by team + nearest date.
- **MLB/NHL game_id** = league id (not ESPN id); game-odds rows from ESPN carry
  `event_ref` and are resolved to `game_id` for NBA/NFL directly, MLB/NHL later.
- Odds tables are **append-only** — never updated; open/close derived from snapshots.
- Re-runs are safe: stats upsert on PK; already-loaded boxscores are skipped.

## Next (analysis layer, not yet built)
- Player-name crosswalk (DFS names ↔ `player` ids) to grade props from box scores.
- Prop grading + CLV computation.
- Projection models off the box-score history.
