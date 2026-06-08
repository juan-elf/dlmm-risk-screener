# Design Notes — dim_token_authority

## 2026-06-06: Live Helius test revealed Opus recommendation #1 was too aggressive

### What Opus said

> "Authority risk: +2 if mint_authority != None, +1 if freeze_authority != None,
> +3 if Token-2022 with permanent delegate. If `authority_risk >= 2` → verdict = 'avoid'."

### What live testing showed

Running the decoder against 8 mainnet SPL mints via Helius:

| Mint | mint_auth | freeze_auth | Original verdict | Should-be |
|------|-----------|-------------|------------------|-----------|
| USDC | held (Circle) | held (Circle) | `avoid` | `safe` (legit stablecoin) |
| USDT | held (Tether) | held (Tether) | `avoid` | `safe` (legit stablecoin) |
| mSOL | held (Marinade) | renounced | `avoid` | `safe` (LST rebasing) |
| wSOL | renounced | renounced | `safe` | `safe` ✓ |
| JUP / JTO / BONK / PYTH | renounced | renounced | `safe` | `safe` ✓ |

The original "any non-renounced authority = avoid" is too aggressive. Many
blue-chip Solana assets intentionally retain authority for legitimate
operations (stablecoin minting, LST rebasing, wrapped-asset redemption).

### Refinement: split into 3 independent flags + 1 composite verdict

| Flag | Type | What it means |
|------|------|---------------|
| `unrenounced_mint_warning` | SOFT | mint authority held — informational; needs whitelist cross-check |
| `unrenounced_freeze_warning` | SOFT | freeze authority held — same |
| `t22_perm_delegate_hard_avoid` | **HARD** | Token-2022 permanent delegate set — known rug primitive |

Verdict logic:
- `avoid`         → T22 permanent delegate tripped (HARD, no whitelist escape)
- `review_mint`   → mint authority held (needs `dim_address_label` to whitelist)
- `review_freeze` → freeze authority held, mint is renounced
- `safe`          → all renounced, no T22 perm delegate

### Why T22 perm delegate stays hard

Token-2022 permanent delegate lets the delegate transfer/burn ANY holder's
tokens at any time. There is no legitimate use case for an LP-able token
that requires this. Unlike mint authority (legit for stablecoins, LSTs) or
freeze authority (rare but possible for compliance tokens), permanent
delegate has zero legit use for an LP position. Keep this as a hard gate.

### What's needed to resolve `review_mint` → `safe`

`dim_address_label` — a whitelist of known-good authority addresses:
- Circle (USDC), Tether (USDT), Marinade (mSOL), Jupiter (JLP/JSOL), Wormhole
  (W), Jupiter Aggregator, Sanctum (LSTs), Meteora (LP tokens), Kraken
  (kTokens), etc.

That table is Opus's item #3 priority. Once built, the SQL becomes:

```sql
-- Effective verdict, post-whitelist
SELECT
    a.token_mint,
    a.authority_verdict,
    CASE
        WHEN a.t22_perm_delegate_hard_avoid = 1 THEN 'avoid'  -- hard
        WHEN a.authority_verdict = 'review_mint'
             AND l.kind = 'known_good_authority' THEN 'safe'
        WHEN a.authority_verdict = 'review_mint'
             AND l.kind IS NULL                THEN 'review_mint'  -- unknown = stay
        ELSE a.authority_verdict
    END AS effective_verdict
FROM v_token_authority_risk a
LEFT JOIN dim_address_label l
    ON l.address = a.mint_authority;  -- (or freeze_authority)
```

### Decision: do not auto-whitelist by address in this table

Even with a known-good address, an authority holder CAN still act badly
(Circle depeg, Tether freeze, etc). The risk signal stays visible; the
verdict changes from "review" to "safe" but the flag is preserved for
auditing.

### Decision: do not add Metaplex metadata parsing in this iteration

`update_authority` (Metaplex metadata) is set on most token mints. It's a
low-signal flag — devs who care about rug risk are not blocked by metadata
mutability. Add a separate `dim_token_metadata` table later if needed.

### Test fixtures updated

Mock RPC fixtures in `test_cli_mock_rpc.py` now match live Helius mainnet
behavior:
- USDC fixture: both authorities held (was: both None — outdated assumption)
- wSOL fixture: both renounced (was: both held — outdated assumption)

This catches the "test passes but live fails" failure mode.

### 2026-06-06: dim_address_label (Opus item #3) — DONE

### What Opus said

> "dim_address_label: seed ~200 known addresses. Unlocks meaningful
> top10_holder_pct_clean." (Actually his priority #3 was exclusion list;
> we re-purposed it as known-good-authority whitelist, which solves the
> false-positive problem from refinement step above.)

### What we built

1. `sql/dim_address_label.sql` — table with CHECK constraint on `kind`,
   plus `v_effective_authority_verdict` view that resolves `review_mint`
   → `safe` when mint_authority is a known_good_authority.

2. `seed_address_labels.py` — seed with 13 known-good authority addresses
   (Circle, Tether, Marinade, BlazeStake, Orca, Render, etc). All "helius_live"
   addresses were observed via real mainnet fetch on 2026-06-06.

3. `TokenAuthorityStore` extended with:
   - `add_address_label(...)` — idempotent upsert
   - `load_address_labels(entries)` — bulk load
   - `effective_authority_verdict(mint)` — query the post-whitelist view
   - `address_label_count(kind=None)` — sanity check

4. CLI: `--load-seeds` flag auto-loads the seed file before computing
   the effective verdict. Two-tier output: raw (pre-whitelist) + effective
   (post-whitelist) with entity name and confidence.

5. `test_address_label.py` — 13 tests:
   - id, idempotency, bulk load, inactive labels
   - USDC whitelisted → `safe` (the headline test)
   - T22 perm delegate stays `avoid` even with whitelisted authority (HARD gate)
   - unknown authority stays `review_mint`
   - freeze-only whitelist resolves `review_freeze` → `safe`
   - inactive whitelist entries are ignored
   - seed file structure: 32-44 char base58, all 4 required live-observed
     addresses present

### Live regression (10 mints)

| Token | Raw | Effective | Entity |
|-------|-----|-----------|--------|
| USDC | `review_mint` | **`safe`** | Circle |
| USDT | `review_mint` | **`safe`** | Tether |
| mSOL | `review_mint` | **`safe`** | Marinade |
| bSOL | `review_mint` | **`safe`** | BlazeStake |
| ORCA | `review_mint` | **`safe`** | Orca |
| RENDER | `review_mint` | **`safe`** | Render Network |
| wSOL, BONK, JUP, PYTH | `safe` | `safe` | (renounced) |

### Why the whitelist does NOT cover everything

The 13 seed entries cover the most common legitimate Solana token
issuers. To reach the "200 addresses" Opus suggested:
- Add well-known LSTs: Sanctum, Jito (already noted in seed), Lido,
  Coinbase wrapped, Kraken kTokens
- Add DEX LP tokens: Meteora (LP-METE), Orca (more varieties), Raydium
- Add bridge issuers: Wormhole (W, more), Mayan, deBridge
- Add protocol treasuries: Jupiter, Meteora DAO, Drift, Mango, Zeta
- Add stablecoins: PYUSD (Paxos), FDUSD (First Digital), USDe (Ethena)

Each entry should be added with a `source` of either `helius_live` (we
fetched the address) or `official_docs` (we cited the source). The
`verified_at` timestamp is set automatically for both.

## 2026-06-07: Meridian analysis → Build 1, 2, 3

Reading `MERIDIAN_ANALYSIS.md` flipped the project's direction.

### Insight 1 — whitelist is the wrong tail

The 13-entry known-good whitelist (`dim_address_label`) is fine for
the blue-chip head of the distribution, but the long tail of new mints
will never be on it. Whitelisting can never grow fast enough. The
high-value inversion is to **blacklist devs**: a single rug deployer
keeps recycling wallets, so each new blacklist entry catches every
future launch from that address with no per-mint work.

### Insight 2 — `dev_rug_count` is the single strongest predictor

Per Meridian's regression on labelled rug outcomes, the strongest
single predictor of a future rug is whether the mint authority has
already abandoned a previous token. OKX surfaces this as
`dev_rug_count` on `/advanced-info`. We promoted it to a hard-reject
rule (priority 4 in the composite ladder).

### Insight 3 — authority disabled ≠ safe

A mint with both authorities renounced can still rug via concentrated
top-10 holdings (the dev pre-allocated supply before renouncing). The
authority gate is necessary but not sufficient; it has to be combined
with a holder-concentration check (Jupiter `top_holders_pct` or OKX
`top10_pct`). Hence priority 6 in the ladder.

### Build 1 — Jupiter `/v1/assets/search`

One HTTP call returns: `organicScore`, `holderCount`, `mcap`,
`globalFees.sol` (cumulative SOL fees the pools have generated),
`audit.topHoldersPercentage`, `audit.botHoldersPercentage`,
`audit.devMigrations`, `audit.mintAuthorityDisabled`,
`audit.freezeAuthorityDisabled`. All captured by `dim_token_jup`.

### Build 2 — OKX `/advanced-info` + `/risk/new/check` + `/price-info`

Three free endpoints, one row per mint. Provides `risk_level`,
`dev_rug_count` (Meridian's signal #2), `bundle_pct`, `sniper_pct`,
`is_honeypot`, `is_rugpull`, `is_wash`, plus ATH / ATL price data.
137/137 tests pass after Build 2.

The OKX free endpoints work without auth, but only with the exact
request shape: `chainIndex=501` for Solana, `Ok-Access-Client-type`
header set. Without those, the API silently returns the wrong chain.

### Build 3 — `dim_blacklist_dev`

New table for the dev-rugger blocklist; CLI subcommands
`add` / `list` / `check` / `deactivate`. Indexed on `wallet_address`
for O(1) lookup. Soft-delete via `is_active` so we never lose
audit history.

### Live OKX 402 / 404

Live test in this session showed OKX returning 402 / 404 against the
public endpoints. Best guess: the routes have moved or now require
auth. The parser and store work against fixtures; logged for
follow-up — does not block screening since the screener treats OKX
errors as missing signal, not failure.

## 2026-06-07: `screen.py` composite pipeline

149/149 tests pass. Composite verdict is a 10-rule priority ladder
combining all four sources (see README §"Composite verdict logic"
for the table).

### Short-circuit ordering

The dev-blacklist check runs **after** the Helius fetch (because we
need `mint_authority` as the dev-wallet key) but **before** the
Jupiter and OKX fetches. A blacklisted dev returns immediately,
saving two HTTP round-trips per rejected mint. This matters at
batch-screening scale.

### Soft-flag scheme

Three signals trigger `review` rather than `reject`:
mint or freeze authority not renounced, Jupiter `organic_score < 60`,
OKX `risk_level >= 4`. Rationale: these are correlated with rugs but
not deterministic; a blanket reject would over-filter legit tokens.

### Conservative-by-default trade-off

A whitelisted authority (e.g. Circle for USDC) sets
`effective_verdict = safe` in the `v_effective_authority_verdict`
view, but the **composite** verdict from `screen.py` still surfaces
the unrenounced-authority soft flag and returns `review`. This is
intentional: the pipeline-level composite uses `raw_verdict`, not
`effective_verdict`, so a whitelist mistake can't silently pass a
malicious token. The trade-off is noise: USDC / USDT / mSOL all
come back as `review` even though we know they're safe.

## Open questions for future work

- **Auto-populate `dim_blacklist_dev` from OKX `dev_rug_count`** —
  a cron / batch script that ingests OKX results and adds any dev
  with `dev_rug_count >= 2` to the blacklist (with `dev_rug_count = 1`
  staying soft-flag only). Needs a back-fill pass before the rule
  goes live.
- **Should `screen.py` consider `effective_verdict` instead of
  `raw_verdict`?** Currently it considers raw only (see
  "Conservative-by-default" above). Switching would silence the
  USDC / mSOL false-positives at the cost of trusting the whitelist
  for verdict — open question whether that's the right risk posture.
- **Telegram bot** — invert the Meridian pattern (~80 lines):
  watch a channel of newly-launched mints, run each through
  `screen.py`, post the verdict back. Almost all wiring is done; the
  bot is the missing trigger.

