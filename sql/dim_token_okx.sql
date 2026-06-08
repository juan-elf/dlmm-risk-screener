-- Schema: dim_token_okx
-- Source: Meridian analysis (MERIDIAN_ANALYSIS.md sections 2, 3, 6) — Build 2.
-- OKX OnchainOS exposes free public Solana endpoints (no API key, just a
-- `Ok-Access-Client-type: agent-cli` header) that bundle the strongest
-- single rug predictor we don't already have: `dev_rug_count` (how many
-- tokens this dev has rugged before). See `tools/okx.js:166-197` in
-- /mnt/e/meridian for the reference parser, and the Insight #3 / #5 notes
-- in MERIDIAN_ANALYSIS.md section 6.
--
-- We fold three endpoints into one row per mint:
--   /advanced-info  -> risk_level, bundle/sniper/suspicious/dev/top10/lp %,
--                       dev_rug_count, dev_token_count, tags
--   /risk/new/check -> is_honeypot, is_rugpull, is_wash
--   /price-info     -> ath, atl, current_price_usd, price_vs_ath_pct
--
-- Each endpoint is independently optional: a partial response (2/3 endpoints
-- succeeded) still gets stored with `last_seen_chain=1`. A total failure
-- gets `error` set and `last_seen_chain=0`.
--
-- Storage: SQLite (Universal SQL Agent stack).

CREATE TABLE IF NOT EXISTS dim_token_okx (
    token_mint               TEXT PRIMARY KEY,
    -- /advanced-info derived ------------------------------------------------
    risk_level               INTEGER,      -- 1-5; null if unknown
    bundle_pct               NUMERIC,      -- 0-100; from bundleHoldingPercent
    sniper_pct               NUMERIC,      -- 0-100; sniperHoldingPercent
    suspicious_pct           NUMERIC,      -- 0-100; suspiciousHoldingPercent
    dev_holding_pct          NUMERIC,      -- 0-100; devHoldingPercent
    top10_pct                NUMERIC,      -- 0-100; top10HoldPercent
    lp_burned_pct            NUMERIC,      -- 0-100; lpBurnedPercent
    -- HEADLINE SIGNAL: dev_rug_count. Per MERIDIAN_ANALYSIS section 6 #3 this
    -- is the single strongest rug predictor we don't currently have.
    dev_rug_count            INTEGER,
    dev_token_count          INTEGER,
    -- /risk/new/check derived ----------------------------------------------
    is_honeypot              BOOLEAN,
    is_rugpull               BOOLEAN,
    is_wash                  BOOLEAN,
    -- /price-info derived ---------------------------------------------------
    ath_usd                  NUMERIC,      -- maxPrice
    atl_usd                  NUMERIC,      -- minPrice
    current_price_usd        NUMERIC,
    price_vs_ath_pct         NUMERIC,      -- negative = down from ATH (price/ath - 100)
    -- tags & raw payloads ---------------------------------------------------
    tags_json                TEXT,         -- JSON array; e.g. ["smart_money_buy","low_liquidity"]
    raw_advanced_info_json   TEXT,
    raw_risk_check_json      TEXT,
    raw_price_info_json      TEXT,
    -- bookkeeping ----------------------------------------------------------
    fetched_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_chain          BOOLEAN,      -- 1 if at least one endpoint succeeded
    error                    TEXT,         -- NULL on success; aggregated otherwise
    source                   TEXT NOT NULL DEFAULT 'okx_web3'
);

-- Headline signal: query "show me every dev who has rugged before". Cheap.
CREATE INDEX IF NOT EXISTS idx_dim_token_okx_dev_rug_count
    ON dim_token_okx(dev_rug_count);
-- Partial index: only stores rows where is_honeypot=1, so honeypot lookups
-- are O(log N_honeypots) instead of O(N_total).
CREATE INDEX IF NOT EXISTS idx_dim_token_okx_honeypot
    ON dim_token_okx(token_mint) WHERE is_honeypot = 1;
CREATE INDEX IF NOT EXISTS idx_dim_token_okx_bundle
    ON dim_token_okx(bundle_pct);
CREATE INDEX IF NOT EXISTS idx_dim_token_okx_fetched_at
    ON dim_token_okx(fetched_at DESC);

-- Per-mint risk view: materializes Meridian's thresholds as 0/1 gate flags
-- so downstream queries can filter without recomputing the cutoffs.
--
-- Thresholds (MERIDIAN_ANALYSIS section 2; references config.js in /mnt/e/meridian):
--   dev_rug_count >= 1   -> HARD (Insight #3, the headline signal)
--   is_honeypot   = 1    -> HARD
--   is_rugpull    = 1    -> HARD
--   is_wash       = 1    -> HARD
--   bundle_pct    > 30   -> HARD (maxBundlePct)
--   top10_pct     > 60   -> HARD (maxTop10Pct, mirrors Jupiter gate)
--   sniper_pct    > 30   -> SOFT (informational; sniper alone isn't a rug)
--   risk_level    >= 4   -> SOFT (OKX's own 1-5 composite)
DROP VIEW IF EXISTS v_token_okx_risk;
CREATE VIEW v_token_okx_risk AS
SELECT
    token_mint,
    risk_level,
    dev_rug_count,
    dev_token_count,
    bundle_pct,
    sniper_pct,
    lp_burned_pct,
    is_honeypot,
    is_rugpull,
    is_wash,
    current_price_usd,
    price_vs_ath_pct,
    tags_json,
    -- HARD gates ------------------------------------------------------------
    CASE WHEN is_honeypot = 1 THEN 1 ELSE 0 END
        AS gate_is_honeypot,
    CASE WHEN is_rugpull = 1 THEN 1 ELSE 0 END
        AS gate_is_rugpull,
    CASE WHEN is_wash = 1 THEN 1 ELSE 0 END
        AS gate_is_wash,
    CASE WHEN dev_rug_count IS NOT NULL AND dev_rug_count >= 1
         THEN 1 ELSE 0 END
        AS gate_dev_rug_count,
    CASE WHEN bundle_pct IS NOT NULL AND bundle_pct > 30
         THEN 1 ELSE 0 END
        AS gate_bundle_high,
    CASE WHEN top10_pct IS NOT NULL AND top10_pct > 60
         THEN 1 ELSE 0 END
        AS gate_top10_concentrated,
    -- SOFT gates (informational) -------------------------------------------
    CASE WHEN sniper_pct IS NOT NULL AND sniper_pct > 30
         THEN 1 ELSE 0 END
        AS gate_sniper_high,
    CASE WHEN risk_level IS NOT NULL AND risk_level >= 4
         THEN 1 ELSE 0 END
        AS gate_risk_level_high,
    -- bookkeeping ----------------------------------------------------------
    CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END
        AS is_unknown,
    fetched_at
FROM dim_token_okx;
