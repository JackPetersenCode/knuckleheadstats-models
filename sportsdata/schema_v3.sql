-- schema v3: multi-bet-type value engine (additive + idempotent).
-- Extends value_play to hold game bets (h2h/spread/total) alongside props,
-- adds the daily ranked best_bets output and the per-category track-record table
-- that drives the ranker's confidence tiers.

ALTER TABLE value_play ADD COLUMN IF NOT EXISTS market_type text DEFAULT 'prop'; -- prop|h2h|spread|total
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS event_ref   text;     -- SGO eventID (game grading join)
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS home_team   text;     -- team abbrev (game settlement)
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS away_team   text;
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS model_fair  numeric;  -- projection-model anchor (props)
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS confidence  text;     -- ranker tier
ALTER TABLE value_play ADD COLUMN IF NOT EXISTS value_score numeric;  -- ranker score
UPDATE value_play SET market_type='prop' WHERE market_type IS NULL;

ALTER TABLE shop_play ADD COLUMN IF NOT EXISTS market_type text DEFAULT 'prop';

-- daily ranked output (one row per surfaced bet, all sports + bet types)
CREATE TABLE IF NOT EXISTS best_bets (
    game_date   date NOT NULL,
    sport       text NOT NULL,
    market_type text NOT NULL,
    selection   text NOT NULL,   -- player (prop) or matchup/team (game)
    stat_type   text,            -- prop market or game market
    line        numeric,
    side        text,
    bet_book    text,
    offered_dec numeric,
    fair_prob   numeric,
    ev          numeric,
    confidence  text,
    value_score numeric,
    rank        int,
    created_ts  timestamptz DEFAULT now(),
    PRIMARY KEY (game_date, sport, market_type, selection, stat_type, line, side, bet_book)
);
CREATE INDEX IF NOT EXISTS idx_best_bets_day ON best_bets (game_date, value_score DESC);

-- realized track record per category -> feeds the ranker's confidence tiers
CREATE TABLE IF NOT EXISTS value_category_stats (
    sport         text NOT NULL,
    market_type   text NOT NULL,
    bet_book      text NOT NULL,
    n_graded      int,
    roi           numeric,
    avg_clv       numeric,
    clv_pos_share numeric,
    updated_ts    timestamptz DEFAULT now(),
    PRIMARY KEY (sport, market_type, bet_book)
);
