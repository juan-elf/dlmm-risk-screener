import unittest
from unittest.mock import patch, MagicMock

import telegram_bot
from telegram_bot import format_verdict, handle_command, send_message
from screen import ScreenResult


def _make_result(verdict="allow", hard=None, soft=None, signals=None,
                 mint="So11111111111111111111111111111111111111112",
                 summary="no issues detected"):
    return ScreenResult(
        mint=mint,
        composite_verdict=verdict,
        summary=summary,
        hard_reject_reasons=hard or [],
        soft_flags=soft or [],
        signals=signals or {},
        fetched_at="2026-06-07T00:00:00+00:00",
    )


_SAMPLE_SIGNALS = {
    "helius": {
        "mint_authority": None, "freeze_authority": None,
        "is_token_2022": False, "has_permanent_delegate": False,
        "raw_verdict": "safe", "effective_verdict": "safe",
        "mint_authority_entity": "Circle", "error": None,
    },
    "jupiter": {
        "organic_score": 85, "global_fees_sol": 120.5,
        "audit_top_holders_pct": 22.4, "error": None,
    },
    "okx": {
        "risk_level": 1, "dev_rug_count": 0, "bundle_pct": 2.1,
        "is_honeypot": False, "is_rugpull": False, "is_wash": False,
        "error": None,
    },
    "blacklist": {
        "dev_wallet": None, "is_blacklisted": False, "reason": "not in list",
    },
}


class FormatVerdictTests(unittest.TestCase):

    def test_format_verdict_allow(self):
        r = _make_result(verdict="allow", signals=_SAMPLE_SIGNALS)
        out = format_verdict(r)
        self.assertIn("VERDICT: ALLOW", out)
        self.assertIn("✅", out)
        self.assertIn("(0 hard, 0 soft)", out)
        self.assertIn("[helius] safe → safe (entity: Circle)", out)
        self.assertIn("[jupiter] organic=85", out)
        self.assertIn("[okx] risk=1", out)
        self.assertIn("[blacklist] clear", out)
        self.assertNotIn("HARD REJECT:", out)
        self.assertNotIn("SOFT FLAGS:", out)

    def test_format_verdict_reject_with_hard_reasons(self):
        bl_signals = dict(_SAMPLE_SIGNALS)
        bl_signals["blacklist"] = {
            "dev_wallet": "FakeDevWallet", "is_blacklisted": True,
            "reason": "prior rug",
        }
        r = _make_result(
            verdict="reject",
            hard=["dev_blacklisted", "honeypot_or_rugpull_or_wash"],
            signals=bl_signals,
            summary="dev_blacklisted; honeypot_or_rugpull_or_wash",
        )
        out = format_verdict(r)
        self.assertIn("VERDICT: REJECT", out)
        self.assertIn("❌", out)
        self.assertIn("(2 hard, 0 soft)", out)
        self.assertIn("HARD REJECT:", out)
        self.assertIn("dev_blacklisted", out)
        self.assertIn("honeypot_or_rugpull_or_wash", out)
        self.assertIn("[blacklist] ⚠️  HIT: prior rug", out)

    def test_format_verdict_review_with_soft_flags(self):
        r = _make_result(
            verdict="review",
            soft=["unrenounced_mint_authority", "low_organic_score"],
            signals=_SAMPLE_SIGNALS,
            summary="unrenounced_mint_authority; low_organic_score",
        )
        out = format_verdict(r)
        self.assertIn("VERDICT: REVIEW", out)
        self.assertIn("⚠️", out)
        self.assertIn("(0 hard, 2 soft)", out)
        self.assertIn("SOFT FLAGS:", out)
        self.assertIn("unrenounced_mint_authority", out)
        self.assertIn("low_organic_score", out)
        self.assertNotIn("HARD REJECT:", out)

    def test_format_verdict_handles_source_errors(self):
        sig = {
            "helius": {"error": "rpc_timeout"},
            "jupiter": {"error": "not_found"},
            "okx": {"error": "503"},
            "blacklist": {"is_blacklisted": False, "reason": "not in list"},
        }
        r = _make_result(verdict="caution", soft=["insufficient_data"],
                         signals=sig, summary="insufficient_data")
        out = format_verdict(r)
        self.assertIn("[helius] error: rpc_timeout", out)
        self.assertIn("[jupiter] error: not_found", out)
        self.assertIn("[okx] error: 503", out)
        self.assertIn("[blacklist] clear", out)


class SendMessageTests(unittest.TestCase):

    def test_format_verdict_truncates_long_messages(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"ok": True, "result": {}}
            return resp

        long_text = "x" * 5000
        with patch.object(telegram_bot.httpx, "post", side_effect=fake_post):
            send_message("FAKE_TOKEN", 12345, long_text)

        sent = captured["json"]["text"]
        self.assertLess(len(sent), 5000)
        self.assertLessEqual(len(sent), 4002)
        self.assertTrue(sent.endswith("[truncated]"))


class HandleCommandTests(unittest.TestCase):

    def test_handle_command_check_invokes_screen(self):
        mint = "So11111111111111111111111111111111111111112"
        fake_result = _make_result(verdict="allow", signals=_SAMPLE_SIGNALS)

        with patch.object(telegram_bot, "screen", return_value=fake_result) as mock_screen:
            reply = handle_command(
                f"/check {mint}", chat_id=42,
                bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
                allowed_chat_id=42, load_seeds=False,
            )

        mock_screen.assert_called_once()
        _args, kwargs = mock_screen.call_args
        self.assertEqual(mock_screen.call_args.args[0], mint)
        self.assertEqual(kwargs["rpc_url"], "http://rpc")
        self.assertEqual(kwargs["db_path"], "/tmp/x.db")
        self.assertFalse(kwargs["load_seeds"])
        self.assertIn("VERDICT: ALLOW", reply)

    def test_handle_command_rejects_unauthorized_chat_id(self):
        with patch.object(telegram_bot, "screen") as mock_screen:
            reply = handle_command(
                "/check So11111111111111111111111111111111111111112",
                chat_id=999,
                bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
                allowed_chat_id=42,
            )
        self.assertIsNone(reply)
        mock_screen.assert_not_called()

    def test_handle_command_help_returns_help_text(self):
        reply = handle_command(
            "/help", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertIsNotNone(reply)
        self.assertIn("/check <mint_address>", reply)
        self.assertIn("Allowed chat: 42", reply)

    def test_handle_command_start_returns_help_text(self):
        reply = handle_command(
            "/start", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertIsNotNone(reply)
        self.assertIn("Solana DLMM token risk screener", reply)

    def test_handle_command_unknown_returns_error(self):
        reply = handle_command(
            "/foobar", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertIsNotNone(reply)
        self.assertIn("Unknown command", reply)
        self.assertIn("/foobar", reply)

    def test_handle_command_non_slash_ignored(self):
        reply = handle_command(
            "hello there", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertIsNone(reply)

    def test_handle_command_check_without_mint(self):
        reply = handle_command(
            "/check", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertEqual(reply, "Usage: /check <mint_address>")

    def test_handle_command_check_rejects_bad_mint_length(self):
        reply = handle_command(
            "/check tooShort", chat_id=42,
            bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
            allowed_chat_id=42,
        )
        self.assertIn("Invalid mint address length", reply)

    def test_handle_command_check_screen_exception_returns_error(self):
        with patch.object(telegram_bot, "screen", side_effect=RuntimeError("boom")):
            reply = handle_command(
                "/check So11111111111111111111111111111111111111112",
                chat_id=42,
                bot_token="t", db_path="/tmp/x.db", rpc_url="http://rpc",
                allowed_chat_id=42,
            )
        self.assertIn("Error screening", reply)
        self.assertIn("RuntimeError", reply)
        self.assertIn("boom", reply)


if __name__ == "__main__":
    unittest.main()
