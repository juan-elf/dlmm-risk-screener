# Wallet_Tracking — Solana DLMM Token Risk Screener

A composite token-risk screening tool for Meteora DLMM liquidity providers.
It combines on-chain authority data, holder distribution, and dev-history
signals from four independent sources into a single verdict. Everything
lives in one SQLite database; the only external dependencies are a Solana
RPC endpoint and two free public APIs.

## What it does

- Screens any SPL / Token-2022 mint by address using **Helius RPC**
  (on-chain authority) + **Jupiter datapi** (holders, organic score,
  audit flags) + **OKX OnchainOS** (risk_level, dev_rug_count, bundle %)
  + a local **dev blacklist**.
- Ships a 13-entry **whitelist of known-good issuers** (Circle, Tether,
  Marinade, BlazeStake, Orca, Render, Solana Foundation, …) so legit
  stablecoins / LSTs don't get flagged for non-renounced authority.
- Maintains a **blocklist of known rug deployers** keyed on the mint
  authority wallet — short-circuits screening before any paid API call.
- Tracks your own **Meteora DLMM positions** with open/close P&L (the
  feedback loop: every position becomes a labeled training example).
- One SQLite DB, no external services beyond the RPC and two free APIs.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install solders solana httpx

export HELIUS_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"

.venv/bin/python screen.py \
    --mint EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
    --db screening.db \
    --rpc "$HELIUS_RPC_URL" \
    --load-seeds
```

Or as a Telegram bot (long-poll, single allow-listed chat_id):

```bash
export TELEGRAM_BOT_TOKEN=...          # from @BotFather
export TELEGRAM_ALLOWED_CHAT_ID=...   # your personal chat id
.venv/bin/python telegram_bot.py      # /check <mint> replies with verdict
```

Expected output (truncated):

```
======================================================================
  COMPOSITE VERDICT: REVIEW   (EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v)
  unrenounced_mint_authority; unrenounced_freeze_authority
======================================================================

[ HELIUS / on-chain authority ]
  mint_authority       : BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG
  raw_verdict          : review_mint
  effective_verdict    : safe
  mint_authority_entity: Circle

[ JUPITER ]
  organic_score        : 94
  holder_count         : 2,734,118
  global_fees_sol      : 18,442.3

[ OKX ]
  risk_level           : 1
  dev_rug_count        : 0

[ DEV BLACKLIST ]
  is_blacklisted       : no
```

Run `screen.py --help` for all flags.

## Modules

| File | What it does | Source / store | Signals produced |
|------|--------------|----------------|------------------|
| `screen.py` | Top-level composite screener; 10-rule verdict ladder | orchestrates all 4 below | `composite_verdict`, `hard_reject_reasons`, `soft_flags` |
| `dim_token_authority.py` | Fetch + decode SPL / Token-2022 mint account | Helius RPC → `dim_token_authority` | `mint_authority`, `freeze_authority`, `is_token_2022`, `has_permanent_delegate`, `authority_verdict` |
| `dim_token_jup.py` | One call to Jupiter `/v1/assets/search` | `datapi.jup.ag` → `dim_token_jup` | `organic_score`, `holder_count`, `mcap_usd`, `global_fees_sol`, `top_holders_pct`, `bot_holders_pct`, `dev_migrations`, `mint_disabled`, `freeze_disabled` |
| `dim_token_okx.py` | OKX `/advanced-info` + `/risk/new/check` + `/price-info` | `web3.okx.com` → `dim_token_okx` | `risk_level`, `dev_rug_count`, `bundle_pct`, `sniper_pct`, `top10_pct`, `is_honeypot`, `is_rugpull`, `is_wash`, `tags`, ATH/ATL |
| `dim_blacklist_dev.py` | Dev rugger blocklist; CLI for add/list/check/deactivate | local DB → `dim_blacklist_dev` | `is_blacklisted`, `reason`, `category` |
| `fact_lp_position.py` | Track your own Meteora DLMM positions + P&L | Meteora API → `fact_lp_position`, `fact_lp_event` | open/close events, realized P&L |
| `seed_address_labels.py` | 13-entry seed of known-good authority holders | static file → `dim_address_label` | whitelist used by `v_effective_authority_verdict` |
| `telegram_bot.py` | Inverse-Meridian pattern: Telegram `/check <mint>` → verdict | `screen.py` in-process | bot reply text |
| `collect_authority_seeds.py` | Research script: pull authorities from a curated mint list | Helius → JSON manifest | (offline tooling) |
| `extract_live_authorities.py` | Same, but emits a full JSON dump with no truncation | Helius → JSON manifest | (offline tooling) |

## Schema

7 tables + 7 views, all in one SQLite DB:

**Tables**

| Table | DDL | Purpose |
|-------|-----|---------|
| `dim_token_authority` | `sql/dim_token_authority.sql:9` | On-chain mint authority + T22 extension flags |
| `dim_address_label` | `sql/dim_address_label.sql:12` | Whitelist of known-good issuer addresses |
| `dim_token_jup` | `sql/dim_token_jup.sql:19` | Jupiter audit + holder + fee snapshot |
| `dim_token_okx` | `sql/dim_token_okx.sql:22` | OKX risk_level, dev_rug_count, bundle %, honeypot flags |
| `dim_blacklist_dev` | `sql/dim_blacklist_dev.sql:19` | Local blocklist of rug deployer wallets |
| `fact_lp_position` | `sql/fact_lp_position.sql:14` | Open Meteora DLMM positions |
| `fact_lp_event` | `sql/fact_lp_position.sql:65` | Event log against each position |

**Views**

| View | DDL | Purpose |
|------|-----|---------|
| `v_token_authority_risk` | `sql/dim_token_authority.sql:61` | Raw (pre-whitelist) authority verdict |
| `v_effective_authority_verdict` | `sql/dim_address_label.sql:48` | Post-whitelist verdict; resolves `review_mint` → `safe` when issuer is known-good |
| `v_token_jup_risk` | `sql/dim_token_jup.sql:71` | Per-mint Jupiter risk summary |
| `v_token_okx_risk` | `sql/dim_token_okx.sql:82` | Per-mint OKX risk summary |
| `v_dev_blacklist_active` | `sql/dim_blacklist_dev.sql:42` | Active rows only (filters out deactivated entries) |
| `v_lp_outcome_summary` | `sql/fact_lp_position.sql:90` | Position-level realized outcome |
| `v_risk_score_validation` | `sql/fact_lp_position.sql:122` | Cross-check screener verdict vs realised LP outcome |

## Composite verdict logic

`screen.py` evaluates a 10-rule priority ladder. Hard rules → `reject`,
soft flags → `review`, no signal → `caution` (insufficient data) → `allow`.
Whichever fires first wins.

| # | Rule | Source signal | Outcome |
|---|------|---------------|---------|
| 1 | Dev wallet is on local blocklist | `dim_blacklist_dev` | `reject: dev_blacklisted` (short-circuits — skips Jupiter & OKX) |
| 2 | Token-2022 with permanent delegate | Helius | `reject: t22_permanent_delegate` |
| 3 | OKX flags honeypot OR rugpull OR wash trading | OKX | `reject: honeypot_or_rugpull_or_wash` |
| 4 | OKX `dev_rug_count >= 1` | OKX | `reject: dev_has_prior_rugs` |
| 5 | Jupiter `global_fees_sol < 30` | Jupiter | `reject: low_trading_fees` |
| 6 | Jupiter `top_holders_pct > 60` OR OKX `top10_pct > 60` | Jupiter / OKX | `reject: holder_concentration` |
| 7 | OKX `bundle_pct > 30` | OKX | `reject: bundle_detected` |
| 8 | Mint or freeze authority not renounced | Helius | soft → `review: unrenounced_*` |
| 9 | Jupiter `organic_score < 60` | Jupiter | soft → `review: low_organic_score` |
| 10 | OKX `risk_level >= 4` | OKX | soft → `review: okx_high_risk_level` |

Rule 4 (`dev_rug_count`) is, per `MERIDIAN_ANALYSIS.md`, the single most
underrated rug-prediction signal: a developer wallet that has already
launched and abandoned a token is overwhelmingly likely to do so again,
yet authority-only screens miss it entirely. See `MERIDIAN_ANALYSIS.md`
for the broader rationale behind the dev-blacklist inversion (insights
#1 and #2) and why authority-disabled ≠ safe without a holder gate
(insight #3).

## Tests

149 tests across 14 files. All offline by default; the few live
integration tests are gated on `HELIUS_RPC_URL`.

```bash
.venv/bin/python -m unittest discover -s . -p "test_*.py" -v
```

Live integration tests require `HELIUS_RPC_URL` to be set. They issue
real mainnet RPC calls and can be slow; offline mock tests cover the
same surface area.

## Limitations

- **OKX endpoints are flaky.** Live runs against the free `/advanced-info`,
  `/risk/new/check`, and `/price-info` endpoints currently return 402 /
  404 — they likely moved or now require auth. The parser still works
  against captured fixtures; treat OKX as best-effort until rerouted.
- Live integration tests require network access and a Helius API key.
- No Telegram bot yet (planned — see Roadmap).
- The dev blacklist is **empty by default**. It has to be seeded manually
  via the `dim_blacklist_dev` CLI, or auto-populated from OKX
  `dev_rug_count` (not yet implemented).

## Roadmap

Suggested by the Opus review (`REVIEW_claude_opus_full.md`) but not yet
built:

- `wallet_profile_snapshot` — point-in-time wallet labels (kills the
  lookahead bias when back-testing).
- `fact_pool_liquidity_snapshot` — periodic TVL snapshot per pool;
  enables collapse detection.
- `fact_price_ohlcv` — needed before `bounce_rate` can scale.
- **Telegram bot** — the inverse-Meridian pattern: route every prospective
  mint through `screen.py` and post the verdict to a private channel.
  ~80 lines of Python on top of the existing screener.

## References

- [`MERIDIAN_ANALYSIS.md`](MERIDIAN_ANALYSIS.md) — full rationale for the
  blacklist inversion, `dev_rug_count` weighting, and post-authority
  holder gating.
- [`REVIEW_claude_opus.md`](REVIEW_claude_opus.md) /
  [`REVIEW_claude_opus_full.md`](REVIEW_claude_opus_full.md) — original
  architectural review and priority list.
- [`dlmm_risk_schema.md`](dlmm_risk_schema.md) — original bronze/silver/gold
  medallion spec the project grew out of.
- [`DESIGN_NOTES.md`](DESIGN_NOTES.md) — running log of decisions and
  trade-offs taken during the build.
