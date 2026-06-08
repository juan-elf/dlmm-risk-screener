-- Schema: dim_token_jup
-- Source: Meridian analysis (MERIDIAN_ANALYSIS.md sections 2 & 3) — Build 1.
-- A single Jupiter datapi /v1/assets/search call returns 10+ risk signals
-- that we previously had to derive piecemeal (or did not have at all).
--
-- Storage: SQLite (Universal SQL Agent stack).
--
-- This is an enrichment layer that AUGMENTS dim_token_authority — it does
-- NOT replace it. Authority decoding (especially Token-2022 perm-delegate)
-- still requires the on-chain mint account read; Jupiter only mirrors the
-- audit.mintAuthorityDisabled / audit.freezeAuthorityDisabled booleans.
--
-- The KEY signal Jupiter gives us that we couldn't get anywhere else is
-- `global_fees_sol` — cumulative priority + jito tips paid by traders of
-- this token. Per Meridian config (`config.js:78`) and Discord pre-checks
-- (`discord-listener/pre-checks.js:130-159`), sub-30-SOL cumulative fees
-- is a hard rejection: organic markets pay real fees.

CREATE TABLE IF NOT EXISTS dim_token_jup (
    token_mint               TEXT PRIMARY KEY,
    jup_id                   TEXT,         -- Jupiter's internal asset id; null for unknowns
    symbol                   TEXT,
    name                     TEXT,
    decimals                 INTEGER,
    organic_score            NUMERIC,      -- 0-100; null if Jupiter has no opinion
    organic_score_label      TEXT,         -- 'high' | 'medium' | 'low'
    holder_count             INTEGER,      -- null for very new tokens
    mcap_usd                 NUMERIC,      -- market cap from Jupiter
    launchpad                TEXT,         -- 'pump.fun' | 'meteora_launchpad' | NULL
    -- KEY SIGNAL: cumulative priority + jito tips in SOL paid by this token's
    -- traders. Sub-30-SOL = bundler/scam proxy per Meridian thresholds.
    global_fees_sol          NUMERIC,
    -- Audit flags (mirror Jupiter's `audit.*` object)
    audit_mint_disabled      BOOLEAN,
    audit_freeze_disabled    BOOLEAN,
    audit_top_holders_pct    NUMERIC,      -- top-10 holder concentration, 0-100
    audit_bot_holders_pct    NUMERIC,      -- 0-100; "5-25% normal" per Meridian
    audit_dev_migrations     INTEGER,      -- dev's prior token migrations
    audit_total_holders      INTEGER,
    audit_risky_holders      INTEGER,
    raw_json                 TEXT,         -- full Jupiter response for future fields
    fetched_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_chain          BOOLEAN,      -- last fetch returned live data
    error                    TEXT          -- NULL on success; 'not_found' / 'http:NNN' / etc.
);

CREATE INDEX IF NOT EXISTS idx_dim_token_jup_organic_score
    ON dim_token_jup(organic_score);
CREATE INDEX IF NOT EXISTS idx_dim_token_jup_global_fees_sol
    ON dim_token_jup(global_fees_sol);
CREATE INDEX IF NOT EXISTS idx_dim_token_jup_mcap_usd
    ON dim_token_jup(mcap_usd);
CREATE INDEX IF NOT EXISTS idx_dim_token_jup_top_holders_pct
    ON dim_token_jup(audit_top_holders_pct);
CREATE INDEX IF NOT EXISTS idx_dim_token_jup_bot_holders_pct
    ON dim_token_jup(audit_bot_holders_pct);
CREATE INDEX IF NOT EXISTS idx_dim_token_jup_fetched_at
    ON dim_token_jup(fetched_at DESC);

-- Per-mint risk view: materializes Meridian's thresholds as 0/1 flags so
-- downstream queries can `WHERE gate_fees_too_low = 0 AND gate_top_holders = 0`
-- without recomputing the cutoffs.
--
-- Thresholds (from MERIDIAN_ANALYSIS.md section 2):
--   global_fees_sol     < 30  -> hard reject (bundler/scam proxy)
--   top_holders_pct     > 60  -> hard reject (single-wallet price control)
--   bot_holders_pct     > 30  -> hard reject (wash/sniper bots dominate)
--   organic_score       < 60  -> soft flag  (Jupiter's own composite score)
--   audit_mint_disabled = 0   -> soft flag  (cross-check with dim_address_label)
DROP VIEW IF EXISTS v_token_jup_risk;
CREATE VIEW v_token_jup_risk AS
SELECT
    token_mint,
    symbol,
    name,
    organic_score,
    holder_count,
    mcap_usd,
    launchpad,
    global_fees_sol,
    audit_mint_disabled,
    audit_freeze_disabled,
    audit_top_holders_pct,
    audit_bot_holders_pct,
    audit_dev_migrations,
    CASE WHEN global_fees_sol IS NOT NULL AND global_fees_sol < 30
         THEN 1 ELSE 0 END
        AS gate_fees_too_low,
    CASE WHEN audit_top_holders_pct IS NOT NULL AND audit_top_holders_pct > 60
         THEN 1 ELSE 0 END
        AS gate_top_holders,
    CASE WHEN audit_bot_holders_pct IS NOT NULL AND audit_bot_holders_pct > 30
         THEN 1 ELSE 0 END
        AS gate_bot_holders,
    CASE WHEN organic_score IS NOT NULL AND organic_score < 60
         THEN 1 ELSE 0 END
        AS gate_organic_score,
    CASE WHEN audit_mint_disabled = 0
         THEN 1 ELSE 0 END
        AS gate_no_mint_disable,
    CASE WHEN error IS NOT NULL
         THEN 1 ELSE 0 END
        AS is_rugged_or_unknown,
    fetched_at
FROM dim_token_jup;
