"""
dim_token_authority — fetch & decode Solana token mint account info.

Decodes the on-chain mint account layout (SPL Token + Token-2022) to extract:
  - mint_authority (None == renounced)
  - freeze_authority (None == no freeze risk)
  - update_authority (Token-2022 / Metaplex metadata)
  - permanent_delegate (Token-2022 honeypot trap)
  - is_token_2022 (program id check)
  - supply, decimals

Storage: SQLite (Universal SQL Agent stack).

Usage:
    from dim_token_authority import TokenAuthorityStore, fetch_authority
    store = TokenAuthorityStore("path/to/wallet_tracking.db")
    rec = fetch_authority(rpc_client, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    store.upsert(rec)

CLI:
    python -m dim_token_authority --mint <MINT> [--db PATH] [--rpc URL]

References:
    - SPL Token Mint layout:  https://github.com/solana-labs/solana-program-library
      /token/program/src/state.rs (82 bytes)
    - Token-2022 Mint extension:  https://github.com/solana-labs/solana-program-library
      /token/program-2022/src/state.rs (variable; base 82 + extensions)
    - Review by Claude Opus: /mnt/e/Wallet_Tracking/REVIEW_claude_opus_full.md
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import struct
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Third-party — installed in this project's venv
import httpx

log = logging.getLogger(__name__)

# --- Solana program IDs -------------------------------------------------------

# Well-known program IDs (base58). Verified against solana-program-library.
SPL_TOKEN_PROGRAM_ID       = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID      = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
METAPLEX_TOKEN_METADATA    = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# --- Layout constants ---------------------------------------------------------

# SPL Token Mint layout (82 bytes):
#   0..4   mint_authority_option (u32 little-endian; 1 = present, 0 = None)
#   4..36  mint_authority (Pubkey, 32 bytes)
#   36..44 supply (u64)
#   44     decimals (u8)
#   45     is_initialized (u8)
#   46..50 freeze_authority_option (u32)
#   50..82 freeze_authority (Pubkey)
SPL_MINT_SIZE = 82
SPL_MINT_LAYOUT = struct.Struct("<I32sQBB I32s")  # only used for sanity; we unpack manually

# --- Data class ---------------------------------------------------------------

@dataclass
class TokenAuthority:
    token_mint: str
    mint_authority: Optional[str] = None
    freeze_authority: Optional[str] = None
    update_authority: Optional[str] = None
    is_mutable_metadata: Optional[bool] = None
    is_token_2022: bool = False
    has_permanent_delegate: bool = False
    program_id: Optional[str] = None
    supply: Optional[int] = None
    decimals: Optional[int] = None
    raw_account_size: int = 0
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen_chain: bool = True
    error: Optional[str] = None
    source: str = "helius_rpc"

    def to_row(self) -> dict:
        return asdict(self)


# --- Parsers ------------------------------------------------------------------

def _parse_pubkey_option(data: bytes, opt_offset: int, key_offset: int) -> Optional[str]:
    """Read a COption<Pubkey> (u32 option tag + 32 bytes key if present).

    Returns base58 pubkey string, or None if the option is empty.
    """
    if opt_offset + 4 > len(data):
        return None
    (opt,) = struct.unpack_from("<I", data, opt_offset)
    if opt == 0:
        return None
    if opt != 1:
        # Malformed account — log and treat as None rather than crash.
        log.warning("Unexpected option tag %d at offset %d", opt, opt_offset)
        return None
    if key_offset + 32 > len(data):
        return None
    # Lazy import: solders is heavy and only needed here.
    from solders.pubkey import Pubkey
    return str(Pubkey(data[key_offset:key_offset + 32]))


def _parse_spl_mint(data: bytes) -> dict:
    """Parse SPL Token mint (82 bytes). Raises ValueError if too short."""
    if len(data) < SPL_MINT_SIZE:
        raise ValueError(f"SPL mint account too short: {len(data)} bytes (need {SPL_MINT_SIZE})")
    mint_authority = _parse_pubkey_option(data, opt_offset=0, key_offset=4)
    (supply,) = struct.unpack_from("<Q", data, 36)
    (decimals,) = struct.unpack_from("<B", data, 44)
    # is_initialized at offset 45 — we don't store this; mint layout requires it.
    freeze_authority = _parse_pubkey_option(data, opt_offset=46, key_offset=50)
    return {
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "supply": supply,
        "decimals": decimals,
    }


def _parse_token_2022_mint(data: bytes) -> dict:
    """Parse Token-2022 mint: SPL base (82) + TLV extensions.

    We only care about extensions that affect LP risk: PermanentDelegate.
    Other extensions (TransferHook, NonTransferable, etc.) get logged but not
    blocked — see TODO for the future expansion.
    """
    base = _parse_spl_mint(data)
    # Extensions start at offset 82 as TLV:  (u16 type, u16 length) then `length` bytes
    off = SPL_MINT_SIZE
    has_perm_delegate = False
    update_authority = None
    is_mutable_metadata = None
    while off + 4 <= len(data):
        (ext_type, ext_len) = struct.unpack_from("<HH", data, off)
        off += 4
        ext_data = data[off:off + ext_len]
        # Extension type IDs from spl-token-2022 state.rs
        # https://github.com/solana-labs/solana-program-library/blob/master/token/program-2022/src/extension.rs
        PERMANENT_DELEGATE_TYPE = 22
        # Metadata-pointer (Type 19) and metadata itself are Metaplex, not T-2022.
        # We don't try to resolve Metaplex PDA here — note None for now.
        if ext_type == PERMANENT_DELEGATE_TYPE:
            # Layout: 32-byte delegate pubkey
            if ext_len >= 32:
                from solders.pubkey import Pubkey
                # presence of any non-zero pubkey counts; we just want the flag here
                has_perm_delegate = any(b != 0 for b in ext_data[:32])
        # Advance
        off += ext_len
    return {
        **base,
        "has_permanent_delegate": has_perm_delegate,
        "update_authority": update_authority,
        "is_mutable_metadata": is_mutable_metadata,
    }


# --- Public API ---------------------------------------------------------------

def parse_mint_account(data: bytes, program_id: str) -> dict:
    """Dispatch parser based on program id; returns dict of fields."""
    if program_id == SPL_TOKEN_PROGRAM_ID:
        return {
            "is_token_2022": False,
            "has_permanent_delegate": False,
            # SPL Token program itself does not store metadata or update authority
            # on the mint account. Metaplex metadata lives in a separate PDA; we
            # leave these None and document that.
            "update_authority": None,
            "is_mutable_metadata": None,
            **_parse_spl_mint(data),
        }
    if program_id == TOKEN_2022_PROGRAM_ID:
        return {"is_token_2022": True, **_parse_token_2022_mint(data)}
    raise ValueError(f"Unknown program_id for mint account: {program_id}")


def fetch_authority(rpc_url: str, mint: str, *, timeout: float = 15.0) -> TokenAuthority:
    """Call getAccountInfo for `mint` and decode the authority fields.

    Uses Helius (or any Solana JSON-RPC) over HTTPS. No SDK required for the
    RPC call itself — keeps the dependency surface minimal.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            mint,
            {"encoding": "base64", "commitment": "confirmed"},
        ],
    }
    try:
        r = httpx.post(rpc_url, json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return TokenAuthority(token_mint=mint, error=f"rpc_error:{type(e).__name__}:{e}",
                              last_seen_chain=False)

    if "error" in body:
        return TokenAuthority(token_mint=mint, error=f"rpc_error:{body['error'].get('message','?')}",
                              last_seen_chain=False)

    value = body.get("result", {}).get("value")
    if value is None:
        return TokenAuthority(token_mint=mint, error="not_found", last_seen_chain=False)

    # Helius/standard RPC returns data as [base64_string] (wrapped in a list
    # when the account has multiple data blobs) or as a base64 string. Be
    # defensive about both shapes.
    raw_data = value.get("data")
    if isinstance(raw_data, list):
        if not raw_data:
            return TokenAuthority(token_mint=mint, error="empty_data", last_seen_chain=False)
        raw_data = raw_data[0]
    if not isinstance(raw_data, str):
        return TokenAuthority(token_mint=mint, error="unparseable:non_string_data",
                              last_seen_chain=False)
    try:
        data = base64.b64decode(raw_data)
    except Exception as e:
        return TokenAuthority(token_mint=mint, error=f"unparseable:b64:{e}",
                              last_seen_chain=False)

    program_id = value.get("owner", "")
    try:
        fields = parse_mint_account(data, program_id)
    except ValueError as e:
        return TokenAuthority(token_mint=mint, program_id=program_id, error=f"unparseable:{e}",
                              raw_account_size=len(data), last_seen_chain=False)

    return TokenAuthority(
        token_mint=mint,
        program_id=program_id,
        raw_account_size=len(data),
        **fields,
    )


# --- SQLite store -------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "sql" / "dim_token_authority.sql"
SCHEMA_PATH_LABELS = Path(__file__).parent / "sql" / "dim_address_label.sql"


class TokenAuthorityStore:
    """SQLite-backed store. Single-writer; pass check_same_thread=False if you
    need to share across threads (we don't, by default).

    Holds two related tables:
      - dim_token_authority: per-mint decoded on-chain state
      - dim_address_label:  per-authority whitelist of known-good issuers
    And two views:
      - v_token_authority_risk:       raw flags + base verdict
      - v_effective_authority_verdict: verdict post-whitelist
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
        # dim_address_label + v_effective_authority_verdict may not exist yet
        # (older DB created before this table). Apply idempotently.
        with open(SCHEMA_PATH_LABELS, "r", encoding="utf-8") as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    def upsert(self, rec: TokenAuthority) -> None:
        row = rec.to_row()
        # We intentionally do NOT carry historical mint_authority_revoked_at;
        # the spec mentions it but it requires chain history, not on-chain read.
        # We could add a `revoked_at` table later, but that's a separate concern.
        self._conn.execute(
            """
            INSERT INTO dim_token_authority (
                token_mint, mint_authority, freeze_authority, update_authority,
                is_mutable_metadata, is_token_2022, has_permanent_delegate,
                program_id, supply, decimals, raw_account_size,
                checked_at, last_seen_chain, error, source
            ) VALUES (
                :token_mint, :mint_authority, :freeze_authority, :update_authority,
                :is_mutable_metadata, :is_token_2022, :has_permanent_delegate,
                :program_id, :supply, :decimals, :raw_account_size,
                :checked_at, :last_seen_chain, :error, :source
            )
            ON CONFLICT(token_mint) DO UPDATE SET
                mint_authority         = excluded.mint_authority,
                freeze_authority       = excluded.freeze_authority,
                update_authority       = excluded.update_authority,
                is_mutable_metadata    = excluded.is_mutable_metadata,
                is_token_2022          = excluded.is_token_2022,
                has_permanent_delegate = excluded.has_permanent_delegate,
                program_id             = excluded.program_id,
                supply                 = excluded.supply,
                decimals               = excluded.decimals,
                raw_account_size       = excluded.raw_account_size,
                checked_at             = excluded.checked_at,
                last_seen_chain        = excluded.last_seen_chain,
                error                  = excluded.error,
                source                 = excluded.source
            """,
            row,
        )
        self._conn.commit()

    def get(self, token_mint: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM dim_token_authority WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def authority_risk(self, token_mint: str) -> Optional[dict]:
        """Raw verdict from v_token_authority_risk. Use effective_authority_verdict
        for the whitelist-resolved decision."""
        cur = self._conn.execute(
            "SELECT * FROM v_token_authority_risk WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def effective_authority_verdict(self, token_mint: str) -> Optional[dict]:
        """Post-whitelist verdict from v_effective_authority_verdict.

        Returns a row with both raw and effective verdicts, plus the entity name
        and confidence of any whitelisted authority. This is the QUERY TO USE
        for downstream LP risk decisions.
        """
        cur = self._conn.execute(
            "SELECT * FROM v_effective_authority_verdict WHERE token_mint = ?",
            (token_mint,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def add_address_label(
        self,
        address: str,
        kind: str,
        entity: str | None = None,
        category: str | None = None,
        source: str = "manual",
        confidence: str = "medium",
        notes: str | None = None,
        active: bool = True,
    ) -> None:
        """Idempotent insert/update of one row in dim_address_label."""
        self._conn.execute(
            """
            INSERT INTO dim_address_label
                (address, kind, entity, category, source, confidence, notes, active, verified_at)
            VALUES
                (:address, :kind, :entity, :category, :source, :confidence, :notes, :active,
                 CASE WHEN :source IN ('helius_live', 'official_docs') THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT(address) DO UPDATE SET
                kind = excluded.kind,
                entity = excluded.entity,
                category = excluded.category,
                source = excluded.source,
                confidence = excluded.confidence,
                notes = excluded.notes,
                active = excluded.active,
                verified_at = CASE WHEN excluded.source IN ('helius_live', 'official_docs')
                                   THEN CURRENT_TIMESTAMP ELSE dim_address_label.verified_at END
            """,
            dict(address=address, kind=kind, entity=entity, category=category,
                 source=source, confidence=confidence, notes=notes, active=1 if active else 0),
        )
        self._conn.commit()

    def load_address_labels(self, entries: list[dict]) -> int:
        """Bulk load from a list of dicts (matching seed_address_labels.SEED_ENTRIES
        schema). Returns count of rows upserted."""
        for e in entries:
            self.add_address_label(
                address=e["address"],
                kind=e["kind"],
                entity=e.get("entity"),
                category=e.get("category"),
                source=e.get("source", "manual"),
                confidence=e.get("confidence", "medium"),
                notes=e.get("notes"),
                active=e.get("active", True),
            )
        return len(entries)

    def address_label_count(self, kind: str | None = None) -> int:
        if kind:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM dim_address_label WHERE kind = ? AND active = 1", (kind,)
            )
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM dim_address_label WHERE active = 1"
            )
        return cur.fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


# --- CLI ----------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Fetch and store Solana token mint authority")
    p.add_argument("--mint", required=True, help="Token mint address (base58)")
    p.add_argument("--db", default="wallet_tracking.db", help="SQLite DB path")
    p.add_argument("--rpc", default=os.environ.get("HELIUS_RPC_URL", ""),
                   help="Solana RPC URL (or set HELIUS_RPC_URL env)")
    p.add_argument("--load-seeds", action="store_true",
                   help="Load seed_address_labels.SEED_ENTRIES into dim_address_label "
                        "before computing the effective verdict")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.rpc:
        print("ERROR: --rpc or HELIUS_RPC_URL is required (e.g. https://mainnet.helius-rpc.com/?api-key=...)")
        return 2

    rec = fetch_authority(args.rpc, args.mint)
    print("--- fetched ---")
    for k, v in rec.to_row().items():
        print(f"  {k}: {v}")

    store = TokenAuthorityStore(args.db)
    store.upsert(rec)
    print(f"\nUpserted into {args.db}")

    if args.load_seeds:
        try:
            from seed_address_labels import get_seed
            n = store.load_address_labels(get_seed())
            print(f"Loaded {n} seed entries into dim_address_label")
        except ImportError:
            print("WARN: seed_address_labels.py not found; skipping seed load")

    risk = store.authority_risk(args.mint)
    if risk:
        # 3 independent flags + composite verdict (live-test refined 2026-06-06).
        # Original Opus "authority_risk score 0..6" was too aggressive: it flagged
        # USDC/USDT/mSOL as "avoid" because they keep mint authority for legitimate
        # operations. Now only Token-2022 permanent delegate is a hard avoid.
        flags = []
        if risk["unrenounced_mint_warning"]:
            flags.append("mint_authority_not_renounced")
        if risk["unrenounced_freeze_warning"]:
            flags.append("freeze_authority_not_renounced")
        if risk["t22_perm_delegate_hard_avoid"]:
            flags.append("t22_permanent_delegate")
        flag_str = ",".join(flags) if flags else "(none)"
        print(
            f"\n--- raw (pre-whitelist) ---\n"
            f"  base_verdict: {risk['authority_verdict']}\n"
            f"  flags: {flag_str}\n"
            f"  hard_avoid (T22 perm_delegate): {bool(risk['t22_perm_delegate_hard_avoid'])}\n"
            f"  soft_warnings: mint={bool(risk['unrenounced_mint_warning'])} "
            f"freeze={bool(risk['unrenounced_freeze_warning'])}"
        )
        # Now resolve via whitelist
        eff = store.effective_authority_verdict(args.mint)
        if eff:
            print(
                f"\n--- effective (post-whitelist) ---\n"
                f"  effective_verdict: {eff['effective_verdict']}\n"
                f"  mint_authority_entity: {eff['mint_authority_entity'] or '(not whitelisted)'}\n"
                f"  freeze_authority_entity: {eff['freeze_authority_entity'] or '(not whitelisted)'}\n"
                f"  mint_authority_confidence: {eff['mint_authority_confidence'] or '-'}\n"
                f"  freeze_authority_confidence: {eff['freeze_authority_confidence'] or '-'}"
            )
    else:
        print(f"verdict: <unknown — fetch error: {rec.error}>")

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
