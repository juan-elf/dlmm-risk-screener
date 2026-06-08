"""Offline tests for screen.screen().

All 4 external integrations are mocked: fetch_authority (Helius),
fetch_jupiter, fetch_okx, and the DevBlacklistStore class. Each test
spins up a fresh temp SQLite DB so the store layers inside screen() can
read/write without hitting any real DB.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from dim_blacklist_dev import DevBlacklistEntry, DevBlacklistStore
from dim_token_authority import TokenAuthority
from dim_token_jup import TokenJupRecord
from dim_token_okx import TokenOkxRecord
from screen import screen


# Real Solana base58 mint (USDT) — keeps it valid for any base58 validator
# screen() or its dependencies may run.
MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Circle's USDC mint authority — used as a benign mint_authority for the
# "review with soft flags only" test (it would only be hard-rejected if it
# were in the blacklist; we don't populate one).
CIRCLE_AUTH = "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG"


def _clean_helius(mint: str = MINT) -> TokenAuthority:
    return TokenAuthority(
        token_mint=mint,
        mint_authority=None,
        freeze_authority=None,
        update_authority=None,
        is_mutable_metadata=False,
        is_token_2022=False,
        has_permanent_delegate=False,
        program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        supply=1_000_000_000,
        decimals=6,
    )


def _clean_jup(mint: str = MINT) -> TokenJupRecord:
    return TokenJupRecord(
        token_mint=mint,
        jup_id=mint,
        symbol="TKN",
        name="Token",
        decimals=6,
        organic_score=90.0,
        organic_score_label="high",
        holder_count=10_000,
        mcap_usd=5_000_000.0,
        launchpad=None,
        global_fees_sol=200.0,
        audit_mint_disabled=True,
        audit_freeze_disabled=True,
        audit_top_holders_pct=20.0,
        audit_bot_holders_pct=5.0,
        audit_dev_migrations=0,
        audit_total_holders=10_000,
        audit_risky_holders=0,
    )


def _clean_okx(mint: str = MINT) -> TokenOkxRecord:
    return TokenOkxRecord(
        token_mint=mint,
        risk_level=1,
        bundle_pct=0.0,
        sniper_pct=0.0,
        suspicious_pct=0.0,
        dev_holding_pct=0.0,
        top10_pct=20.0,
        lp_burned_pct=100.0,
        dev_rug_count=0,
        dev_token_count=1,
        is_honeypot=False,
        is_rugpull=False,
        is_wash=False,
        ath_usd=1.0,
        atl_usd=0.1,
        current_price_usd=0.5,
        price_vs_ath_pct=-50.0,
    )


class TestScreen(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)
        for sfx in ("-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def _run(self, helius, jup, okx, *, blacklist_active=False):
        patchers = [
            patch("screen.fetch_authority", return_value=helius),
            patch("screen.fetch_jupiter", return_value=jup),
            patch("screen.fetch_okx", return_value=okx),
            patch.object(DevBlacklistStore, "is_blacklisted",
                         return_value=blacklist_active),
        ]
        for p in patchers:
            p.start()
        try:
            return screen(MINT, rpc_url="http://dummy", db_path=self.db)
        finally:
            for p in patchers:
                p.stop()

    # 1
    def test_reject_for_t22_perm_delegate(self):
        helius = _clean_helius()
        helius.is_token_2022 = True
        helius.has_permanent_delegate = True
        helius.program_id = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
        result = self._run(helius, _clean_jup(), _clean_okx())
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("t22_permanent_delegate", result.hard_reject_reasons)

    # 2
    def test_reject_for_honeypot(self):
        okx = _clean_okx()
        okx.is_honeypot = True
        result = self._run(_clean_helius(), _clean_jup(), okx)
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("honeypot_or_rugpull_or_wash", result.hard_reject_reasons)

    # 3
    def test_reject_for_dev_rug_count(self):
        okx = _clean_okx()
        okx.dev_rug_count = 1
        result = self._run(_clean_helius(), _clean_jup(), okx)
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("dev_has_prior_rugs", result.hard_reject_reasons)

    # 4
    def test_reject_for_low_fees(self):
        jup = _clean_jup()
        jup.global_fees_sol = 10.0
        jup.organic_score = 80.0
        result = self._run(_clean_helius(), jup, _clean_okx())
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("low_trading_fees", result.hard_reject_reasons)

    # 5
    def test_reject_for_holder_concentration(self):
        jup = _clean_jup()
        jup.audit_top_holders_pct = 80.0
        result = self._run(_clean_helius(), jup, _clean_okx())
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("holder_concentration", result.hard_reject_reasons)

    # 6
    def test_reject_for_bundle(self):
        okx = _clean_okx()
        okx.bundle_pct = 50.0
        result = self._run(_clean_helius(), _clean_jup(), okx)
        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("bundle_detected", result.hard_reject_reasons)

    # 7
    def test_allow_for_clean_token(self):
        result = self._run(_clean_helius(), _clean_jup(), _clean_okx())
        self.assertEqual(result.composite_verdict, "allow")
        self.assertEqual(list(result.hard_reject_reasons), [])
        self.assertEqual(list(result.soft_flags), [])

    # 8
    def test_review_for_soft_flags_only(self):
        helius = _clean_helius()
        helius.mint_authority = CIRCLE_AUTH
        jup = _clean_jup()
        jup.organic_score = 80.0
        # Keep Jupiter's audit consistent with the on-chain mint_authority
        # actually being present, so the soft signal is unambiguous.
        jup.audit_mint_disabled = False
        result = self._run(helius, jup, _clean_okx())
        self.assertEqual(result.composite_verdict, "review")
        self.assertIn("unrenounced_mint_authority", result.soft_flags)

    # 9
    def test_caution_when_data_missing(self):
        patchers = [
            patch("screen.fetch_authority", return_value=_clean_helius()),
            patch("screen.fetch_jupiter", side_effect=RuntimeError("jup boom")),
            patch("screen.fetch_okx", side_effect=RuntimeError("okx boom")),
            patch.object(DevBlacklistStore, "is_blacklisted", return_value=False),
        ]
        for p in patchers:
            p.start()
        try:
            result = screen(MINT, rpc_url="http://dummy", db_path=self.db)
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(result.composite_verdict, "caution")

    # 10
    def test_dev_blacklist_short_circuits(self):
        # Pre-populate the real blacklist on the temp DB so screen()'s
        # internal DevBlacklistStore sees it.
        store = DevBlacklistStore(self.db)
        try:
            store.add_entry(DevBlacklistEntry(
                dev_wallet=CIRCLE_AUTH,
                reason="manual",
                evidence_source="manual",
                notes="test fixture",
            ))
        finally:
            store.close()

        helius = _clean_helius()
        helius.mint_authority = CIRCLE_AUTH

        jup_mock = MagicMock(return_value=_clean_jup())
        okx_mock = MagicMock(return_value=_clean_okx())

        patchers = [
            patch("screen.fetch_authority", return_value=helius),
            patch("screen.fetch_jupiter", jup_mock),
            patch("screen.fetch_okx", okx_mock),
        ]
        for p in patchers:
            p.start()
        try:
            result = screen(MINT, rpc_url="http://dummy", db_path=self.db)
        finally:
            for p in patchers:
                p.stop()

        self.assertEqual(result.composite_verdict, "reject")
        self.assertIn("dev_blacklisted", result.hard_reject_reasons)
        self.assertEqual(jup_mock.call_count, 0,
                         "fetch_jupiter must not run when dev is blacklisted")
        self.assertEqual(okx_mock.call_count, 0,
                         "fetch_okx must not run when dev is blacklisted")


if __name__ == "__main__":
    unittest.main()
