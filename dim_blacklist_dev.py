"""
dim_blacklist_dev — developer (mint authority) blacklist for Solana token screening.

Per MERIDIAN_ANALYSIS insights #1 and #2: the 13-entry known-good whitelist
in dim_address_label is the wrong direction for new tokens. The long tail of
new mints is dominated by unknown deployers, and a small dev BLOCKLIST catches
more rugs than a large whitelist — because `dev_rug_count >= 1` (sourced from
dim_token_okx) is the single strongest single-shot rug predictor we have.

Screening flow:
  1. fetch authority -> dev_wallet = mint_authority
  2. is_blacklisted(dev_wallet) -> if True, REJECT and stop
  3. (otherwise) fetch enrichments

Storage: SQLite (Universal SQL Agent stack).

Usage:
    from dim_blacklist_dev import DevBlacklistEntry, DevBlacklistStore
    store = DevBlacklistStore("path/to/wallet_tracking.db")
    store.add_entry(DevBlacklistEntry(
        dev_wallet="...",
        reason="serial_rugger",
        evidence_mint="...",
        evidence_source="okx_advanced_info",
        dev_rug_count_at_time=3,
    ))
    if store.is_blacklisted(dev_wallet):
        ...

CLI:
    python dim_blacklist_dev.py add --wallet <W> --reason <R> [--evidence-mint <M>] [--notes <N>]
    python dim_blacklist_dev.py list
    python dim_blacklist_dev.py check --wallet <W>
    python dim_blacklist_dev.py deactivate --wallet <W>
"""
from __future__ import annotations

import logging
import os
import sqlite3
import string
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

VALID_REASONS = ("rug_pull", "serial_rugger", "scam", "manual")
VALID_EVIDENCE_SOURCES = ("okx_advanced_info", "manual", "auto_from_screening")

# Solana base58 pubkeys are 32-44 chars. Base58 alphabet excludes 0, O, I, l.
_BASE58_ALPHABET = set(string.ascii_letters + string.digits) - {"0", "O", "I", "l"}


def _is_valid_base58_wallet(wallet: str) -> bool:
    if not isinstance(wallet, str):
        return False
    n = len(wallet)
    if n < 32 or n > 44:
        return False
    return all(c in _BASE58_ALPHABET for c in wallet)


# --- Data class --------------------------------------------------------------

@dataclass
class DevBlacklistEntry:
    """One row in dim_blacklist_dev. Mirrors the columns 1:1."""
    dev_wallet: str
    reason: str
    evidence_mint: Optional[str] = None
    evidence_source: Optional[str] = None
    dev_rug_count_at_time: Optional[int] = None
    first_token_seen: Optional[str] = None
    last_token_seen: Optional[str] = None
    notes: Optional[str] = None
    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    added_by: str = "auto"
    active: bool = True

    def to_row(self) -> dict:
        d = asdict(self)
        d["active"] = 1 if d["active"] else 0
        return d


# --- SQLite store ------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "sql" / "dim_blacklist_dev.sql"


class DevBlacklistStore:
    """SQLite-backed store for dim_blacklist_dev. Single-writer; idempotent
    upserts. Kept as a SEPARATE class — safe to share a DB file with
    TokenAuthorityStore / TokenOkxStore / TokenJupStore.
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

    def add_entry(self, entry: DevBlacklistEntry) -> None:
        """Idempotent upsert: insert a new blacklist entry, or overwrite the
        existing row for this dev_wallet."""
        row = entry.to_row()
        self._conn.execute(
            """
            INSERT INTO dim_blacklist_dev (
                dev_wallet, reason, evidence_mint, evidence_source,
                dev_rug_count_at_time, first_token_seen, last_token_seen,
                notes, added_at, added_by, active
            ) VALUES (
                :dev_wallet, :reason, :evidence_mint, :evidence_source,
                :dev_rug_count_at_time, :first_token_seen, :last_token_seen,
                :notes, :added_at, :added_by, :active
            )
            ON CONFLICT(dev_wallet) DO UPDATE SET
                reason                = excluded.reason,
                evidence_mint         = excluded.evidence_mint,
                evidence_source       = excluded.evidence_source,
                dev_rug_count_at_time = excluded.dev_rug_count_at_time,
                first_token_seen      = excluded.first_token_seen,
                last_token_seen       = excluded.last_token_seen,
                notes                 = excluded.notes,
                added_at              = excluded.added_at,
                added_by              = excluded.added_by,
                active                = excluded.active
            """,
            row,
        )
        self._conn.commit()

    def is_blacklisted(self, dev_wallet: str) -> bool:
        """The hot path: True iff dev_wallet appears in v_dev_blacklist_active.

        Uses the view (active=1 only), so deactivated entries don't fire.
        """
        cur = self._conn.execute(
            "SELECT 1 FROM v_dev_blacklist_active WHERE dev_wallet = ? LIMIT 1",
            (dev_wallet,),
        )
        return cur.fetchone() is not None

    def get(self, dev_wallet: str) -> Optional[dict]:
        """Full row regardless of active state. Returns None if unknown."""
        cur = self._conn.execute(
            "SELECT * FROM dim_blacklist_dev WHERE dev_wallet = ?", (dev_wallet,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_active(self) -> list[dict]:
        """All active entries, newest first."""
        cur = self._conn.execute(
            "SELECT * FROM v_dev_blacklist_active ORDER BY added_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def deactivate(self, dev_wallet: str) -> None:
        """Set active=0 for this wallet. No-op if the wallet is unknown."""
        self._conn.execute(
            "UPDATE dim_blacklist_dev SET active = 0 WHERE dev_wallet = ?",
            (dev_wallet,),
        )
        self._conn.commit()

    def count_active(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM v_dev_blacklist_active")
        return cur.fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


# --- CLI ---------------------------------------------------------------------

def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Manage dim_blacklist_dev (developer/mint-authority blacklist)"
    )
    p.add_argument("--db", default="wallet_tracking.db", help="SQLite DB path")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    # add
    p_add = sub.add_parser("add", help="Add (or overwrite) a blacklist entry")
    p_add.add_argument("--wallet", required=True, help="dev wallet (base58, 32-44 chars)")
    p_add.add_argument("--reason", required=True, choices=VALID_REASONS)
    p_add.add_argument("--evidence-mint", default=None,
                       help="mint address where the bad behavior was observed")
    p_add.add_argument("--evidence-source", default="manual",
                       choices=VALID_EVIDENCE_SOURCES)
    p_add.add_argument("--dev-rug-count", type=int, default=None,
                       help="value of dev_rug_count when the entry was added")
    p_add.add_argument("--notes", default=None)
    p_add.add_argument("--added-by", default="auto",
                       help="who added this entry ('auto' or a user handle)")

    # list
    sub.add_parser("list", help="List all active blacklist entries")

    # check
    p_check = sub.add_parser("check", help="Check whether a wallet is currently blacklisted")
    p_check.add_argument("--wallet", required=True)

    # deactivate
    p_deact = sub.add_parser("deactivate", help="Mark a wallet as inactive (soft-remove)")
    p_deact.add_argument("--wallet", required=True)

    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate wallet format for any subcommand that takes one
    if args.cmd in ("add", "check", "deactivate"):
        if not _is_valid_base58_wallet(args.wallet):
            print(f"ERROR: --wallet must be 32-44 base58 chars (got {len(args.wallet)} chars)")
            return 2

    store = DevBlacklistStore(args.db)
    try:
        if args.cmd == "add":
            entry = DevBlacklistEntry(
                dev_wallet=args.wallet,
                reason=args.reason,
                evidence_mint=args.evidence_mint,
                evidence_source=args.evidence_source,
                dev_rug_count_at_time=args.dev_rug_count,
                notes=args.notes,
                added_by=args.added_by,
            )
            store.add_entry(entry)
            print(f"Added: {args.wallet} reason={args.reason} active=True")
            print(f"active_count: {store.count_active()}")
            return 0

        if args.cmd == "list":
            rows = store.list_active()
            print(f"active_count: {len(rows)}")
            for r in rows:
                print(
                    f"  {r['dev_wallet']}  reason={r['reason']}  "
                    f"evidence_mint={r['evidence_mint']}  "
                    f"added_at={r['added_at']}  added_by={r['added_by']}"
                )
            return 0

        if args.cmd == "check":
            hit = store.is_blacklisted(args.wallet)
            print(f"wallet: {args.wallet}")
            print(f"blacklisted: {hit}")
            if hit:
                row = store.get(args.wallet)
                if row:
                    print(f"  reason: {row['reason']}")
                    print(f"  evidence_mint: {row['evidence_mint']}")
                    print(f"  added_at: {row['added_at']}")
            return 0

        if args.cmd == "deactivate":
            row_before = store.get(args.wallet)
            if row_before is None:
                print(f"NOT FOUND: {args.wallet}")
                return 1
            store.deactivate(args.wallet)
            print(f"Deactivated: {args.wallet}")
            print(f"active_count: {store.count_active()}")
            return 0

        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(_cli())
