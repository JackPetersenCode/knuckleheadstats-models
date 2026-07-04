-- sportsedge schema — v2 predictive-feature layer (additive, idempotent).
-- Adds: MLB pitch-level Statcast + game context, player handedness,
-- NFL snap counts + advanced offensive stats, NHL advanced (xG) metrics,
-- and cross-sport rest / back-to-back helper view.
-- Safe to re-run; all statements are IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

-- ========================= player handedness (MLB + others) =========================
ALTER TABLE player ADD COLUMN IF NOT EXISTS bat_side  text;   -- 'L' | 'R' | 'S'
ALTER TABLE player ADD COLUMN IF NOT EXISTS throw_hand text;  -- 'L' | 'R'

-- ========================= MLB: pitch-level Statcast =========================
-- One row per pitch (Baseball Savant 'details' feed). batter/pitcher are MLBAM
-- ids = statsapi person ids = our mlb player_id, so this joins to box tables directly.
-- game_pk = statsapi gamePk = our mlb game_id.
CREATE TABLE IF NOT EXISTS mlb_statcast_pitch (
    game_pk          int  NOT NULL,
    at_bat_number    int  NOT NULL,
    pitch_number     int  NOT NULL,
    game_date        date,
    game_year        int,
    game_type        text,
    pitcher          int,            -- MLBAM id
    batter           int,            -- MLBAM id
    pitcher_name     text,
    stand            text,           -- batter side L/R
    p_throws         text,           -- pitcher hand L/R
    pitch_type       text,
    pitch_name       text,
    -- release / movement
    release_speed       numeric,
    effective_speed     numeric,
    release_spin_rate   numeric,
    spin_axis           numeric,
    release_extension   numeric,
    release_pos_x       numeric,
    release_pos_y       numeric,
    release_pos_z       numeric,
    pfx_x               numeric,
    pfx_z               numeric,
    arm_angle           numeric,
    -- location
    plate_x   numeric,
    plate_z   numeric,
    zone      int,
    sz_top    numeric,
    sz_bot    numeric,
    -- count / state
    balls         int,
    strikes       int,
    outs_when_up  int,
    inning        int,
    inning_topbot text,
    on_1b int, on_2b int, on_3b int,
    n_thruorder_pitcher int,
    pitcher_days_since_prev_game int,
    -- outcome
    type        text,   -- B|S|X
    description text,
    events      text,
    des         text,
    bb_type      text,
    hit_location int,
    hc_x numeric, hc_y numeric,
    -- batted-ball tracking
    launch_speed     numeric,
    launch_angle     numeric,
    hit_distance_sc  numeric,
    bat_speed        numeric,
    swing_length     numeric,
    launch_speed_angle int,
    -- expected stats
    estimated_ba_using_speedangle   numeric,
    estimated_woba_using_speedangle numeric,
    estimated_slg_using_speedangle  numeric,
    woba_value   numeric,
    woba_denom   numeric,
    babip_value  numeric,
    iso_value    numeric,
    -- run/win value
    delta_run_exp         numeric,
    delta_home_win_exp    numeric,
    delta_pitcher_run_exp numeric,
    -- fielding alignment / score context
    if_fielding_alignment text,
    of_fielding_alignment text,
    home_team text, away_team text,
    home_score int, away_score int, bat_score int, fld_score int,
    PRIMARY KEY (game_pk, at_bat_number, pitch_number)
);
CREATE INDEX IF NOT EXISTS idx_sc_pitcher ON mlb_statcast_pitch (pitcher, game_date);
CREATE INDEX IF NOT EXISTS idx_sc_batter  ON mlb_statcast_pitch (batter, game_date);
CREATE INDEX IF NOT EXISTS idx_sc_date    ON mlb_statcast_pitch (game_date);

-- ========================= MLB: per-game context =========================
-- Weather, umpires, probable pitchers, day/night, attendance. One row per game.
CREATE TABLE IF NOT EXISTS mlb_game_context (
    game_id        text PRIMARY KEY,   -- = mlb game_id (gamePk)
    game_date      date,
    venue          text,
    day_night      text,
    attendance     int,
    weather_cond   text,
    temp_f         int,
    wind_mph       int,
    wind_dir       text,
    ump_home       text,
    ump_1b         text,
    ump_2b         text,
    ump_3b         text,
    home_prob_pitcher_id text,
    away_prob_pitcher_id text,
    home_prob_pitcher    text,
    away_prob_pitcher    text,
    updated_at     timestamptz DEFAULT now()
);

-- ========================= NFL: snap counts (nflverse) =========================
-- Keyed by nflverse pfr id + season/week (separate id space from ESPN box).
CREATE TABLE IF NOT EXISTS nfl_snap_counts (
    season       int  NOT NULL,
    week         int  NOT NULL,
    game_type    text,
    nfl_game_id  text,            -- nflverse game_id e.g. 2024_01_ARI_BUF
    pfr_player_id text NOT NULL,
    player_name  text,
    position     text,
    team         text,
    opponent     text,
    offense_snaps int, offense_pct numeric,
    defense_snaps int, defense_pct numeric,
    st_snaps      int, st_pct      numeric,
    PRIMARY KEY (season, week, pfr_player_id, team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_snaps_player ON nfl_snap_counts (player_name, season, week);

-- ========================= NFL: advanced offensive stats (nflverse) =========================
-- Air yards, target share, EPA, etc. Per player per week.
CREATE TABLE IF NOT EXISTS nfl_player_advanced (
    player_id    text NOT NULL,     -- nflverse gsis id
    player_name  text,
    season       int  NOT NULL,
    week         int  NOT NULL,
    season_type  text,
    team         text,
    position     text,
    -- passing
    completions int, attempts int, passing_yards int, passing_tds int, interceptions int,
    sacks numeric, passing_air_yards int, passing_yards_after_catch int,
    passing_epa numeric, pacr numeric, dakota numeric,
    -- rushing
    carries int, rushing_yards int, rushing_tds int, rushing_epa numeric,
    -- receiving
    receptions int, targets int, receiving_yards int, receiving_tds int,
    receiving_air_yards int, receiving_yards_after_catch int,
    target_share numeric, air_yards_share numeric, wopr numeric, receiving_epa numeric,
    -- usage
    fantasy_points numeric, fantasy_points_ppr numeric,
    PRIMARY KEY (player_id, season, week)
);
CREATE INDEX IF NOT EXISTS idx_nfl_adv_name ON nfl_player_advanced (player_name, season, week);

-- ========================= NHL: advanced metrics (MoneyPuck) =========================
-- Season-summary expected-goals / Corsi / Fenwick. playerId = NHL id = our player_id.
CREATE TABLE IF NOT EXISTS nhl_skater_advanced (
    player_id    text NOT NULL,
    season       int  NOT NULL,
    situation    text NOT NULL,    -- 'all' | '5on5' | '4on5' | '5on4' | 'other'
    name         text,
    team         text,
    position     text,
    games_played int,
    icetime      numeric,
    gameScore    numeric,
    onIce_xGoalsPercentage  numeric,
    onIce_corsiPercentage   numeric,
    onIce_fenwickPercentage numeric,
    I_F_xGoals   numeric,
    I_F_xOnGoal  numeric,
    I_F_shotAttempts numeric,
    I_F_goals    numeric,
    I_F_points   numeric,
    I_F_primaryAssists numeric,
    I_F_hits     numeric,
    I_F_takeaways numeric,
    I_F_giveaways numeric,
    PRIMARY KEY (player_id, season, situation)
);

CREATE TABLE IF NOT EXISTS nhl_goalie_advanced (
    player_id    text NOT NULL,
    season       int  NOT NULL,
    situation    text NOT NULL,
    name         text,
    team         text,
    games_played int,
    icetime      numeric,
    xGoals       numeric,           -- expected goals against
    goals        numeric,           -- actual goals against
    ongoal       numeric,           -- shots on goal faced (saves = ongoal - goals)
    xRebounds    numeric,
    highDangerShots  numeric,
    highDangerxGoals numeric,
    highDangerGoals  numeric,
    PRIMARY KEY (player_id, season, situation)
);

-- ========================= cross-sport: rest / back-to-back =========================
-- Days of rest and B2B flag per team per game, derived from the game schedule.
-- Useful feature for every sport (fatigue effect on player/team performance).
CREATE OR REPLACE VIEW v_team_rest AS
WITH tg AS (
    SELECT sport, game_id, game_date, home_team_id AS team_id, true  AS is_home FROM game WHERE game_date IS NOT NULL
    UNION ALL
    SELECT sport, game_id, game_date, away_team_id AS team_id, false AS is_home FROM game WHERE game_date IS NOT NULL
)
SELECT sport, game_id, team_id, is_home, game_date,
       game_date - LAG(game_date) OVER (PARTITION BY sport, team_id ORDER BY game_date) AS days_rest,
       (game_date - LAG(game_date) OVER (PARTITION BY sport, team_id ORDER BY game_date)) = 1 AS is_back_to_back
FROM tg;
