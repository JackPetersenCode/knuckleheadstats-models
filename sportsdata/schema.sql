-- sportsedge schema — stats layer (v1)
-- Shared dimensions (team/player/game) + per-sport player box-score facts.
-- Designed for player-prop modeling: every prop-relevant counting stat is captured.

-- ========================= shared dimensions =========================
CREATE TABLE IF NOT EXISTS team (
    sport        text NOT NULL,
    team_id      text NOT NULL,          -- canonical = source id
    source       text NOT NULL,          -- 'espn' | 'mlb' | 'nhl'
    name         text,
    abbrev       text,
    location     text,
    display_name text,
    conference   text,
    division     text,
    updated_at   timestamptz DEFAULT now(),
    PRIMARY KEY (sport, team_id)
);

CREATE TABLE IF NOT EXISTS player (
    sport           text NOT NULL,
    player_id       text NOT NULL,
    source          text NOT NULL,
    full_name       text,
    position        text,
    current_team_id text,
    updated_at      timestamptz DEFAULT now(),
    PRIMARY KEY (sport, player_id)
);
CREATE INDEX IF NOT EXISTS idx_player_name ON player (sport, lower(full_name));

CREATE TABLE IF NOT EXISTS game (
    sport           text NOT NULL,
    game_id         text NOT NULL,        -- source game id (espn event / mlb gamePk / nhl id)
    source          text NOT NULL,
    season          int,
    season_type     text,                 -- 'regular' | 'postseason' | 'preseason'
    game_date       date,
    start_ts        timestamptz,
    home_team_id    text,
    away_team_id    text,
    home_score      int,
    away_score      int,
    status          text,                 -- 'scheduled' | 'in' | 'final'
    venue           text,
    boxscore_loaded boolean DEFAULT false,
    updated_at      timestamptz DEFAULT now(),
    PRIMARY KEY (sport, game_id)
);
CREATE INDEX IF NOT EXISTS idx_game_date   ON game (sport, game_date);
CREATE INDEX IF NOT EXISTS idx_game_status ON game (sport, status);
CREATE INDEX IF NOT EXISTS idx_game_pending ON game (sport, status) WHERE NOT boxscore_loaded;

-- ========================= NBA =========================
CREATE TABLE IF NOT EXISTS nba_player_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean, starter boolean,
    min numeric, pts int, fgm int, fga int, fg3m int, fg3a int, ftm int, fta int,
    oreb int, dreb int, reb int, ast int, stl int, blk int, tov int, pf int, plus_minus int,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_nba_box_player ON nba_player_box (player_id, game_date);

-- ========================= NFL =========================
CREATE TABLE IF NOT EXISTS nfl_player_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean,
    pass_cmp int, pass_att int, pass_yds int, pass_td int, pass_int int, pass_sacked int, qbr numeric, pass_rtg numeric,
    rush_att int, rush_yds int, rush_td int, rush_long int,
    rec int, rec_tgts int, rec_yds int, rec_td int, rec_long int,
    fum int, fum_lost int,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_box_player ON nfl_player_box (player_id, game_date);

-- ========================= MLB =========================
CREATE TABLE IF NOT EXISTS mlb_batting_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean, batting_order int,
    ab int, r int, h int, doubles int, triples int, hr int, rbi int, bb int, k int,
    sb int, cs int, hbp int, tb int, lob int,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_mlb_bat_player ON mlb_batting_box (player_id, game_date);

CREATE TABLE IF NOT EXISTS mlb_pitching_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean, started boolean,
    ip numeric, h int, r int, er int, bb int, k int, hr int, bf int, pitches int, strikes int,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_mlb_pit_player ON mlb_pitching_box (player_id, game_date);

-- ========================= NHL =========================
CREATE TABLE IF NOT EXISTS nhl_skater_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean, position text,
    goals int, assists int, points int, shots int, plus_minus int, pim int,
    hits int, blocks int, giveaways int, takeaways int, toi numeric, ppg int, faceoff_pct numeric,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_nhl_sk_player ON nhl_skater_box (player_id, game_date);

CREATE TABLE IF NOT EXISTS nhl_goalie_box (
    game_id text NOT NULL, player_id text NOT NULL,
    game_date date, team_id text, opp_team_id text, is_home boolean,
    shots_against int, saves int, goals_against int, save_pct numeric, toi numeric, decision text,
    PRIMARY KEY (game_id, player_id)
);

-- ========================= odds (append-only snapshots) =========================
-- Player props from DFS apps (PrizePicks / Underdog). One row per projection per snapshot.
-- Re-snapshotting through the day captures open->close line movement (CLV).
CREATE TABLE IF NOT EXISTS odds_prop_snapshot (
    id              bigserial PRIMARY KEY,
    snapshot_ts     timestamptz DEFAULT now(),
    sport           text,
    source          text,                 -- 'prizepicks' | 'underdog'
    source_player_id text,
    player_name     text,
    team            text,
    opp_team        text,
    stat_type       text,                 -- raw market name e.g. 'Points','Pitcher Strikeouts'
    line            numeric,
    line_type       text,                 -- 'standard' | 'demon' | 'goblin'
    over_mult       numeric,              -- payout multipliers (Underdog); null = pick'em even
    under_mult      numeric,
    start_ts        timestamptz,
    game_ref        text,
    raw             jsonb
);
CREATE INDEX IF NOT EXISTS idx_prop_snap   ON odds_prop_snapshot (sport, source, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_prop_player ON odds_prop_snapshot (sport, lower(player_name), stat_type);

-- Game markets (moneyline / spread / total) from ESPN odds feed (and optionally The Odds API).
CREATE TABLE IF NOT EXISTS odds_game_snapshot (
    id          bigserial PRIMARY KEY,
    snapshot_ts timestamptz DEFAULT now(),
    sport       text,
    source      text,                     -- 'espn' | 'oddsapi'
    event_ref   text,                     -- source event id
    game_id     text,                     -- resolved sportsedge game id (nullable)
    commence_ts timestamptz,
    home_team   text,
    away_team   text,
    book        text,                     -- provider/bookmaker
    market      text,                     -- 'h2h' | 'spread' | 'total'
    outcome     text,                     -- 'home'|'away'|'over'|'under'
    line        numeric,                  -- spread/total points (null for h2h)
    price       int,                      -- american odds
    raw         jsonb
);
CREATE INDEX IF NOT EXISTS idx_game_odds ON odds_game_snapshot (sport, event_ref, snapshot_ts);

-- ========================= analysis layer =========================
-- DFS prop player (source + source_player_id) -> our player_id.
CREATE TABLE IF NOT EXISTS player_xwalk (
    sport            text,
    source           text,           -- 'prizepicks' | 'underdog'
    source_player_id text,
    dfs_name         text,
    player_id        text,           -- resolved sportsedge player id (null if unmatched)
    matched_name     text,
    method           text,           -- 'exact' | 'initial_last' | 'manual' | 'unmatched' | 'combo'
    updated_at       timestamptz DEFAULT now(),
    PRIMARY KEY (sport, source, source_player_id)
);

-- Graded props: one row per (snapshot prop) once the game has a box score.
CREATE TABLE IF NOT EXISTS prop_graded (
    id           bigserial PRIMARY KEY,
    sport        text, source text, source_player_id text, player_id text,
    player_name  text, stat_type text, box_stat text,
    line         numeric, line_type text, over_mult numeric, under_mult numeric,
    game_date    date, actual numeric, result text,   -- 'over'|'under'|'push'
    open_line    numeric, close_line numeric, clv numeric,  -- line movement (CLV)
    snapshot_ts  timestamptz,
    UNIQUE (sport, source, source_player_id, stat_type, line_type, game_date)
);
CREATE INDEX IF NOT EXISTS idx_graded_player ON prop_graded (sport, player_id, game_date);

-- ========================= ops =========================
CREATE TABLE IF NOT EXISTS collect_log (
    id        bigserial PRIMARY KEY,
    run_ts    timestamptz DEFAULT now(),
    job       text, sport text, target_date date,
    n_games   int, n_rows int, status text, message text
);
