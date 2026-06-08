-- Schema: dim_token_authority
-- Source: review of /mnt/e/Wallet_Tracking/dlmm_risk_schema.md by Claude Opus
-- Storage: SQLite (sesuai stack Universal SQL Agent di D:\universal-sql-agent)
--
-- One row per token mint. Decoded from raw Solana RPC getAccountInfo.
-- This is the "highest-leverage" table per Opus: ~half of rugs have non-renounced
-- mint authority, so this single table catches them as a hard gate.

CREATE TABLE IF NOT EXISTS dim_token_authority (
    token_mint                 TEXT PRIMARY KEY,
    mint_authority             TEXT,        -- NULL = renounced (good)
    freeze_authority           TEXT,        -- NULL = no freeze risk (good)
    update_authority           TEXT,        -- metadata updater (Token-2022 / Metaplex)
    mint_authority_revoked_at  TIMESTAMP,   -- historically, when did it become NULL?
    freeze_authority_revoked_at TIMESTAMP,
    is_mutable_metadata        BOOLEAN,
    is_token_2022              BOOLEAN,
    has_permanent_delegate     BOOLEAN,     -- Token-2022 honeypot trap
    program_id                 TEXT,        -- SPL Token vs Token-2022
    supply                     NUMERIC,     -- current supply (snapshot at fetch time)
    decimals                   INTEGER,
    raw_account_size           INTEGER,     -- 82 (SPL) / 182 (Token-2022 mint) / etc.
    checked_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_chain            BOOLEAN,     -- last fetch returned live data (vs. cached)
    error                      TEXT,        -- NULL on success; 'not_found' / 'unparseable' / etc.
    source                     TEXT NOT NULL DEFAULT 'helius_rpc'
);

CREATE INDEX IF NOT EXISTS idx_dim_token_authority_mint_auth
    ON dim_token_authority(mint_authority) WHERE mint_authority IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dim_token_authority_freeze_auth
    ON dim_token_authority(freeze_authority) WHERE freeze_authority IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dim_token_authority_perm_delegate
    ON dim_token_authority(has_permanent_delegate) WHERE has_permanent_delegate = 1;
CREATE INDEX IF NOT EXISTS idx_dim_token_authority_checked_at
    ON dim_token_authority(checked_at DESC);

-- Risk feature view: three independent warnings, one hard gate.
--
-- Per live-test observation (2026-06-06, Helius mainnet):
--   USDC, USDT, mSOL, JLP, WBTC, and many legitimate tokens intentionally
--   DO NOT renounce mint authority, because they need to mint/burn as part
--   of normal operations (stablecoin issuance, liquid staking rebasing, etc).
--   Flagging these as "avoid" produces false positives on most blue-chip
--   Solana assets.
--
-- Opus's original "hard gate" therefore gets split into:
--   - unrenounced_mint_warning    : SOFT  (informational; cross-check with
--                                            dim_address_label whitelist later)
--   - unrenounced_freeze_warning  : SOFT  (informational; same note)
--   - t22_perm_delegate_hard_avoid: HARD  (Token-2022 permanent delegate
--                                            is a known rug/honeypot primitive,
--                                            no legitimate use case)
--   - authority_verdict: composite verdict
--       'avoid'        : hard gate tripped (Token-2022 perm delegate)
--       'review_mint'  : mint authority not renounced (not auto-avoid; need
--                        dim_address_label whitelist to confirm)
--       'review_freeze': freeze authority not renounced
--       'safe'         : all renounced and no Token-2022 perm delegate
DROP VIEW IF EXISTS v_token_authority_risk;
CREATE VIEW v_token_authority_risk AS
SELECT
    token_mint,
    CASE WHEN mint_authority IS NOT NULL
         THEN 1 ELSE 0 END
        AS unrenounced_mint_warning,
    CASE WHEN freeze_authority IS NOT NULL
         THEN 1 ELSE 0 END
        AS unrenounced_freeze_warning,
    CASE WHEN is_token_2022 = 1 AND has_permanent_delegate = 1
         THEN 1 ELSE 0 END
        AS t22_perm_delegate_hard_avoid,
    CASE
        WHEN is_token_2022 = 1 AND has_permanent_delegate = 1
            THEN 'avoid'
        WHEN mint_authority IS NOT NULL
            THEN 'review_mint'
        WHEN freeze_authority IS NOT NULL
            THEN 'review_freeze'
        ELSE 'safe'
    END AS authority_verdict,
    checked_at
FROM dim_token_authority
WHERE error IS NULL;
