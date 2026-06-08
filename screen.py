import argparse
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from dim_token_authority import fetch_authority, TokenAuthorityStore
from dim_token_jup import fetch_jupiter, TokenJupStore
from dim_token_okx import fetch_okx, TokenOkxStore
from dim_blacklist_dev import DevBlacklistStore
from seed_address_labels import get_seed


@dataclass
class ScreenResult:
    mint: str
    composite_verdict: str
    summary: str
    hard_reject_reasons: list = field(default_factory=list)
    soft_flags: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    fetched_at: str = ""


def _safe_get(obj, name, default=None):
    if obj is None:
        return default
    val = getattr(obj, name, default)
    return val if val is not None else default


def _bool(val):
    return val is True


def screen(mint, *, rpc_url, db_path,
           api_jupiter_base="https://datapi.jup.ag",
           api_okx_base="https://web3.okx.com",
           load_seeds=False, verbose=False) -> ScreenResult:

    signals = {
        "helius": {"mint_authority": None, "freeze_authority": None,
                   "is_token_2022": None, "has_permanent_delegate": None,
                   "raw_verdict": None, "effective_verdict": None,
                   "mint_authority_entity": None, "error": None},
        "jupiter": {"organic_score": None, "holder_count": None, "mcap_usd": None,
                    "global_fees_sol": None, "audit_top_holders_pct": None,
                    "audit_bot_holders_pct": None, "audit_dev_migrations": None,
                    "audit_mint_disabled": None, "audit_freeze_disabled": None,
                    "error": None},
        "okx": {"risk_level": None, "dev_rug_count": None, "bundle_pct": None,
                "sniper_pct": None, "top10_pct": None, "is_honeypot": None,
                "is_rugpull": None, "is_wash": None, "tags": None,
                "error": None},
        "blacklist": {"dev_wallet": None, "is_blacklisted": False,
                      "reason": "not in list"},
    }

    auth_store = TokenAuthorityStore(db_path)
    jup_store = TokenJupStore(db_path)
    okx_store = TokenOkxStore(db_path)

    if load_seeds:
        try:
            seeds = get_seed()
            if seeds:
                auth_store.load_address_labels(seeds)
        except Exception as e:
            if verbose:
                print(f"[seed] load failed: {e}")

    # 2. Helius authority
    auth_rec = None
    try:
        auth_rec = fetch_authority(rpc_url, mint, timeout=15.0)
        try:
            auth_store.upsert(auth_rec)
        except Exception as e:
            if verbose:
                print(f"[helius] upsert failed: {e}")
        signals["helius"]["mint_authority"] = _safe_get(auth_rec, "mint_authority")
        signals["helius"]["freeze_authority"] = _safe_get(auth_rec, "freeze_authority")
        signals["helius"]["is_token_2022"] = _safe_get(auth_rec, "is_token_2022", False)
        signals["helius"]["has_permanent_delegate"] = _safe_get(auth_rec, "has_permanent_delegate", False)
        signals["helius"]["error"] = _safe_get(auth_rec, "error")
    except Exception as e:
        signals["helius"]["error"] = str(e)

    # effective verdict from store
    try:
        eff = auth_store.effective_authority_verdict(mint)
        if isinstance(eff, dict):
            signals["helius"]["raw_verdict"] = eff.get("raw_verdict") or eff.get("verdict")
            signals["helius"]["effective_verdict"] = eff.get("effective_verdict") or eff.get("verdict")
            signals["helius"]["mint_authority_entity"] = eff.get("mint_authority_entity") or eff.get("entity")
        elif eff is not None:
            signals["helius"]["effective_verdict"] = str(eff)
    except Exception as e:
        if verbose:
            print(f"[helius] verdict lookup failed: {e}")

    # 3. Dev blacklist (must run before Jupiter/OKX so a blacklisted dev
    # short-circuits and saves two network round-trips).
    dev_wallet = signals["helius"]["mint_authority"]
    signals["blacklist"]["dev_wallet"] = dev_wallet
    bl_active = False
    if dev_wallet:
        try:
            bl_store = DevBlacklistStore(db_path)
            try:
                is_bl = bl_store.is_blacklisted(dev_wallet)
                signals["blacklist"]["is_blacklisted"] = bool(is_bl)
                if is_bl:
                    bl_active = True
                    entry = None
                    try:
                        entry = bl_store.get(dev_wallet)
                    except Exception:
                        entry = None
                    reason = "blacklisted"
                    if isinstance(entry, dict):
                        reason = entry.get("reason") or entry.get("category") or "blacklisted"
                    signals["blacklist"]["reason"] = reason
                else:
                    signals["blacklist"]["reason"] = "not in list"
            except sqlite3.OperationalError:
                signals["blacklist"]["reason"] = "table_missing"
            except Exception as e:
                msg = str(e).lower()
                if "no such table" in msg or "table" in msg and "missing" in msg:
                    signals["blacklist"]["reason"] = "table_missing"
                else:
                    signals["blacklist"]["reason"] = f"error: {e}"
        except Exception as e:
            msg = str(e).lower()
            if "no such table" in msg:
                signals["blacklist"]["reason"] = "table_missing"
            else:
                signals["blacklist"]["reason"] = f"error: {e}"
    else:
        signals["blacklist"]["reason"] = "no_dev_wallet"

    if bl_active:
        signals["jupiter"]["skipped"] = "dev_blacklisted_short_circuit"
        signals["okx"]["skipped"] = "dev_blacklisted_short_circuit"
        return ScreenResult(
            mint=mint,
            composite_verdict="reject",
            summary="dev_blacklisted",
            hard_reject_reasons=["dev_blacklisted"],
            soft_flags=[],
            signals=signals,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    # 4. Jupiter
    jup_rec = None
    try:
        jup_rec = fetch_jupiter(mint, timeout=15.0, api_base=api_jupiter_base)
        try:
            jup_store.upsert(jup_rec)
        except Exception as e:
            if verbose:
                print(f"[jupiter] upsert failed: {e}")
        for k in ("organic_score", "holder_count", "mcap_usd", "global_fees_sol",
                  "audit_top_holders_pct", "audit_bot_holders_pct",
                  "audit_dev_migrations", "audit_mint_disabled",
                  "audit_freeze_disabled", "error"):
            signals["jupiter"][k] = _safe_get(jup_rec, k)
    except Exception as e:
        signals["jupiter"]["error"] = str(e)

    # 5. OKX
    okx_rec = None
    try:
        okx_rec = fetch_okx(mint, timeout=15.0, api_base=api_okx_base)
        try:
            okx_store.upsert(okx_rec)
        except Exception as e:
            if verbose:
                print(f"[okx] upsert failed: {e}")
        for k in ("risk_level", "dev_rug_count", "bundle_pct", "sniper_pct",
                  "top10_pct", "is_honeypot", "is_rugpull", "is_wash",
                  "tags", "error"):
            signals["okx"][k] = _safe_get(okx_rec, k)
    except Exception as e:
        signals["okx"]["error"] = str(e)

    # 7. Composite verdict
    hard = []
    soft = []
    h = signals["helius"]
    j = signals["jupiter"]
    o = signals["okx"]

    # Priority 1
    if bl_active:
        hard.append("dev_blacklisted")
    # Priority 2
    if _bool(h.get("is_token_2022")) and _bool(h.get("has_permanent_delegate")):
        hard.append("t22_permanent_delegate")
    # Priority 3
    if _bool(o.get("is_honeypot")) or _bool(o.get("is_rugpull")) or _bool(o.get("is_wash")):
        hard.append("honeypot_or_rugpull_or_wash")
    # Priority 4
    drc = o.get("dev_rug_count")
    if isinstance(drc, (int, float)) and drc >= 1:
        hard.append("dev_has_prior_rugs")
    # Priority 5
    gfs = j.get("global_fees_sol")
    if isinstance(gfs, (int, float)) and gfs < 30:
        hard.append("low_trading_fees")
    # Priority 6
    top_j = j.get("audit_top_holders_pct")
    top_o = o.get("top10_pct")
    if (isinstance(top_j, (int, float)) and top_j > 60) or \
       (isinstance(top_o, (int, float)) and top_o > 60):
        hard.append("holder_concentration")
    # Priority 7
    bp = o.get("bundle_pct")
    if isinstance(bp, (int, float)) and bp > 30:
        hard.append("bundle_detected")

    # Priority 8 - soft flags
    if h.get("mint_authority"):
        soft.append("unrenounced_mint_authority")
    if h.get("freeze_authority"):
        soft.append("unrenounced_freeze_authority")
    os_score = j.get("organic_score")
    if isinstance(os_score, (int, float)) and os_score < 60:
        soft.append("low_organic_score")
    if j.get("audit_mint_disabled") is False:
        soft.append("mint_not_disabled")
    rl = o.get("risk_level")
    if isinstance(rl, (int, float)) and rl >= 4:
        soft.append("okx_high_risk_level")

    # Determine final verdict
    if hard:
        verdict = "reject"
        summary = "; ".join(hard)
    elif soft:
        verdict = "review"
        summary = "; ".join(soft)
    elif j.get("error") and o.get("error"):
        verdict = "caution"
        summary = "insufficient_data"
        soft.append("insufficient_data")
    else:
        verdict = "allow"
        summary = "no issues detected"

    return ScreenResult(
        mint=mint,
        composite_verdict=verdict,
        summary=summary,
        hard_reject_reasons=hard,
        soft_flags=soft,
        signals=signals,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _print_result(r: ScreenResult):
    verdict = r.composite_verdict.upper()
    bar = "=" * 70
    print(bar)
    print(f"  COMPOSITE VERDICT: {verdict}   ({r.mint})")
    print(f"  {r.summary}")
    print(f"  fetched_at: {r.fetched_at}")
    print(bar)

    h = r.signals.get("helius", {})
    print("\n[ HELIUS / on-chain authority ]")
    print(f"  mint_authority       : {_fmt(h.get('mint_authority'))}")
    print(f"  freeze_authority     : {_fmt(h.get('freeze_authority'))}")
    print(f"  token-2022           : {_fmt(h.get('is_token_2022'))}")
    print(f"  permanent_delegate   : {_fmt(h.get('has_permanent_delegate'))}")
    print(f"  raw_verdict          : {_fmt(h.get('raw_verdict'))}")
    print(f"  effective_verdict    : {_fmt(h.get('effective_verdict'))}")
    print(f"  mint_authority_entity: {_fmt(h.get('mint_authority_entity'))}")
    if h.get("error"):
        print(f"  ERROR                : {h['error']}")

    j = r.signals.get("jupiter", {})
    print("\n[ JUPITER ]")
    print(f"  organic_score        : {_fmt(j.get('organic_score'))}")
    print(f"  holder_count         : {_fmt(j.get('holder_count'))}")
    print(f"  mcap_usd             : {_fmt(j.get('mcap_usd'))}")
    print(f"  global_fees_sol      : {_fmt(j.get('global_fees_sol'))}")
    print(f"  top_holders_pct      : {_fmt(j.get('audit_top_holders_pct'))}")
    print(f"  bot_holders_pct      : {_fmt(j.get('audit_bot_holders_pct'))}")
    print(f"  dev_migrations       : {_fmt(j.get('audit_dev_migrations'))}")
    print(f"  mint_disabled        : {_fmt(j.get('audit_mint_disabled'))}")
    print(f"  freeze_disabled      : {_fmt(j.get('audit_freeze_disabled'))}")
    if j.get("error"):
        print(f"  ERROR                : {j['error']}")

    o = r.signals.get("okx", {})
    print("\n[ OKX ]")
    print(f"  risk_level           : {_fmt(o.get('risk_level'))}")
    print(f"  dev_rug_count        : {_fmt(o.get('dev_rug_count'))}")
    print(f"  bundle_pct           : {_fmt(o.get('bundle_pct'))}")
    print(f"  sniper_pct           : {_fmt(o.get('sniper_pct'))}")
    print(f"  top10_pct            : {_fmt(o.get('top10_pct'))}")
    print(f"  is_honeypot          : {_fmt(o.get('is_honeypot'))}")
    print(f"  is_rugpull           : {_fmt(o.get('is_rugpull'))}")
    print(f"  is_wash              : {_fmt(o.get('is_wash'))}")
    print(f"  tags                 : {_fmt(o.get('tags'))}")
    if o.get("error"):
        print(f"  ERROR                : {o['error']}")

    b = r.signals.get("blacklist", {})
    print("\n[ DEV BLACKLIST ]")
    print(f"  dev_wallet           : {_fmt(b.get('dev_wallet'))}")
    print(f"  is_blacklisted       : {_fmt(b.get('is_blacklisted'))}")
    print(f"  reason               : {_fmt(b.get('reason'))}")

    print("\n" + bar)
    if r.hard_reject_reasons:
        print("HARD REJECT REASONS:")
        for x in r.hard_reject_reasons:
            print(f"  - {x}")
    else:
        print("HARD REJECT REASONS: (none)")
    if r.soft_flags:
        print("SOFT FLAGS:")
        for x in r.soft_flags:
            print(f"  - {x}")
    else:
        print("SOFT FLAGS: (none)")
    print(bar)


def _cli():
    p = argparse.ArgumentParser(description="Composite token screen across Helius/Jupiter/OKX/Blacklist")
    p.add_argument("--mint", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--rpc", required=True)
    p.add_argument("--load-seeds", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()
    result = screen(args.mint, rpc_url=args.rpc, db_path=args.db,
                    load_seeds=args.load_seeds, verbose=args.verbose)
    _print_result(result)


if __name__ == "__main__":
    _cli()
