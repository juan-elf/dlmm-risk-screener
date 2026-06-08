"""
dim_token_okx — RugCheck enrichment for Solana token mints.

Historical name: this module was originally backed by OKX OnchainOS. As of
2026-06-08, the OKX /api/v6/dex/market/* endpoints moved to an x402
micropayment paywall ($0.50/call). The fetcher was migrated to RugCheck
(api.rugcheck.xyz/v1/tokens/<mint>/report) — free, no API key.

The table name (`dim_token_okx`), `TokenOkxRecord` dataclass, `TokenOkxStore`
class name, and `v_token_okx_risk` view are kept for continuity. Only the
underlying HTTP source changed. See DESIGN_NOTES.md "2026-06-08: OKX →
RugCheck fetcher migration".

Field provenance after the migration:
  - risk_level         <- bucketed from `score_normalised`
  - is_honeypot        <- any risk with type/name containing "honeypot"
  - is_rugpull         <- `rugged` boolean
  - is_wash            <- any risk with type/name containing "wash"
  - dev_rug_count      <- len(creatorTokens) (proxy: prior tokens by creator)
  - bundle_pct         <- sum of insiderNetworks percentages (cluster %)
  - sniper_pct         <- risks[].pct where name/type matches "sniper"
  - top10_pct          <- sum of top 10 holders' percentages
  - total_holders      <- totalHolders (stored as dev_token_count fallback)
  - tags_json          <- JSON array of risk names from risks[]
  - lp_burned_pct,
    suspicious_pct,
    dev_holding_pct,
    ath_usd / atl_usd / price_vs_ath_pct, current_price_usd
                       -> None (RugCheck does not provide these)

The defensive contract is preserved: every HTTP failure returns a record with
`error` set rather than raising. 404 -> error="not_found",
last_seen_chain=False.

Storage: SQLite (Universal SQL Agent stack).

Usage:
    from dim_token_okx import TokenOkxStore, fetch_okx
    store = TokenOkxStore("path/to/wallet_tracking.db")
    rec = fetch_okx("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    store.upsert(rec)

CLI:
    python dim_token_okx.py --mint <MINT> [--db PATH] [--api URL]
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

# --- RugCheck API ------------------------------------------------------------

RUGCHECK_BASE = "https://api.rugcheck.xyz"
RUGCHECK_REPORT_PATH = "/v1/tokens/{mint}/report"

# Backward-compat alias — old code paths or imports referencing OKX_WEB3_BASE
# get RugCheck transparently. Kept so external callers don't break.
OKX_WEB3_BASE = RUGCHECK_BASE

# --- Data class ---------------------------------------------------------------

@dataclass
class TokenOkxRecord:
    """Mirrors dim_token_okx columns. All enrichment fields nullable for
    unknown / partial-failure responses.

    Name preserved from the OKX era for SQL/schema continuity; the underlying
    fetcher is now RugCheck.
    """
    token_mint: str
    # Bucketed risk (1-5, derived from RugCheck score_normalised)
    risk_level: Optional[int] = None
    # Insider/cluster holding %
    bundle_pct: Optional[float] = None
    # Sniper risk percentage (if reported in risks[])
    sniper_pct: Optional[float] = None
    # Not provided by RugCheck; left None
    suspicious_pct: Optional[float] = None
    dev_holding_pct: Optional[float] = None
    # Top-10 holder concentration %
    top10_pct: Optional[float] = None
    # Not provided by RugCheck; left None
    lp_burned_pct: Optional[float] = None
    # Headline: number of prior tokens deployed by the creator
    dev_rug_count: Optional[int] = None
    # Repurposed: now stores totalHolders (RugCheck doesn't give per-dev count)
    dev_token_count: Optional[int] = None
    # Risk booleans
    is_honeypot: Optional[bool] = None
    is_rugpull: Optional[bool] = None
    is_wash: Optional[bool] = None
    # Not provided by RugCheck; left None
    ath_usd: Optional[float] = None
    atl_usd: Optional[float] = None
    current_price_usd: Optional[float] = None
    price_vs_ath_pct: Optional[float] = None
    # Risk names as JSON array
    tags_json: Optional[str] = None
    # Raw payloads. raw_advanced_info_json now stores the full RugCheck body;
    # raw_risk_check_json / raw_price_info_json are kept None (kept in schema
    # so the table layout is unchanged).
    raw_advanced_info_json: Optional[str] = None
    raw_risk_check_json: Optional[str] = None
    raw_price_info_json: Optional[str] = None
    # bookkeeping
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_seen_chain: bool = True
    error: Optional[str] = None
    source: str = "rugcheck"

    def to_row(self) -> dict:
        return asdict(self)


# --- Coercion helpers --------------------------------------------------------

def _coerce_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _safe_json_dumps(obj) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        log.warning("raw_json serialize failed: %s", e)
        return None


# --- Parser ------------------------------------------------------------------

def _bucket_risk_level(score) -> Optional[int]:
    """Bucket RugCheck's score_normalised (0-100) into a 1-5 risk level.

      >= 80 -> 5  (severe)
      >= 60 -> 4  (high)
      >= 40 -> 3  (medium)
      >= 20 -> 2  (low)
      else  -> 1  (clean)
    """
    s = _coerce_float(score)
    if s is None:
        return None
    if s >= 80:
        return 5
    if s >= 60:
        return 4
    if s >= 40:
        return 3
    if s >= 20:
        return 2
    return 1


def _risk_name(risk) -> str:
    """Extract a lowercased identifier from a risk entry (dict or string)."""
    if isinstance(risk, str):
        return risk.lower()
    if isinstance(risk, dict):
        for key in ("name", "type", "title", "category"):
            v = risk.get(key)
            if isinstance(v, str) and v:
                return v.lower()
    return ""


def _risk_pct(risk) -> Optional[float]:
    if isinstance(risk, dict):
        for key in ("pct", "percentage", "percent", "value"):
            v = _coerce_float(risk.get(key))
            if v is not None:
                return v
    return None


def _sum_top_holder_pct(top_holders) -> Optional[float]:
    """Sum percentages of the top-10 holders. Returns None when the field
    is missing or unparseable (so the `gate_top10_concentrated` rule
    stays at 0 instead of falsely tripping on an unknown denominator)."""
    if not isinstance(top_holders, list) or not top_holders:
        return None
    total = 0.0
    found_any = False
    for h in top_holders[:10]:
        if not isinstance(h, dict):
            continue
        for key in ("pct", "percentage", "percent", "ownerPct"):
            v = _coerce_float(h.get(key))
            if v is not None:
                total += v
                found_any = True
                break
    return round(total, 4) if found_any else None


def _sum_insider_pct(insider_networks) -> Optional[float]:
    """Sum percentages across all detected insider clusters. Returns None
    when no insider data is available (so `gate_bundle_high` doesn't
    misfire on missing data)."""
    if insider_networks is None:
        return None
    # RugCheck can return either a list of network dicts or a dict keyed by
    # network ID. Handle both.
    networks = []
    if isinstance(insider_networks, list):
        networks = insider_networks
    elif isinstance(insider_networks, dict):
        networks = list(insider_networks.values())
    else:
        return None
    if not networks:
        return None
    total = 0.0
    found_any = False
    for n in networks:
        if not isinstance(n, dict):
            continue
        for key in ("tokenAmountPct", "pct", "percentage", "percent", "share"):
            v = _coerce_float(n.get(key))
            if v is not None:
                total += v
                found_any = True
                break
    return round(total, 4) if found_any else None


def _parse_rugcheck_report(mint: str, body) -> dict:
    """Parse a RugCheck /v1/tokens/<mint>/report body into the dim_token_okx
    field dict. Defensive: missing/null/malformed fields -> None. Never raises.
    """
    out = {
        "risk_level":         None,
        "bundle_pct":         None,
        "sniper_pct":         None,
        "suspicious_pct":     None,
        "dev_holding_pct":    None,
        "top10_pct":          None,
        "lp_burned_pct":      None,
        "dev_rug_count":      None,
        "dev_token_count":    None,
        "is_honeypot":        None,
        "is_rugpull":         None,
        "is_wash":            None,
        "ath_usd":            None,
        "atl_usd":            None,
        "current_price_usd":  None,
        "price_vs_ath_pct":   None,
        "tags_json":          None,
        "raw_advanced_info_json": _safe_json_dumps(body),
        "raw_risk_check_json":    None,
        "raw_price_info_json":    None,
    }
    if not isinstance(body, dict):
        return out

    # Risk level (bucketed score_normalised)
    out["risk_level"] = _bucket_risk_level(body.get("score_normalised"))

    # Risks-array driven flags
    risks = body.get("risks")
    risk_entries: list = risks if isinstance(risks, list) else []

    honeypot = False
    wash = False
    sniper_pct: Optional[float] = None
    risk_names: list = []
    for r in risk_entries:
        name = _risk_name(r)
        if not name:
            continue
        risk_names.append(
            (r.get("name") if isinstance(r, dict) and r.get("name") else name)
        )
        if "honeypot" in name:
            honeypot = True
        if "wash" in name:
            wash = True
        if "sniper" in name and sniper_pct is None:
            pct = _risk_pct(r)
            if pct is not None:
                sniper_pct = pct

    out["is_honeypot"] = honeypot
    out["is_wash"]     = wash
    out["sniper_pct"]  = sniper_pct
    if risk_names:
        try:
            out["tags_json"] = json.dumps(risk_names, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            out["tags_json"] = None
    else:
        out["tags_json"] = json.dumps([])

    # `rugged` -> is_rugpull (defaults to False when key missing)
    rugged_val = body.get("rugged")
    out["is_rugpull"] = bool(rugged_val) if rugged_val is not None else False

    # creatorTokens -> dev_rug_count (proxy: how many tokens the creator
    # previously deployed). null/missing -> 0.
    ct = body.get("creatorTokens")
    if isinstance(ct, list):
        out["dev_rug_count"] = len(ct)
    else:
        out["dev_rug_count"] = 0

    # Insider networks -> bundle_pct (cluster concentration)
    out["bundle_pct"] = _sum_insider_pct(body.get("insiderNetworks"))

    # Top-10 holders -> top10_pct
    out["top10_pct"] = _sum_top_holder_pct(body.get("topHolders"))

    # totalHolders -> dev_token_count slot (repurposed; the column was unused
    # for dev_token_count after the OKX paid endpoints went away).
    total_holders = _coerce_int(body.get("totalHolders"))
    out["dev_token_count"] = total_holders if total_holders is not None else 0

    return out


# --- HTTP helper -------------------------------------------------------------

def _get_json(client: httpx.Client, url: str, timeout: float):
    """GET and decode JSON. Returns (body, error_str). On success error_str=None.
    Maps 404 to error='not_found' (RugCheck signals unknown tokens this way).
    """
    try:
        r = client.get(url, timeout=timeout)
    except httpx.HTTPError as e:
        return None, f"http_error:{type(e).__name__}"
    if r.status_code == 404:
        return None, "not_found"
    if r.status_code != 200:
        return None, f"http:{r.status_code}"
    try:
        return r.json(), None
    except ValueError:
        return None, "invalid_json"


# --- Public API --------------------------------------------------------------

def fetch_risk(
    token_mint: str,
    *,
    timeout: float = 15.0,
    api_base: str = RUGCHECK_BASE,
) -> TokenOkxRecord:
    """GET RugCheck's /v1/tokens/<mint>/report and parse into a record.

    Never raises. Network/HTTP errors set `error` and `last_seen_chain=False`.
    A 404 surfaces as `error='not_found'`.
    """
    base = api_base.rstrip("/")
    url = f"{base}{RUGCHECK_REPORT_PATH.format(mint=token_mint)}"
    rec = TokenOkxRecord(token_mint=token_mint)
    with httpx.Client(headers={"Accept": "application/json"}) as client:
        body, err = _get_json(client, url, timeout)

    if err is not None:
        rec.error = err
        rec.last_seen_chain = False
        # Still capture whatever the server returned (None on transport error)
        rec.raw_advanced_info_json = _safe_json_dumps(body)
        return rec

    fields = _parse_rugcheck_report(token_mint, body)
    for k, v in fields.items():
        setattr(rec, k, v)
    rec.error = None
    rec.last_seen_chain = True
    return rec


# Backward-compat alias: existing callers (e.g. screen.py) use `fetch_okx`.
# Same signature; just dispatches to the RugCheck-backed implementation.
def fetch_okx(
    token_mint: str,
    *,
    timeout: float = 15.0,
    api_base: str = RUGCHECK_BASE,
    chain_index: str = "501",  # kept for signature compatibility; unused
) -> TokenOkxRecord:
    return fetch_risk(token_mint, timeout=timeout, api_base=api_base)


# --- SQLite store ------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "sql" / "dim_token_okx.sql"


class TokenOkxStore:
    """SQLite-backed store for dim_token_okx. Single-writer; idempotent upserts.

    Name preserved from the OKX era; underlying fetcher is now RugCheck. The
    table schema and `v_token_okx_risk` view are unchanged.
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

    def upsert(self, rec: TokenOkxRecord) -> None:
        row = rec.to_row()
        self._conn.execute(
            """
            INSERT INTO dim_token_okx (
                token_mint,
                risk_level, bundle_pct, sniper_pct, suspicious_pct,
                dev_holding_pct, top10_pct, lp_burned_pct,
                dev_rug_count, dev_token_count,
                is_honeypot, is_rugpull, is_wash,
                ath_usd, atl_usd, current_price_usd, price_vs_ath_pct,
                tags_json, raw_advanced_info_json, raw_risk_check_json, raw_price_info_json,
                fetched_at, last_seen_chain, error, source
            ) VALUES (
                :token_mint,
                :risk_level, :bundle_pct, :sniper_pct, :suspicious_pct,
                :dev_holding_pct, :top10_pct, :lp_burned_pct,
                :dev_rug_count, :dev_token_count,
                :is_honeypot, :is_rugpull, :is_wash,
                :ath_usd, :atl_usd, :current_price_usd, :price_vs_ath_pct,
                :tags_json, :raw_advanced_info_json, :raw_risk_check_json, :raw_price_info_json,
                :fetched_at, :last_seen_chain, :error, :source
            )
            ON CONFLICT(token_mint) DO UPDATE SET
                risk_level             = excluded.risk_level,
                bundle_pct             = excluded.bundle_pct,
                sniper_pct             = excluded.sniper_pct,
                suspicious_pct         = excluded.suspicious_pct,
                dev_holding_pct        = excluded.dev_holding_pct,
                top10_pct              = excluded.top10_pct,
                lp_burned_pct          = excluded.lp_burned_pct,
                dev_rug_count          = excluded.dev_rug_count,
                dev_token_count        = excluded.dev_token_count,
                is_honeypot            = excluded.is_honeypot,
                is_rugpull             = excluded.is_rugpull,
                is_wash                = excluded.is_wash,
                ath_usd                = excluded.ath_usd,
                atl_usd                = excluded.atl_usd,
                current_price_usd      = excluded.current_price_usd,
                price_vs_ath_pct       = excluded.price_vs_ath_pct,
                tags_json              = excluded.tags_json,
                raw_advanced_info_json = excluded.raw_advanced_info_json,
                raw_risk_check_json    = excluded.raw_risk_check_json,
                raw_price_info_json    = excluded.raw_price_info_json,
                fetched_at             = excluded.fetched_at,
                last_seen_chain        = excluded.last_seen_chain,
                error                  = excluded.error,
                source                 = excluded.source
            """,
            row,
        )
        self._conn.commit()

    def get(self, token_mint: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM dim_token_okx WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def okx_risk(self, token_mint: str) -> Optional[dict]:
        """Query v_token_okx_risk for the per-mint screening row."""
        cur = self._conn.execute(
            "SELECT * FROM v_token_okx_risk WHERE token_mint = ?", (token_mint,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM dim_token_okx")
        return cur.fetchone()["c"]

    def close(self) -> None:
        self._conn.close()


# --- CLI ---------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Fetch RugCheck enrichment for a Solana token mint"
    )
    p.add_argument("--mint", required=True, help="Token mint address (base58)")
    p.add_argument("--db", default="wallet_tracking.db", help="SQLite DB path")
    p.add_argument("--api", default=os.environ.get("RUGCHECK_URL", RUGCHECK_BASE),
                   help=f"RugCheck API base URL (default: {RUGCHECK_BASE})")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="HTTP timeout in seconds (default: 15.0)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rec = fetch_risk(args.mint, timeout=args.timeout, api_base=args.api)
    print("--- fetched ---")
    for k, v in rec.to_row().items():
        if k.startswith("raw_") and isinstance(v, str) and len(v) > 200:
            v = v[:200] + f"... ({len(v)} chars total)"
        print(f"  {k}: {v}")

    store = TokenOkxStore(args.db)
    store.upsert(rec)
    print(f"\nUpserted into {args.db}")

    risk = store.okx_risk(args.mint)
    if risk:
        hard_flags = []
        if risk["gate_is_honeypot"]:
            hard_flags.append("is_honeypot")
        if risk["gate_is_rugpull"]:
            hard_flags.append("is_rugpull")
        if risk["gate_is_wash"]:
            hard_flags.append("is_wash")
        if risk["gate_dev_rug_count"]:
            hard_flags.append("dev_rug_count>=1")
        if risk["gate_bundle_high"]:
            hard_flags.append("bundle_pct>30")
        if risk["gate_top10_concentrated"]:
            hard_flags.append("top10_pct>60")
        soft_flags = []
        if risk["gate_sniper_high"]:
            soft_flags.append("sniper_pct>30")
        if risk["gate_risk_level_high"]:
            soft_flags.append("risk_level>=4")
        hard_str = ",".join(hard_flags) if hard_flags else "(none)"
        soft_str = ",".join(soft_flags) if soft_flags else "(none)"
        print(
            f"\n--- v_token_okx_risk ---\n"
            f"  risk_level: {risk['risk_level']}\n"
            f"  dev_rug_count: {risk['dev_rug_count']}\n"
            f"  dev_token_count: {risk['dev_token_count']}\n"
            f"  bundle_pct: {risk['bundle_pct']}\n"
            f"  sniper_pct: {risk['sniper_pct']}\n"
            f"  lp_burned_pct: {risk['lp_burned_pct']}\n"
            f"  is_honeypot: {risk['is_honeypot']}  "
            f"is_rugpull: {risk['is_rugpull']}  "
            f"is_wash: {risk['is_wash']}\n"
            f"  current_price_usd: {risk['current_price_usd']}\n"
            f"  price_vs_ath_pct: {risk['price_vs_ath_pct']}\n"
            f"  tags_json: {risk['tags_json']}\n"
            f"  hard_flags: {hard_str}\n"
            f"  soft_flags: {soft_str}\n"
            f"  is_unknown: {bool(risk['is_unknown'])}"
        )
    else:
        print(f"\n(no risk row — fetch error: {rec.error})")

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
