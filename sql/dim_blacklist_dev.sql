-- Schema: dim_blacklist_dev
-- Source: Meridian analysis (MERIDIAN_ANALYSIS.md section 6 insights #1 and #2).
--
-- The 13-entry whitelist on dim_address_label is the wrong direction for new
-- tokens — the long tail of new mints is dominated by unknown deployers.
-- A small dev blocklist catches more rugs than a large whitelist, because
-- `dev_rug_count >= 1` (sourced from dim_token_okx) is the single strongest
-- rug predictor we have.
--
-- During screening, the flow is:
--   1. fetch authority -> dev_wallet = mint_authority
--   2. check v_dev_blacklist_active for dev_wallet -> if hit, REJECT
--   3. (otherwise) fetch enrichments
--
-- This table is the right side of that gate.
--
-- Storage: SQLite (Universal SQL Agent stack).

CREATE TABLE IF NOT EXISTS dim_blacklist_dev (
    dev_wallet               TEXT PRIMARY KEY,        -- base58 pubkey (dev / mint authority)
    reason                   TEXT NOT NULL,           -- 'rug_pull' | 'serial_rugger' | 'scam' | 'manual'
    evidence_mint            TEXT,                    -- the mint where the rug was first observed
    evidence_source          TEXT,                    -- 'okx_advanced_info' | 'manual' | 'auto_from_screening'
    dev_rug_count_at_time    INTEGER,                 -- the dev_rug_count value when the entry was added
    first_token_seen         TIMESTAMP,
    last_token_seen          TIMESTAMP,
    notes                    TEXT,
    added_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    added_by                 TEXT NOT NULL DEFAULT 'auto',  -- 'auto' | <user handle>
    active                   BOOLEAN NOT NULL DEFAULT 1,
    CHECK (reason IN ('rug_pull', 'serial_rugger', 'scam', 'manual'))
);

-- Partial index on active rows: vast majority of lookups are "is this dev
-- currently blacklisted?" -> needs to scan only active=1 rows.
CREATE INDEX IF NOT EXISTS idx_dim_blacklist_dev_active
    ON dim_blacklist_dev(dev_wallet) WHERE active = 1;

-- View: active entries only. Use this in the screening flow:
--   SELECT 1 FROM v_dev_blacklist_active WHERE dev_wallet = :dev
DROP VIEW IF EXISTS v_dev_blacklist_active;
CREATE VIEW v_dev_blacklist_active AS
SELECT
    dev_wallet,
    reason,
    evidence_mint,
    evidence_source,
    dev_rug_count_at_time,
    first_token_seen,
    last_token_seen,
    notes,
    added_at,
    added_by
FROM dim_blacklist_dev
WHERE active = 1;
