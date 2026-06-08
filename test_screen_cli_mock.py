"""
End-to-end subprocess tests for screen.py CLI.

Mirrors the style of test_dim_token_jup_cli_mock.py but cannot use a local
mock HTTP server because screen.py does not expose --api-jupiter / --api-okx
flags — only --rpc. So these tests run the real CLI against the live Helius
RPC (when HELIUS_RPC_URL is set) and verify only the CLI plumbing:

  - Test 1: known clean mint (USDC) — verdict is not REJECT.
  - Test 2: blacklisted dev — pre-populating dim_blacklist_dev with USDC's
    real mint authority forces a REJECT via the dev-blacklist short-circuit.

Both tests skip cleanly if HELIUS_RPC_URL is not set in the environment.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Circle's USDC mint authority — a well-known on-chain constant. Used here
# only to seed a fake blacklist entry so the screen.py CLI rejects USDC.
USDC_MINT_AUTHORITY = "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG"


class TestScreenCli(unittest.TestCase):
    def setUp(self) -> None:
        self.rpc = os.environ.get("HELIUS_RPC_URL", "")
        if not self.rpc:
            self.skipTest("HELIUS_RPC_URL not set")
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name

    def tearDown(self) -> None:
        if hasattr(self, "db"):
            for sfx in ("", "-wal", "-shm", "-journal"):
                Path(self.db + sfx).unlink(missing_ok=True)

    def _run_cli(self, mint: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "screen.py",
            "--mint", mint, "--db", self.db, "--rpc", self.rpc,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=Path(__file__).parent, timeout=60,
        )

    def test_screen_known_clean_mint_via_subprocess(self):
        # Pre-load the whitelist so USDC's authority resolves to a known issuer.
        from dim_token_authority import TokenAuthorityStore
        from seed_address_labels import get_seed
        TokenAuthorityStore(self.db).load_address_labels(get_seed())

        p = self._run_cli(USDC_MINT)
        self.assertEqual(
            p.returncode, 0,
            f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}",
        )
        self.assertIn("COMPOSITE VERDICT:", p.stdout)
        # USDC is whitelisted — must not be rejected. We look at just the
        # verdict line to avoid false positives from elsewhere in the banner.
        verdict_line = (
            p.stdout.split("COMPOSITE VERDICT:", 1)[1].split("\n", 1)[0]
        )
        self.assertNotIn("REJECT", verdict_line.upper())

    def test_screen_blacklisted_dev_via_subprocess(self):
        # Pre-blacklist Circle's USDC mint authority. The Helius fetch still
        # runs first to populate dim_token_authority, but the dev-blacklist
        # check then short-circuits Jupiter/OKX with a hard REJECT.
        from dim_blacklist_dev import DevBlacklistStore, DevBlacklistEntry
        store = DevBlacklistStore(self.db)
        store.add_entry(DevBlacklistEntry(
            dev_wallet=USDC_MINT_AUTHORITY,
            reason="rug_pull",
            evidence_mint=USDC_MINT,
            evidence_source="manual",
            notes="TEST: not real, just for E2E test",
        ))

        p = self._run_cli(USDC_MINT)
        self.assertEqual(
            p.returncode, 0,
            f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}",
        )
        self.assertIn("REJECT", p.stdout)
        self.assertIn("dev_blacklisted", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
