"""
dim_token_jup — Jupiter datapi /v1/assets/search enrichment.

One HTTP call to Jupiter returns 10+ risk signals in a single shot:
  - audit.mintAuthorityDisabled / freezeAuthorityDisabled / topHoldersPercentage
    / botHoldersPercentage / devMigrations / totalHolders / riskyHolders
  - organicScore + organicScoreLabel
  - holderCount, mcap, launchpad
  - fees (cumulative priority+jito SOL paid by traders) — KEY new signal

This module is an ENRICHMENT LAYER for dim_token_authority, not a replacement.
On-chain mint decoding (Token-2022 permanent delegate in particular) still
requires the Helius getAccountInfo path.

Storage: SQLite (Universal SQL Agent stack).

Usage:
    from dim_token_jup import TokenJupStore, fetch_jupiter
    store = TokenJupStore("path/to/wallet_tracking.db")
    rec = fetch_jupiter("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    store.upsert(rec)

CLI:
    python dim_token_jup.py --mint <MINT> [--db PATH] [--api URL]

References:
    - Meridian config thresholds: /mnt/e/meridian/config.js:78 (minTokenFeesSol),
      /mnt/e/meridian/discord-listener/pre-checks.js:130-159 (fees gate)
    - Jupiter datapi: /mnt/e/meridian/tools/token.js:22-58
    - Analysis: /mnt/e/Wallet_Tracking/MERIDIAN_ANALYSIS.md sections 2 & 3
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# --- Jupiter API --------------------------------------------------------------

JUPITER_API_BASE = "https://datapi.jup.ag"
JUPITER_SEARCH_PATH = "/v1/assets/search"

# --- Data class --------------------------------------------------------------

@dataclass
class TokenJupRecord:
    """Mirrors dim_token_jup columns. Most fields nullable for unknown tokens."""
    token_mint: str
    jup_id: Optional[str] = None
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: Optional[int] = None
    organic_score: Optional[float] = None
    organic_score_label: Optional[str] = None
    holder_count: Optional[int] = None
    mcap_usd: Optional[float] = None
    launchpad: Optional[str] = None
    global_fees_sol: Optional[float] = None
    audit_mint_disabled: Optional[bool] = None
    audit_freeze_disabled: Optional[bool] = None
    audit_top_holders_pct: Optional[float] = None
    audit_bot_holders_pct: Optional[float] = None
    audit_dev_migrations: Optional[int] = None
    audit_total_holders: Optional[int] = None
    audit_risky_holders: Optional[int] = None
    raw_json: Optional[str] = None
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_seen_chain: bool = True
    error: Optional[str] = None
    source: str = "jupiter_datapi"

    def to_row(self) -> dict:
        return asdict(self)


# --- Parsers -----------------------------------------------------------------

def _coerce_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def _normalize_search_response(raw_body, target_mint: str) -> Optional[dict]:
    """Pick the asset entry whose id (or tokenAddress) matches `target_mint`.

    Jupiter's actual response shape isn't fully documented and may vary:
      - bare list:          [ {...}, {...} ]
      - data envelope:      {"data":    [...]}
      - results envelope:   {"results": [...]}
      - assets envelope:    {"assets":  [...]}
    Be defensive about all of them.

    Returns the matching single asset dict, or None if the mint isn't in the
    returned results.
    """
    if raw_body is None:
        return None
    if isinstance(raw_body, list):
        items = raw_body
    elif isinstance(raw_body, dict):
        items = (raw_body.get("data") or raw_body.get("results")
                 or raw_body.get("assets") or [])
        # If body is itself a single asset dict that already matches, return it.
        if not items and (raw_body.get("id") == target_mint
                          or raw_body.get("tokenAddress") == target_mint):
            return raw_body
    else:
        return None
    if not isinstance(items, list):
        return None
    for asset in items:
        if not isinstance(asset, dict):
            continue
        if asset.get("id") == target_mint or asset.get("tokenAddress") == target_mint:
            return asset
    return None


def _parse_jupiter_response(mint: str, body) -> TokenJupRecord:
    """Map a raw Jupiter response body to a TokenJupRecord.

    Missing fields -> None. If the mint isn't found in the results, return a
    record with error='not_found' and last_seen_chain=False.

    The full body is stored as raw_json on success so future fields can be
    backfilled without re-fetching.
    """
    asset = _normalize_search_response(body, mint)
    if asset is None:
        return TokenJupRecord(
            token_mint=mint, error="not_found", last_seen_chain=False,
            raw_json=_safe_json_dumps(body),
        )

    audit = asset.get("audit") if isinstance(asset.get("audit"), dict) else {}

    return TokenJupRecord(
        token_mint=mint,
        jup_id=asset.get("id") or asset.get("tokenAddress"),
        symbol=asset.get("symbol"),
        name=asset.get("name"),
        decimals=_coerce_int(asset.get("decimals")),
        organic_score=_coerce_float(asset.get("organicScore")),
        organic_score_label=asset.get("organicScoreLabel"),
        holder_count=_coerce_int(asset.get("holderCount")),
        mcap_usd=_coerce_float(asset.get("mcap") or asset.get("marketCap")),
        launchpad=asset.get("launchpad"),
        # Jupiter exposes cumulative SOL fees as `fees` on the asset; some
        # response variants nest it under `audit.fees`. Check both.
        global_fees_sol=_coerce_float(
            asset.get("fees") if asset.get("fees") is not None
            else audit.get("fees")
        ),
        audit_mint_disabled=_coerce_bool(audit.get("mintAuthorityDisabled")),
        audit_freeze_disabled=_coerce_bool(audit.get("freezeAuthorityDisabled")),
        audit_top_holders_pct=_coerce_float(audit.get("topHoldersPercentage")),
        audit_bot_holders_pct=_coerce_float(audit.get("botHoldersPercentage")),
        audit_dev_migrations=_coerce_int(audit.get("devMigrations")),
        audit_total_holders=_coerce_int(audit.get("totalHolders")),
        audit_risky_holders=_coerce_int(audit.get("riskyHolders")),
        raw_json=_safe_json_dumps(asset),
        last_seen_chain=True,
        error=None,
    )


def _safe_json_dumps(obj) -> Optional[str]:
    """Best-effort JSON serialization. Returns None on failure rather than raise
    (we never want raw_json persistence to break a fetch)."""
    if obj is None:
        return None
    try:
        return json.dumps(obj, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        log.warning("raw_json serialize failed: %s", e)
        return None


# --- Public API --------------------------------------------------------------

def fetch_jupiter(
    token_mint: str,
    *,
    timeout: float = 5.0,
    api_base: str = JUPITER_API_BASE,
) -> TokenJupRecord:
    """GET {api_base}{JUPITER_SEARCH_PATH}?query={mint} and parse the result.

    Per spec: 5-second HTTP timeout (Meridian also keeps this short — Jupiter
    is fast and we'd rather flag a slow fetch than block screening). The
    `timeout` arg overrides the default for tests.

    Returns a TokenJupRecord in all cases. Errors are stored in `.error` and
    `last_seen_chain=False`, never raised:
      - http:<status>        non-200 HTTP response
      - http_error:<type>    connect / read / DNS / etc.
      - invalid_json         body wasn't JSON-parseable
      - not_found            mint not in returned results
    """
    url = f"{api_base.rstrip('/')}{JUPITER_SEARCH_PATH}"
    try:
        r = httpx.get(
            url,
            params={"query": token_mint},
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        return TokenJupRecord(
            token_mint=token_mint,
            error=f"http_error:{type(e).__name__}",
            last_seen_chain=False,
        )

    if r.status_code != 200:
        return TokenJupRecord(
            token_mint=token_mint,
            error=f"http:{r.status_code}",
            last_seen_chain=False,
        )

    try:
        body = r.json()
    except ValueError:
        return TokenJupRecord(
            token_mint=token_mint,
            error="invalid_json",
            last_seen_chain=False,
        )

    return _parse_jupiter_response(token_mint, body)


# --- SQLite store ------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "sql" / "dim_token_jup.sql"


class TokenJupStore:
    """SQLite-backed store for dim_token_jup. Single-writer; idempotent upserts.

    Kept as a SEPARATE class from TokenAuthorityStore (per spec) to minimize
    blast radius. The two can share a DB file safely — they don't touch each
    other's tables.
    """

    def __init__(self, db_path: str | os.PathLike):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    def upsert(self, rec: TokenJupRecord) -> None:
        row = rec.to_row()
        # `source` is on the dataclass for parity with TokenAuthority but is
        # implicit in this table (only Jupiter feeds it), so don't persist it.
        row.pop("source", None)
        self._conn.execute(
            """
            INSERT INTO dim_token_jup (
                token_mint, jup_id, symbol, name, decimals,
                organic_score, organic_score_label, holder_count, mcap_usd, launchpad,
                global_fees_sol,
                audit_mint_disabled, audit_freeze_disabled,
                audit_top_holders_pct, audit_bot_holders_pct,
                audit_dev_migrations, audit_total_holders, audit_risky_holders,
                raw_json, fetched_at, last_seen_chain, error
            ) VALUES (
                :token_mint, :jup_id, :symbol, :name, :decimals,
                :organic_score, :organic_score_label, :holder_count, :mcap_usd, :launchpad,
                :global_fees_sol,
                :audit_mint_disabled, :audit_freeze_disabled,
                :audit_top_holders_pct, :audit_bot_holders_pct,
                :audit_dev_migrations, :audit_total_holders, :audit_risky_holders,
                :raw_json, :fetched_at, :last_seen_chain, :error
            )
            ON CONFLICT(token_mint) DO UPDATE SET
                jup_id                = excluded.jup_id,
                symbol                = excluded.symbol,
                name                  = excluded.name,
                decimals              = excluded.decimals,
                organic_score         = excluded.organic_score,
                organic_score_label   = excluded.organic_score_label,
                holder_count          = excluded.holder_count,
                mcap_usd              = excluded.mcap_usd,
                launchpad             = excluded.launchpad,
                global_fees_sol       = excluded.global_fees_sol,
                audit_mint_disabled   = excluded.audit_mint_disabled,
                audit_freeze_disabled = excluded.audit_freeze_disabled,
                audit_top_holders_pct = excluded.audit_top_holders_pct,
                audit_bot_holders_pct = excluded.audit_bot_holders_pct,
                audit_dev_migrations  = excluded.audit_dev_migrations,
                audit_total_holders   = excluded.audit_total_holders,
                audit_risky_holders   = excluded.audit_risky_holders,
                raw_json              = excluded.raw_json,
                fetched_at            = excluded.fetched_at,
                last_seen_chain       = excluded.last_seen_chain,
                error                 = excluded.error
            """,
            row,
        )
        self._conn.commit()

    def get(self, token_mint: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM dim_token_jup WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def jup_risk(self, token_mint: str) -> Optional[dict]:
        """Query v_token_jup_risk for the per-mint screening row."""
        cur = self._conn.execute(
            "SELECT * FROM v_token_jup_risk WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM dim_token_jup")
        return cur.fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


# --- CLI ---------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Fetch Jupiter datapi enrichment for a Solana token mint"
    )
    p.add_argument("--mint", required=True, help="Token mint address (base58)")
    p.add_argument("--db", default="wallet_tracking.db", help="SQLite DB path")
    p.add_argument("--api", default=os.environ.get("JUPITER_API_URL", JUPITER_API_BASE),
                   help=f"Jupiter API base URL (default: {JUPITER_API_BASE})")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="HTTP timeout seconds (default: 5.0)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rec = fetch_jupiter(args.mint, timeout=args.timeout, api_base=args.api)
    print("--- fetched ---")
    for k, v in rec.to_row().items():
        # raw_json can be huge — truncate for terminal readability
        if k == "raw_json" and isinstance(v, str) and len(v) > 200:
            v = v[:200] + f"... ({len(v)} chars total)"
        print(f"  {k}: {v}")

    store = TokenJupStore(args.db)
    store.upsert(rec)
    print(f"\nUpserted into {args.db}")

    risk = store.jup_risk(args.mint)
    if risk:
        flags = []
        if risk["gate_fees_too_low"]:
            flags.append("fees_too_low")
        if risk["gate_top_holders"]:
            flags.append("top_holders_too_concentrated")
        if risk["gate_bot_holders"]:
            flags.append("too_many_bot_holders")
        if risk["gate_organic_score"]:
            flags.append("low_organic_score")
        if risk["gate_no_mint_disable"]:
            flags.append("mint_authority_not_disabled")
        flag_str = ",".join(flags) if flags else "(none)"
        print(
            f"\n--- v_token_jup_risk ---\n"
            f"  symbol: {risk['symbol']}\n"
            f"  organic_score: {risk['organic_score']}\n"
            f"  holder_count: {risk['holder_count']}\n"
            f"  mcap_usd: {risk['mcap_usd']}\n"
            f"  global_fees_sol: {risk['global_fees_sol']}\n"
            f"  top_holders_pct: {risk['audit_top_holders_pct']}\n"
            f"  bot_holders_pct: {risk['audit_bot_holders_pct']}\n"
            f"  dev_migrations: {risk['audit_dev_migrations']}\n"
            f"  launchpad: {risk['launchpad']}\n"
            f"  flags: {flag_str}\n"
            f"  is_rugged_or_unknown: {bool(risk['is_rugged_or_unknown'])}"
        )
    else:
        print(f"\n(no risk row — fetch error: {rec.error})")

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
