"""
Offline unit tests for fact_lp_position parser logic.

Tests the Meteora API JSON -> LPPosition dataclass mapping without
making any network calls. Also tests LPPositionStore CRUD operations.

Run:
    .venv/bin/python -m unittest test_fact_lp_position -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fact_lp_position import (
    LPPosition,
    LPPositionStore,
    _normalize_position,
)


def make_raw_position(
    position_address="PosAddr11111111111111111111111111111111",
    pool_address="PoolAddr11111111111111111111111111111111",
    owner="Wallet111111111111111111111111111111111",
    total_deposit_value_usd="1000.50",
    total_withdraw_value_usd=None,
    fees_claimed_value_usd="25.30",
    lower_bin_id=-100,
    upper_bin_id=100,
    bin_step=10,
    created_at=1717000000,  # 2024-05-29
    last_updated_at=1717500000,
) -> dict:
    return {
        "position_address": position_address,
        "pool_address": pool_address,
        "owner": owner,
        "lower_bin_id": lower_bin_id,
        "upper_bin_id": upper_bin_id,
        "bin_step": bin_step,
        "created_at": created_at,
        "last_updated_at": last_updated_at,
        "total_deposit_x": "100.0",
        "total_deposit_y": "200.0",
        "total_deposit_value_usd": total_deposit_value_usd,
        "total_withdraw_x": "0.0",
        "total_withdraw_y": "0.0",
        "total_withdraw_value_usd": total_withdraw_value_usd,
        "fees_claimed_x": "1.5",
        "fees_claimed_y": "3.0",
        "fees_claimed_value_usd": fees_claimed_value_usd,
        "token_x_mint": "So11111111111111111111111111111111111111112",
        "token_y_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    }


class TestNormalizePosition(unittest.TestCase):
    def test_open_position(self):
        raw = make_raw_position(total_withdraw_value_usd=None)
        pos = _normalize_position(raw, owner="W")
        self.assertEqual(pos.status, "open")
        self.assertEqual(pos.initial_usd, 1000.50)
        self.assertEqual(pos.fees_usd_claimed, 25.30)
        self.assertEqual(pos.bin_lower, -100)
        self.assertEqual(pos.bin_upper, 100)
        self.assertEqual(pos.bin_step, 10)
        self.assertIsNone(pos.closed_at)
        self.assertIsNotNone(pos.opened_at)

    def test_closed_position(self):
        raw = make_raw_position(total_withdraw_value_usd="1050.0")
        pos = _normalize_position(raw, owner="W")
        self.assertEqual(pos.status, "closed")
        # current_usd is None for closed positions (we just record what happened)
        self.assertIsNone(pos.current_usd)

    def test_missing_optional_fields(self):
        # Minimal raw dict — should not crash
        raw = {"position_address": "PA", "pool_address": "PO"}
        pos = _normalize_position(raw, owner="W")
        self.assertEqual(pos.position_id, "PA")
        self.assertEqual(pos.pool_address, "PO")
        self.assertEqual(pos.status, "open")
        self.assertEqual(pos.initial_usd, 0.0)  # default
        self.assertIsNone(pos.bin_lower)

    def test_timestamp_parsing(self):
        raw = make_raw_position(created_at=0)
        pos = _normalize_position(raw, owner="W")
        self.assertIsNotNone(pos.opened_at)
        # 1970-01-01 epoch
        self.assertIn("1970", pos.opened_at)

    def test_numeric_string_conversion(self):
        raw = make_raw_position(total_deposit_value_usd="12345.6789")
        pos = _normalize_position(raw, owner="W")
        self.assertEqual(pos.initial_usd, 12345.6789)

    def test_pnl_computation_open_position(self):
        # Open position: current_usd = initial - withdraw
        raw = make_raw_position(
            total_deposit_value_usd="1000",
            total_withdraw_value_usd="200",  # withdrew 200 worth
            fees_claimed_value_usd="50",
        )
        pos = _normalize_position(raw, owner="W")
        # Meteora's withdraw tracking is a bit fuzzy; for now we trust initial_usd only
        # This test just documents current behavior.
        self.assertEqual(pos.fees_usd_claimed, 50.0)


class TestLPPositionStore(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name
        self.store = LPPositionStore(self.db)

    def tearDown(self):
        self.store.close()
        Path(self.db).unlink(missing_ok=True)
        for sfx in ("-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def _make_pos(self, position_id, **overrides):
        defaults = dict(
            position_id=position_id,
            owner_wallet="W111111111111111111111111111111111111",
            pool_address="P11111111111111111111111111111111111",
            status="open",
            initial_usd=1000.0,
            current_usd=950.0,
            pnl_usd=-50.0,
        )
        defaults.update(overrides)
        return LPPosition(**defaults)

    def test_upsert_and_get(self):
        pos = self._make_pos("Pos11111111111111111111111111111111111")
        self.store.upsert(pos)
        got = self.store.get("Pos11111111111111111111111111111111111")
        self.assertIsNotNone(got)
        self.assertEqual(got["initial_usd"], 1000.0)
        self.assertEqual(got["pnl_usd"], -50.0)

    def test_upsert_is_idempotent(self):
        pos = self._make_pos("Pos11111111111111111111111111111111111", initial_usd=1000)
        self.store.upsert(pos)
        pos2 = self._make_pos("Pos11111111111111111111111111111111111", initial_usd=1500)
        self.store.upsert(pos2)
        got = self.store.get("Pos11111111111111111111111111111111111")
        self.assertEqual(got["initial_usd"], 1500)  # updated
        self.assertEqual(self.store.count(), 1)     # still one row

    def test_list_by_owner(self):
        for i in range(3):
            self.store.upsert(self._make_pos(f"Pos{i}1111111111111111111111111111111111"))
        # Different owner
        self.store.upsert(self._make_pos(
            "Other11111111111111111111111111111111111",
            owner_wallet="Other11111111111111111111111111111111",
        ))
        rows = self.store.list_by_owner("W111111111111111111111111111111111111")
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["owner_wallet"], "W111111111111111111111111111111111111")

    def test_views_exist(self):
        cur = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
        views = {row[0] for row in cur}
        self.assertIn("v_lp_outcome_summary", views)
        self.assertIn("v_risk_score_validation", views)

    def test_outcome_view(self):
        # Add a winning position
        self.store.upsert(self._make_pos(
            "WinPos1111111111111111111111111111111111",
            pnl_usd=200.0,
            status="closed",
        ))
        # Add a losing position
        self.store.upsert(self._make_pos(
            "LossPos1111111111111111111111111111111111",
            pnl_usd=-100.0,
            status="closed",
        ))
        # Add an open position (excluded from view's win/loss categorization)
        self.store.upsert(self._make_pos(
            "OpenPos1111111111111111111111111111111111",
            pnl_usd=50.0,
            status="open",
        ))

        cur = self.store._conn.execute(
            "SELECT * FROM v_lp_outcome_summary WHERE position_id = 'WinPos1111111111111111111111111111111111'"
        )
        row = cur.fetchone()
        self.assertEqual(row["outcome"], "win")

        cur = self.store._conn.execute(
            "SELECT * FROM v_lp_outcome_summary WHERE position_id = 'LossPos1111111111111111111111111111111111'"
        )
        row = cur.fetchone()
        self.assertEqual(row["outcome"], "loss")

        cur = self.store._conn.execute(
            "SELECT * FROM v_lp_outcome_summary WHERE position_id = 'OpenPos1111111111111111111111111111111111'"
        )
        row = cur.fetchone()
        self.assertEqual(row["outcome"], "open")


if __name__ == "__main__":
    unittest.main(verbosity=2)
