"""
fact_lp_position — fetch & track Meteora DLMM LP positions.

This is Opus review item #2 — the feedback loop. Every position you open
becomes a labeled training example so the system can answer:
  "Did the risk_score at deposit time correlate with the actual P&L outcome?"

Architecture:
  - fact_lp_position: one row per position (current state)
  - fact_lp_event:    append-only event log (open/add/remove/claim/close)
  - v_lp_outcome_summary: per-position P&L stats
  - v_risk_score_validation: aggregate win/loss by verdict

Sources:
  - Meteora DLMM API:   https://dlmm-api.meteora.ag
      GET /position/{owner}         -> all positions for a wallet
      GET /position/{pubkey}        -> single position by position NFT
      GET /pair/{address}           -> pool info (token_x/y, bin_step, ...)
  - Helius RPC:        for tx-level event decoding (out of scope here)

References:
  - Opus review item #2: /mnt/e/Wallet_Tracking/REVIEW_claude_opus_full.md
  - Meteora DLMM API:    https://docs.meteora.ag/
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

METEORA_API = "https://dlmm-api.meteora.ag"

# --- Data classes ------------------------------------------------------------

@dataclass
class LPPosition:
    position_id: str
    owner_wallet: str
    pool_address: str
    token_x_mint: Optional[str] = None
    token_y_mint: Optional[str] = None
    bin_lower: Optional[int] = None
    bin_upper: Optional[int] = None
    bin_step: Optional[int] = None
    status: str = "open"  # 'open' | 'closed'
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    initial_x_amount: Optional[float] = None
    initial_y_amount: Optional[float] = None
    initial_usd: Optional[float] = None
    current_x_amount: Optional[float] = None
    current_y_amount: Optional[float] = None
    current_usd: Optional[float] = None
    fees_x_claimed: float = 0.0
    fees_y_claimed: float = 0.0
    fees_usd_claimed: float = 0.0
    hold_value_usd: Optional[float] = None
    il_usd: Optional[float] = None
    il_pct: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    risk_score_at_open: Optional[float] = None
    verdict_at_open: Optional[str] = None
    last_refreshed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_event_at: Optional[str] = None
    source: str = "meteora_api"
    notes: Optional[str] = None

    def to_row(self) -> dict:
        return asdict(self)


# --- Meteora API client -------------------------------------------------------

def _normalize_position(raw: dict, owner: str) -> LPPosition:
    """Map Meteora API JSON shape to our LPPosition dataclass.

    Meteora API shape (as of 2026-06-06 — verify against current docs):
      {
        "position_address": "...",
        "pool_address":     "...",
        "owner":            "...",
        "lower_bin_id":     <int>,
        "upper_bin_id":     <int>,
        "created_at":       <unix seconds>,
        "last_updated_at":  <unix seconds>,
        "total_deposit_x":  "<amount>",
        "total_deposit_y":  "<amount>",
        "total_deposit_value_usd": "<usd>",
        "total_withdraw_x": "<amount>",
        "total_withdraw_y": "<amount>",
        "total_withdraw_value_usd": "<usd>",
        "fees_claimed_x":   "<amount>",
        "fees_claimed_y":   "<amount>",
        "fees_claimed_value_usd": "<usd>",
        ...
      }
    """
    def _ts(epoch) -> Optional[str]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None

    def _num(x) -> Optional[float]:
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    pos_addr = raw.get("position_address") or raw.get("position_pubkey") or ""
    pool     = raw.get("pool_address") or raw.get("lb_pair") or ""
    status   = "closed" if raw.get("closed") or raw.get("total_withdraw_value_usd") else "open"

    initial_usd = _num(raw.get("total_deposit_value_usd")) or 0.0
    fees_usd    = _num(raw.get("fees_claimed_value_usd"))    or 0.0
    withdraw_usd= _num(raw.get("total_withdraw_value_usd"))  or 0.0
    current_usd = (initial_usd - withdraw_usd) if status == "open" else None
    # P&L = (current_value + fees) - initial_value
    pnl_usd = ((current_usd or 0.0) + fees_usd) - initial_usd if status == "open" else None

    return LPPosition(
        position_id      = pos_addr,
        owner_wallet     = owner,
        pool_address     = pool,
        token_x_mint     = raw.get("token_x_mint"),
        token_y_mint     = raw.get("token_y_mint"),
        bin_lower        = raw.get("lower_bin_id"),
        bin_upper        = raw.get("upper_bin_id"),
        bin_step         = raw.get("bin_step"),
        status           = status,
        opened_at        = _ts(raw.get("created_at")),
        closed_at        = _ts(raw.get("closed_at")),
        initial_x_amount = _num(raw.get("total_deposit_x")),
        initial_y_amount = _num(raw.get("total_deposit_y")),
        initial_usd      = initial_usd,
        current_x_amount = _num(raw.get("current_x_amount")),
        current_y_amount = _num(raw.get("current_y_amount")),
        current_usd      = current_usd,
        fees_x_claimed   = _num(raw.get("fees_claimed_x")) or 0.0,
        fees_y_claimed   = _num(raw.get("fees_claimed_y")) or 0.0,
        fees_usd_claimed = fees_usd,
        last_event_at    = _ts(raw.get("last_updated_at")),
        source           = "meteora_api",
    )


def fetch_positions(owner_wallet: str, *, timeout: float = 30.0,
                    api_base: str = METEORA_API) -> list[LPPosition]:
    """Fetch all LP positions for `owner_wallet` from Meteora DLMM API.

    Returns empty list on 404 (no positions) or any other 'no data' response.
    Returns the raw error message in log on actual failures.
    """
    url = f"{api_base}/position/{owner_wallet}"
    try:
        r = httpx.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        log.error("HTTP error fetching positions for %s: %s", owner_wallet, e)
        return []
    if r.status_code == 404:
        log.info("No positions found for %s (404)", owner_wallet)
        return []
    if r.status_code != 200:
        log.error("Meteora API %s for %s: %s", r.status_code, owner_wallet, r.text[:200])
        return []
    try:
        data = r.json()
    except ValueError as e:
        log.error("Invalid JSON from Meteora for %s: %s", owner_wallet, e)
        return []
    # Meteora may return a list directly, or {"data": [...]} — be defensive.
    if isinstance(data, dict):
        items = data.get("data") or data.get("positions") or data.get("results") or []
    else:
        items = data
    if not isinstance(items, list):
        log.warning("Unexpected Meteora response shape for %s: %s", owner_wallet, type(items))
        return []
    return [_normalize_position(item, owner_wallet) for item in items]


# --- SQLite store -------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "sql" / "fact_lp_position.sql"


class LPPositionStore:
    """SQLite store for LP positions and events. Idempotent upserts."""

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

    def upsert(self, pos: LPPosition) -> None:
        row = pos.to_row()
        self._conn.execute(
            """
            INSERT INTO fact_lp_position (
                position_id, owner_wallet, pool_address,
                token_x_mint, token_y_mint, bin_lower, bin_upper, bin_step,
                status, opened_at, closed_at,
                initial_x_amount, initial_y_amount, initial_usd,
                current_x_amount, current_y_amount, current_usd,
                fees_x_claimed, fees_y_claimed, fees_usd_claimed,
                hold_value_usd, il_usd, il_pct, pnl_usd, pnl_pct,
                risk_score_at_open, verdict_at_open,
                last_refreshed_at, last_event_at, source, notes
            ) VALUES (
                :position_id, :owner_wallet, :pool_address,
                :token_x_mint, :token_y_mint, :bin_lower, :bin_upper, :bin_step,
                :status, :opened_at, :closed_at,
                :initial_x_amount, :initial_y_amount, :initial_usd,
                :current_x_amount, :current_y_amount, :current_usd,
                :fees_x_claimed, :fees_y_claimed, :fees_usd_claimed,
                :hold_value_usd, :il_usd, :il_pct, :pnl_usd, :pnl_pct,
                :risk_score_at_open, :verdict_at_open,
                :last_refreshed_at, :last_event_at, :source, :notes
            )
            ON CONFLICT(position_id) DO UPDATE SET
                pool_address        = excluded.pool_address,
                token_x_mint        = excluded.token_x_mint,
                token_y_mint        = excluded.token_y_mint,
                bin_lower           = excluded.bin_lower,
                bin_upper           = excluded.bin_upper,
                bin_step            = excluded.bin_step,
                status              = excluded.status,
                opened_at           = excluded.opened_at,
                closed_at           = excluded.closed_at,
                initial_x_amount    = excluded.initial_x_amount,
                initial_y_amount    = excluded.initial_y_amount,
                initial_usd         = excluded.initial_usd,
                current_x_amount    = excluded.current_x_amount,
                current_y_amount    = excluded.current_y_amount,
                current_usd         = excluded.current_usd,
                fees_x_claimed      = excluded.fees_x_claimed,
                fees_y_claimed      = excluded.fees_y_claimed,
                fees_usd_claimed    = excluded.fees_usd_claimed,
                hold_value_usd      = excluded.hold_value_usd,
                il_usd              = excluded.il_usd,
                il_pct              = excluded.il_pct,
                pnl_usd             = excluded.pnl_usd,
                pnl_pct             = excluded.pnl_pct,
                risk_score_at_open  = excluded.risk_score_at_open,
                verdict_at_open     = excluded.verdict_at_open,
                last_refreshed_at   = excluded.last_refreshed_at,
                last_event_at       = excluded.last_event_at,
                source              = excluded.source,
                notes               = excluded.notes
            """,
            row,
        )
        self._conn.commit()

    def get(self, position_id: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM fact_lp_position WHERE position_id = ?", (position_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_by_owner(self, owner_wallet: str) -> list[dict]:
        cur = self._conn.execute(
            """SELECT * FROM fact_lp_position
               WHERE owner_wallet = ?
               ORDER BY opened_at DESC NULLS LAST""",
            (owner_wallet,),
        )
        return [dict(r) for r in cur]

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM fact_lp_position")
        return cur.fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


# --- CLI ----------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Fetch and store Meteora DLMM LP positions for a wallet"
    )
    p.add_argument("--wallet", required=True, help="Solana wallet address (base58)")
    p.add_argument("--db", default="wallet_tracking.db", help="SQLite DB path")
    p.add_argument("--api", default=METEORA_API,
                   help=f"Meteora API base URL (default: {METEORA_API})")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    positions = fetch_positions(args.wallet, api_base=args.api)
    if not positions:
        print(f"No positions found for wallet {args.wallet}")
        return 0

    store = LPPositionStore(args.db)
    for p in positions:
        store.upsert(p)
        print(f"  Upserted position {p.position_id[:12]}... "
              f"pool={p.pool_address[:12]}... status={p.status} "
              f"initial_usd=${p.initial_usd or 0:.2f}")

    print(f"\nTotal positions stored: {len(positions)}")
    print(f"  open:   {sum(1 for p in positions if p.status == 'open')}")
    print(f"  closed: {sum(1 for p in positions if p.status == 'closed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
