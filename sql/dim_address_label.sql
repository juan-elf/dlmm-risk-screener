-- Schema: dim_address_label
-- Source: Opus review item #3 (priority order). Whitelist of known-good
-- authority addresses so that `v_token_authority_risk.verdict = 'review_mint'`
-- can be resolved to 'safe' when the authority is on this list.
--
-- Storage: SQLite (Universal SQL Agent stack).
--
-- IMPORTANT: this is a TRUST signal, not a SAFETY guarantee. Even a known-good
-- authority (Circle) can depeg, freeze unexpectedly, or be compromised. The
-- risk flag stays visible; only the verdict changes. See DESIGN_NOTES.md.

CREATE TABLE IF NOT EXISTS dim_address_label (
    address        TEXT PRIMARY KEY,         -- base58 pubkey
    kind           TEXT NOT NULL,            -- 'known_good_authority' | 'cex_hot' |
                                             -- 'burn' | 'vesting' | 'router' |
                                             -- 'lp_program' | 'unknown'
    entity         TEXT,                     -- e.g. 'Circle' | 'Tether' | 'Marinade'
    category       TEXT,                     -- 'stablecoin' | 'lst' | 'dex' | 'bridge' |
                                             -- 'wrapped' | 'meme' | 'protocol'
    source         TEXT NOT NULL,            -- 'helius_live' | 'official_docs' | 'manual'
    confidence     TEXT NOT NULL DEFAULT 'medium',  -- 'high' | 'medium' | 'low'
    notes          TEXT,
    added_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at    TIMESTAMP,
    active         BOOLEAN NOT NULL DEFAULT 1,
    CHECK (kind IN (
        'known_good_authority',  -- whitelist for v_token_authority_risk
        'cex_hot',               -- CEX hot wallet (for top10_holder exclusion later)
        'burn',                  -- burn address / null address
        'vesting',               -- Streamflow / vesting program
        'router',                -- Jupiter / aggregator router
        'lp_program',            -- Meteora / Raydium / Orca LP program PDA
        'protocol_treasury',     -- known protocol treasury
        'unknown'                -- placeholder for review
    ))
);

CREATE INDEX IF NOT EXISTS idx_dim_address_label_kind
    ON dim_address_label(kind) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_dim_address_label_entity
    ON dim_address_label(entity) WHERE active = 1;

-- View: effective verdict post-whitelist. Resolves `review_mint` -> `safe` if
-- mint_authority is a known_good_authority. Preserves the raw flag for audit.
--
-- This is the QUERY TO USE for downstream decisions (LP risk, UI display).
DROP VIEW IF EXISTS v_effective_authority_verdict;
CREATE VIEW v_effective_authority_verdict AS
SELECT
    r.token_mint,
    r.unrenounced_mint_warning,
    r.unrenounced_freeze_warning,
    r.t22_perm_delegate_hard_avoid,
    r.authority_verdict AS raw_verdict,
    CASE
        -- Hard gate first: T22 perm delegate is never whitelistable
        WHEN r.t22_perm_delegate_hard_avoid = 1 THEN 'avoid'
        -- If mint authority is whitelisted, resolve review_mint -> safe
        WHEN r.authority_verdict = 'review_mint'
             AND l.kind = 'known_good_authority'
             AND l.active = 1
        THEN 'safe'
        -- If freeze authority is whitelisted, resolve review_freeze -> safe
        WHEN r.authority_verdict = 'review_freeze'
             AND f.kind = 'known_good_authority'
             AND f.active = 1
        THEN 'safe'
        -- Otherwise keep raw verdict
        ELSE r.authority_verdict
    END AS effective_verdict,
    r.authority_verdict AS base_verdict,
    l.entity AS mint_authority_entity,
    f.entity AS freeze_authority_entity,
    l.confidence AS mint_authority_confidence,
    f.confidence AS freeze_authority_confidence,
    r.checked_at
FROM v_token_authority_risk r
LEFT JOIN dim_token_authority ta ON ta.token_mint = r.token_mint
LEFT JOIN dim_address_label l
    ON l.address = ta.mint_authority AND l.active = 1
LEFT JOIN dim_address_label f
    ON f.address = ta.freeze_authority AND f.active = 1;
