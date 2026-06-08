"""
Offline unit tests for dim_blacklist_dev (DevBlacklistStore + dataclass + CLI
validation). No network. Uses a temp SQLite file per test.

Run:
    .venv/bin/python -m unittest test_dim_blacklist_dev -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dim_blacklist_dev import DevBlacklistEntry, DevBlacklistStore


# Real-looking base58 strings (32-44 chars, no 0/O/I/l).
SCAM_DEV  = "ScamDev1111111111111111111111111111111111aB"
SCAM_DEV2 = "ScamDev2222222222222222222222222222222222cD"
SCAM_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
UNKNOWN_DEV = "UnknownDev99999999999999999999999999999aB"


class TestDevBlacklistStore(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name
        self.store = DevBlacklistStore(self.db)

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    # --- schema --------------------------------------------------------------

    def test_schema_init(self):
        """_init_schema should create the table + index + view idempotently."""
        cur = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dim_blacklist_dev'"
        )
        self.assertIsNotNone(cur.fetchone())
        cur = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_dev_blacklist_active'"
        )
        self.assertIsNotNone(cur.fetchone())
        # Re-init should be a no-op
        self.store._init_schema()
        self.assertEqual(self.store.count_active(), 0)

    # --- add / get -----------------------------------------------------------

    def test_add_and_get_round_trip(self):
        entry = DevBlacklistEntry(
            dev_wallet=SCAM_DEV,
            reason="serial_rugger",
            evidence_mint=SCAM_MINT,
            evidence_source="okx_advanced_info",
            dev_rug_count_at_time=3,
            notes="3 prior rugs per OKX",
            added_by="auto",
        )
        self.store.add_entry(entry)
        row = self.store.get(SCAM_DEV)
        self.assertIsNotNone(row)
        self.assertEqual(row["dev_wallet"], SCAM_DEV)
        self.assertEqual(row["reason"], "serial_rugger")
        self.assertEqual(row["evidence_mint"], SCAM_MINT)
        self.assertEqual(row["evidence_source"], "okx_advanced_info")
        self.assertEqual(row["dev_rug_count_at_time"], 3)
        self.assertEqual(row["notes"], "3 prior rugs per OKX")
        self.assertEqual(row["added_by"], "auto")
        self.assertEqual(row["active"], 1)

    def test_add_is_idempotent(self):
        """ON CONFLICT DO UPDATE — re-adding the same wallet overwrites."""
        self.store.add_entry(DevBlacklistEntry(
            dev_wallet=SCAM_DEV, reason="rug_pull", notes="first",
        ))
        self.store.add_entry(DevBlacklistEntry(
            dev_wallet=SCAM_DEV, reason="serial_rugger", notes="updated",
            dev_rug_count_at_time=5, added_by="human-reviewer",
        ))
        self.assertEqual(self.store.count_active(), 1)
        row = self.store.get(SCAM_DEV)
        self.assertEqual(row["reason"], "serial_rugger")
        self.assertEqual(row["notes"], "updated")
        self.assertEqual(row["dev_rug_count_at_time"], 5)
        self.assertEqual(row["added_by"], "human-reviewer")

    # --- is_blacklisted ------------------------------------------------------

    def test_is_blacklisted_true_when_active(self):
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV, reason="scam"))
        self.assertTrue(self.store.is_blacklisted(SCAM_DEV))

    def test_is_blacklisted_false_when_inactive(self):
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV, reason="scam"))
        self.assertTrue(self.store.is_blacklisted(SCAM_DEV))
        self.store.deactivate(SCAM_DEV)
        self.assertFalse(self.store.is_blacklisted(SCAM_DEV))
        # get() still returns the row (with active=0); only the view filters it
        row = self.store.get(SCAM_DEV)
        self.assertIsNotNone(row)
        self.assertEqual(row["active"], 0)

    def test_is_blacklisted_false_for_unknown(self):
        self.assertFalse(self.store.is_blacklisted(UNKNOWN_DEV))
        self.assertIsNone(self.store.get(UNKNOWN_DEV))

    # --- list ----------------------------------------------------------------

    def test_list_active_excludes_inactive(self):
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV,  reason="rug_pull"))
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV2, reason="serial_rugger"))
        self.assertEqual(len(self.store.list_active()), 2)
        self.store.deactivate(SCAM_DEV2)
        active = self.store.list_active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["dev_wallet"], SCAM_DEV)

    def test_deactivate(self):
        """deactivate is a no-op for an unknown wallet, and idempotent."""
        # No-op on unknown
        self.store.deactivate(UNKNOWN_DEV)
        self.assertEqual(self.store.count_active(), 0)
        # Add then deactivate twice
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV, reason="scam"))
        self.assertEqual(self.store.count_active(), 1)
        self.store.deactivate(SCAM_DEV)
        self.assertEqual(self.store.count_active(), 0)
        self.store.deactivate(SCAM_DEV)
        self.assertEqual(self.store.count_active(), 0)

    def test_count_active(self):
        self.assertEqual(self.store.count_active(), 0)
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV,  reason="rug_pull"))
        self.assertEqual(self.store.count_active(), 1)
        self.store.add_entry(DevBlacklistEntry(dev_wallet=SCAM_DEV2, reason="manual"))
        self.assertEqual(self.store.count_active(), 2)
        self.store.deactivate(SCAM_DEV)
        self.assertEqual(self.store.count_active(), 1)


class TestCliValidation(unittest.TestCase):
    """The CLI must reject bad input before touching the DB."""

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

    def test_invalid_reason_rejected(self):
        """argparse choices= must reject an unknown reason."""
        p = self._run("add", "--wallet", SCAM_DEV, "--reason", "totally_made_up")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("invalid choice", p.stderr)

    def test_invalid_wallet_format_rejected(self):
        """Wallet length / charset is validated before insert."""
        p = self._run("add", "--wallet", "tooshort", "--reason", "scam")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ERROR", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
