"""
End-to-end CLI tests for dim_blacklist_dev. Driven via subprocess against a
temp SQLite file — no network needed.

Mirrors the subprocess style of test_cli_mock_rpc.py.

Run:
    .venv/bin/python -m unittest test_dim_blacklist_dev_cli -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Real-looking base58 wallets (length 32-44, no 0/O/I/l).
SCAM_DEV  = "ScamDev1111111111111111111111111111111111aB"
SCAM_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
UNKNOWN_DEV = "UnknownDev99999999999999999999999999999aB"


class TestBlacklistCli(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name

    def tearDown(self):
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def _run(self, *extra_args) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "dim_blacklist_dev.py", "--db", self.db, *extra_args]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=Path(__file__).parent, timeout=15,
        )

    # ------------------------------------------------------------------------

    def test_add_then_list(self):
        p = self._run(
            "add", "--wallet", SCAM_DEV, "--reason", "serial_rugger",
            "--evidence-mint", SCAM_MINT,
            "--evidence-source", "okx_advanced_info",
            "--dev-rug-count", "3",
            "--notes", "3 prior rugs",
            "--added-by", "auto",
        )
        self.assertEqual(p.returncode, 0,
                         f"add failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
        self.assertIn(f"Added: {SCAM_DEV}", p.stdout)
        self.assertIn("reason=serial_rugger", p.stdout)
        self.assertIn("active_count: 1", p.stdout)

        p = self._run("list")
        self.assertEqual(p.returncode, 0,
                         f"list failed:\nSTDERR:\n{p.stderr}")
        self.assertIn("active_count: 1", p.stdout)
        self.assertIn(SCAM_DEV, p.stdout)
        self.assertIn("reason=serial_rugger", p.stdout)
        self.assertIn(f"evidence_mint={SCAM_MINT}", p.stdout)

    def test_check_blacklisted_wallet(self):
        self._run("add", "--wallet", SCAM_DEV, "--reason", "scam",
                  "--evidence-mint", SCAM_MINT)
        p = self._run("check", "--wallet", SCAM_DEV)
        self.assertEqual(p.returncode, 0,
                         f"check failed:\nSTDERR:\n{p.stderr}")
        self.assertIn("blacklisted: True", p.stdout)
        self.assertIn("reason: scam", p.stdout)
        self.assertIn(f"evidence_mint: {SCAM_MINT}", p.stdout)

    def test_check_unknown_wallet_returns_false(self):
        p = self._run("check", "--wallet", UNKNOWN_DEV)
        self.assertEqual(p.returncode, 0,
                         f"check failed:\nSTDERR:\n{p.stderr}")
        self.assertIn("blacklisted: False", p.stdout)
        # No reason / evidence lines when not blacklisted
        self.assertNotIn("reason:", p.stdout)

    def test_deactivate_then_check_returns_false(self):
        self._run("add", "--wallet", SCAM_DEV, "--reason", "rug_pull")
        p = self._run("check", "--wallet", SCAM_DEV)
        self.assertIn("blacklisted: True", p.stdout)

        p = self._run("deactivate", "--wallet", SCAM_DEV)
        self.assertEqual(p.returncode, 0,
                         f"deactivate failed:\nSTDERR:\n{p.stderr}")
        self.assertIn(f"Deactivated: {SCAM_DEV}", p.stdout)
        self.assertIn("active_count: 0", p.stdout)

        p = self._run("check", "--wallet", SCAM_DEV)
        self.assertIn("blacklisted: False", p.stdout)

        # And list should be empty
        p = self._run("list")
        self.assertIn("active_count: 0", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
