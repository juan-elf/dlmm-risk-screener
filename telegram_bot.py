import argparse, logging, os, sys, time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
from screen import screen, ScreenResult  # type: ignore

log = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_VERDICT_EMOJI = {"allow": "✅", "review": "⚠️", "caution": "❔", "reject": "❌"}


def _api_call(token: str, method: str, **params) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    r = httpx.post(url, json=params, timeout=30.0)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        log.error("Telegram API error: %s", body)
        raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
    return body["result"]


def send_message(token: str, chat_id: int, text: str) -> None:
    if len(text) > 4000:
        text = text[:3990] + "\n[truncated]"
    _api_call(token, "sendMessage", chat_id=chat_id, text=text, parse_mode=None)


def _source_line(src: str, sig: dict) -> Optional[str]:
    err = sig.get("error")
    if err:
        return f"  [{src}] error: {err}"
    if src == "helius":
        entity = sig.get("mint_authority_entity") or "unknown"
        eff = sig.get("effective_verdict", "?")
        raw = sig.get("raw_verdict", "?")
        t22 = " T22+perm_del" if sig.get("has_permanent_delegate", False) else ""
        return f"  [helius] {raw} → {eff} (entity: {entity}){t22}"
    if src == "jupiter":
        return (f"  [jupiter] organic={sig.get('organic_score')} "
                f"fees={sig.get('global_fees_sol')} SOL "
                f"top10={sig.get('audit_top_holders_pct')}%")
    if src == "okx":
        return (f"  [okx] risk={sig.get('risk_level')} "
                f"dev_rugs={sig.get('dev_rug_count')} "
                f"bundle={sig.get('bundle_pct')}%")
    if src == "blacklist":
        if sig.get("is_blacklisted", False):
            return f"  [blacklist] ⚠️  HIT: {sig.get('reason', '?')}"
        return "  [blacklist] clear"
    return None


def format_verdict(result: ScreenResult) -> str:
    lines = [
        f"{_VERDICT_EMOJI.get(result.composite_verdict, '?')} "
        f"VERDICT: {result.composite_verdict.upper()}  "
        f"({len(result.hard_reject_reasons)} hard, {len(result.soft_flags)} soft)",
        f"Mint: `{result.mint}`",
    ]
    if result.summary:
        lines.append(f"  {result.summary}")
    if result.hard_reject_reasons:
        lines.append("")
        lines.append("HARD REJECT:")
        lines.extend(f"  ❌ {r}" for r in result.hard_reject_reasons)
    if result.soft_flags:
        lines.append("")
        lines.append("SOFT FLAGS:")
        lines.extend(f"  ⚠️  {f}" for f in result.soft_flags)
    if result.signals:
        lines.append("")
        lines.append("SOURCES:")
        for src in ("helius", "jupiter", "okx", "blacklist"):
            sig = result.signals.get(src) or {}
            if not sig:
                continue
            line = _source_line(src, sig)
            if line is not None:
                lines.append(line)
    return "\n".join(lines)


def handle_command(text: str, chat_id: int, *,
                   bot_token: str, db_path: str, rpc_url: str,
                   allowed_chat_id: Optional[int] = None,
                   load_seeds: bool = True) -> Optional[str]:
    if allowed_chat_id is not None and chat_id != allowed_chat_id:
        log.warning("handle_command: ignoring chat_id=%s (allowed=%s)",
                    chat_id, allowed_chat_id)
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd in ("/start", "/help"):
        return (
            "Solana DLMM token risk screener.\n\n"
            "Usage: /check <mint_address>\n\n"
            "Sources: Helius (authority), Jupiter (organic/fees/holders), "
            "OKX (dev_rug_count/bundle), local dev blacklist.\n"
            "Reply takes 3-8 seconds (network calls).\n\n"
            f"Allowed chat: {chat_id}"
        )
    if cmd == "/check":
        if len(parts) < 2:
            return "Usage: /check <mint_address>"
        mint = parts[1].strip()
        if not (32 <= len(mint) <= 44):
            return f"Invalid mint address length ({len(mint)} chars; expected 32-44 base58)."
        try:
            result = screen(mint, rpc_url=rpc_url, db_path=db_path,
                            load_seeds=load_seeds, verbose=False)
            return format_verdict(result)
        except Exception as e:
            log.exception("screen() failed")
            return f"Error screening: {type(e).__name__}: {e}"
    return f"Unknown command: {cmd}. Try /help."


def run_bot(token: str, allowed_chat_id: int, db_path: str, rpc_url: str,
            load_seeds: bool = True, poll_timeout: int = 30) -> None:
    log.info("Bot starting (allowed_chat_id=%s, db=%s)", allowed_chat_id, db_path)
    offset: Optional[int] = None
    while True:
        try:
            updates = _api_call(token, "getUpdates", timeout=poll_timeout,
                                offset=offset, allowed_updates=["message"])
        except httpx.HTTPError as e:
            log.error("getUpdates failed: %s — retrying in 5s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id != allowed_chat_id:
                log.warning("Ignoring message from chat_id=%s (not allowed)", chat_id)
                continue
            text = msg.get("text", "")
            log.info("chat_id=%s: %s", chat_id, text[:60])
            try:
                reply = handle_command(text, chat_id, bot_token=token,
                                       db_path=db_path, rpc_url=rpc_url,
                                       allowed_chat_id=allowed_chat_id,
                                       load_seeds=load_seeds)
            except Exception as e:
                log.exception("handle_command crashed")
                reply = f"Internal error: {type(e).__name__}"
            if reply is not None:
                try:
                    send_message(token, chat_id, reply)
                except Exception as e:
                    log.error("sendMessage failed: %s", e)


def _cli() -> int:
    p = argparse.ArgumentParser(description="Telegram bot for the wallet-tracking screener")
    p.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    p.add_argument("--chat-id", type=int,
                   default=int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0") or "0"))
    p.add_argument("--db", default=os.environ.get("WALLETS_DB", "./wallets.db"))
    p.add_argument("--rpc", default=os.environ.get("HELIUS_RPC_URL", ""))
    p.add_argument("--no-load-seeds", action="store_true")
    p.add_argument("--poll-timeout", type=int, default=30)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for key, val, msg in (("token", args.token, "TELEGRAM_BOT_TOKEN"),
                          ("chat_id", args.chat_id, "TELEGRAM_ALLOWED_CHAT_ID"),
                          ("rpc", args.rpc, "HELIUS_RPC_URL")):
        if not val:
            print(f"ERROR: --{key.replace('_','-')} or {msg} is required")
            return 2

    run_bot(token=args.token, allowed_chat_id=args.chat_id, db_path=args.db,
            rpc_url=args.rpc, load_seeds=not args.no_load_seeds,
            poll_timeout=args.poll_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
