-- Schema: fact_lp_position
-- Source: Opus review item #2. YOUR own LP positions on Meteora DLMM.
-- This is the feedback loop: every position you close becomes a labeled
-- training example, so the system can later answer: "did the risk_score at
-- deposit time correlate with the actual P&L outcome?"
--
-- Storage: SQLite (Universal SQL Agent stack).
--
-- A position is identified by its position NFT mint (Meteora uses an NFT
-- per LP position). We snapshot (or fetch on demand) the position state.
-- Schema supports both current-state snapshots AND historical events
-- (open / add / remove / claim_fee / close).

CREATE TABLE IF NOT EXISTS fact_lp_position (
    position_id        TEXT PRIMARY KEY,    -- the Meteora position NFT mint
    owner_wallet       TEXT NOT NULL,
    pool_address       TEXT NOT NULL,       -- Meteora LbPair address
    token_x_mint       TEXT,
    token_y_mint       TEXT,
    bin_lower          INTEGER,             -- inclusive lower bin id
    bin_upper          INTEGER,             -- inclusive upper bin id
    bin_step           INTEGER,             -- pool bin step (basis points)
    status             TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed'
    opened_at          TIMESTAMP,
    closed_at          TIMESTAMP,
    -- Initial deposit (snapshot at open)
    initial_x_amount   NUMERIC,
    initial_y_amount   NUMERIC,
    initial_usd        NUMERIC,
    -- Current state (NULL for closed positions)
    current_x_amount   NUMERIC,
    current_y_amount   NUMERIC,
    current_usd        NUMERIC,
    -- Fees
    fees_x_claimed     NUMERIC DEFAULT 0,
    fees_y_claimed     NUMERIC DEFAULT 0,
    fees_usd_claimed   NUMERIC DEFAULT 0,
    -- Computed metrics (refreshed on every fetch)
    hold_value_usd     NUMERIC,             -- value if you'd just held (no LP)
    il_usd             NUMERIC,             -- (current_usd + fees_usd) - hold_value_usd
    il_pct             NUMERIC,             -- il_usd / initial_usd
    pnl_usd            NUMERIC,             -- (current_usd + fees_usd) - initial_usd
    pnl_pct            NUMERIC,             -- pnl_usd / initial_usd
    -- Risk score at deposit time (joined from token_risk_score if available)
    risk_score_at_open NUMERIC,
    verdict_at_open    TEXT,
    -- Audit
    last_refreshed_at  TIMESTAMP,
    last_event_at      TIMESTAMP,           -- last add/remove/claim event
    source             TEXT NOT NULL DEFAULT 'meteora_api',
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_fact_lp_position_owner_status
    ON fact_lp_position(owner_wallet, status);
CREATE INDEX IF NOT EXISTS idx_fact_lp_position_pool
    ON fact_lp_position(pool_address);
CREATE INDEX IF NOT EXISTS idx_fact_lp_position_opened
    ON fact_lp_position(opened_at);
CREATE INDEX IF NOT EXISTS idx_fact_lp_position_pnl
    ON fact_lp_position(pnl_usd);

-- Position event log: every add/remove/claim/fee event.
-- One row per on-chain LP event. Useful for time-series analysis.
CREATE TABLE IF NOT EXISTS fact_lp_event (
    event_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id        TEXT NOT NULL,
    tx_signature       TEXT,
    block_time         TIMESTAMP,
    block_slot         BIGINT,
    event_type         TEXT NOT NULL,        -- 'open' | 'add' | 'remove' | 'claim_fee' | 'close'
    token_x_delta      NUMERIC,              -- positive for add, negative for remove
    token_y_delta      NUMERIC,
    usd_delta          NUMERIC,              -- USD value of the change
    fees_x_claimed     NUMERIC DEFAULT 0,
    fees_y_claimed     NUMERIC DEFAULT 0,
    fees_usd_claimed   NUMERIC DEFAULT 0,
    bin_lower          INTEGER,              -- bin range at event time
    bin_upper          INTEGER,
    UNIQUE (position_id, tx_signature, event_type)
);

CREATE INDEX IF NOT EXISTS idx_fact_lp_event_position
    ON fact_lp_event(position_id, block_time);
CREATE INDEX IF NOT EXISTS idx_fact_lp_event_tx
    ON fact_lp_event(tx_signature);

-- Outcome view: aggregate P&L stats per position.
DROP VIEW IF EXISTS v_lp_outcome_summary;
CREATE VIEW v_lp_outcome_summary AS
SELECT
    p.position_id,
    p.owner_wallet,
    p.pool_address,
    p.token_x_mint,
    p.token_y_mint,
    p.status,
    p.opened_at,
    p.closed_at,
    CASE WHEN p.closed_at IS NOT NULL AND p.opened_at IS NOT NULL
         THEN (julianday(p.closed_at) - julianday(p.opened_at)) * 86400.0
    END AS hold_seconds,
    p.initial_usd,
    p.current_usd,
    p.fees_usd_claimed,
    p.pnl_usd,
    p.pnl_pct,
    p.il_usd,
    p.il_pct,
    p.risk_score_at_open,
    p.verdict_at_open,
    CASE
        WHEN p.status = 'closed' AND p.pnl_usd > 0     THEN 'win'
        WHEN p.status = 'closed' AND p.pnl_usd <= 0    THEN 'loss'
        WHEN p.status = 'open'                         THEN 'open'
    END AS outcome,
    p.last_refreshed_at
FROM fact_lp_position p;

-- Aggregate view: how well did the risk_score predict losses?
DROP VIEW IF EXISTS v_risk_score_validation;
CREATE VIEW v_risk_score_validation AS
SELECT
    verdict_at_open,
    COUNT(*) AS n_positions,
    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS n_wins,
    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS n_losses,
    AVG(pnl_usd)    AS avg_pnl_usd,
    AVG(pnl_pct)    AS avg_pnl_pct,
    AVG(il_pct)     AS avg_il_pct,
    SUM(pnl_usd)    AS total_pnl_usd
FROM fact_lp_position
WHERE pnl_usd IS NOT NULL
GROUP BY verdict_at_open;
