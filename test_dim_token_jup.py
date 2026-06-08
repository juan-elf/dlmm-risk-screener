"""
Offline unit tests for dim_token_jup parser + store.

These tests do NOT call the network — they exercise the JSON parser against
hand-crafted Jupiter-shaped payloads, and verify the SQLite schema/view round
trips correctly.

Run:
    .venv/bin/python -m unittest test_dim_token_jup -v
or
    .venv/bin/python test_dim_token_jup.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dim_token_jup import (
    TokenJupRecord,
    TokenJupStore,
    _parse_jupiter_response,
    _normalize_search_response,
)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
UNKNOWN_MINT = "NotFoundMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


def _usdc_asset() -> dict:
    """Realistic-looking Jupiter asset entry for USDC."""
    return {
        "id": USDC_MINT,
        "name": "USD Coin",
        "symbol": "USDC",
        "decimals": 6,
        "icon": "https://example/usdc.png",
        "organicScore": 95.5,
        "organicScoreLabel": "high",
        "audit": {
            "mintAuthorityDisabled": False,  # Circle keeps it
            "freezeAuthorityDisabled": False,
            "topHoldersPercentage": 18.4,
            "botHoldersPercentage": 4.2,
            "devMigrations": 0,
            "totalHolders": 1234567,
            "riskyHolders": 12,
        },
        "holderCount": 1234567,
        "mcap": 32_500_000_000,
        "launchpad": None,
        "fees": 12345.67,
    }


def _bonk_asset_with_envelope_data() -> dict:
    """Same shape but wrapped in {"data": [...]}."""
    return {
        "data": [
            {
                "id": BONK_MINT,
                "name": "Bonk",
                "symbol": "BONK",
                "decimals": 5,
                "organicScore": 72.1,
                "organicScoreLabel": "medium",
                "audit": {
                    "mintAuthorityDisabled": True,
                    "freezeAuthorityDisabled": True,
                    "topHoldersPercentage": 24.0,
                    "botHoldersPercentage": 8.5,
                    "devMigrations": 2,
                    "totalHolders": 800_000,
                    "riskyHolders": 250,
                },
                "holderCount": 800_000,
                "mcap": 1_900_000_000,
                "launchpad": "pump.fun",
                "fees": 543.21,
            }
        ]
    }


# ---------------------------------------------------------------------------
# TestParseJupiter
# ---------------------------------------------------------------------------

class TestParseJupiter(unittest.TestCase):
    def test_parse_realistic_response_with_envelope(self):
        body = {"data": [_usdc_asset()]}
        rec = _parse_jupiter_response(USDC_MINT, body)
        self.assertEqual(rec.token_mint, USDC_MINT)
        self.assertEqual(rec.symbol, "USDC")
        self.assertEqual(rec.name, "USD Coin")
        self.assertEqual(rec.decimals, 6)
        self.assertEqual(rec.organic_score, 95.5)
        self.assertEqual(rec.organic_score_label, "high")
        self.assertEqual(rec.holder_count, 1_234_567)
        self.assertEqual(rec.mcap_usd, 32_500_000_000)
        self.assertIsNone(rec.launchpad)
        self.assertEqual(rec.global_fees_sol, 12345.67)
        self.assertEqual(rec.audit_mint_disabled, False)
        self.assertEqual(rec.audit_freeze_disabled, False)
        self.assertEqual(rec.audit_top_holders_pct, 18.4)
        self.assertEqual(rec.audit_bot_holders_pct, 4.2)
        self.assertEqual(rec.audit_dev_migrations, 0)
        self.assertEqual(rec.audit_total_holders, 1_234_567)
        self.assertEqual(rec.audit_risky_holders, 12)
        self.assertTrue(rec.last_seen_chain)
        self.assertIsNone(rec.error)
        self.assertIsNotNone(rec.raw_json)

    def test_parse_mint_not_found(self):
        # USDC asset present but we asked for an unknown mint
        body = {"data": [_usdc_asset()]}
        rec = _parse_jupiter_response(UNKNOWN_MINT, body)
        self.assertEqual(rec.token_mint, UNKNOWN_MINT)
        self.assertEqual(rec.error, "not_found")
        self.assertFalse(rec.last_seen_chain)
        self.assertIsNone(rec.symbol)
        self.assertIsNone(rec.global_fees_sol)

    def test_parse_envelope_data_shape(self):
        body = _bonk_asset_with_envelope_data()
        rec = _parse_jupiter_response(BONK_MINT, body)
        self.assertEqual(rec.symbol, "BONK")
        self.assertEqual(rec.launchpad, "pump.fun")
        self.assertEqual(rec.global_fees_sol, 543.21)
        self.assertEqual(rec.audit_dev_migrations, 2)

    def test_parse_bare_list_shape(self):
        body = [_usdc_asset()]
        rec = _parse_jupiter_response(USDC_MINT, body)
        self.assertEqual(rec.symbol, "USDC")
        self.assertEqual(rec.organic_score, 95.5)

    def test_parse_results_envelope_shape(self):
        body = {"results": [_usdc_asset()]}
        rec = _parse_jupiter_response(USDC_MINT, body)
        self.assertEqual(rec.symbol, "USDC")

    def test_parse_malformed_body_returns_not_found(self):
        # Body is a string instead of dict/list — defensive parser shouldn't crash.
        rec = _parse_jupiter_response(USDC_MINT, "garbage_string")
        self.assertEqual(rec.error, "not_found")
        self.assertFalse(rec.last_seen_chain)

    def test_parse_empty_dict_returns_not_found(self):
        rec = _parse_jupiter_response(USDC_MINT, {})
        self.assertEqual(rec.error, "not_found")

    def test_parse_none_body_returns_not_found(self):
        rec = _parse_jupiter_response(USDC_MINT, None)
        self.assertEqual(rec.error, "not_found")
        self.assertFalse(rec.last_seen_chain)

    def test_all_nullable_fields_none_when_missing(self):
        # Bare-minimum asset: just the id, nothing else.
        body = [{"id": USDC_MINT}]
        rec = _parse_jupiter_response(USDC_MINT, body)
        self.assertIsNone(rec.symbol)
        self.assertIsNone(rec.name)
        self.assertIsNone(rec.decimals)
        self.assertIsNone(rec.organic_score)
        self.assertIsNone(rec.organic_score_label)
        self.assertIsNone(rec.holder_count)
        self.assertIsNone(rec.mcap_usd)
        self.assertIsNone(rec.launchpad)
        self.assertIsNone(rec.global_fees_sol)
        self.assertIsNone(rec.audit_mint_disabled)
        self.assertIsNone(rec.audit_freeze_disabled)
        self.assertIsNone(rec.audit_top_holders_pct)
        self.assertIsNone(rec.audit_bot_holders_pct)
        self.assertIsNone(rec.audit_dev_migrations)
        self.assertIsNone(rec.audit_total_holders)
        self.assertIsNone(rec.audit_risky_holders)
        # But last_seen_chain stays True — we DID find the asset.
        self.assertTrue(rec.last_seen_chain)
        self.assertIsNone(rec.error)

    def test_global_fees_sol_from_audit_fallback(self):
        # Some response variants nest fees under audit
        asset = _usdc_asset()
        asset.pop("fees")
        asset["audit"]["fees"] = 999.0
        body = [asset]
        rec = _parse_jupiter_response(USDC_MINT, body)
        self.assertEqual(rec.global_fees_sol, 999.0)

    def test_global_fees_sol_top_level_takes_precedence(self):
        asset = _usdc_asset()
        asset["fees"] = 100.0
        asset["audit"]["fees"] = 999.0
        rec = _parse_jupiter_response(USDC_MINT, [asset])
        self.assertEqual(rec.global_fees_sol, 100.0)

    def test_match_by_token_address_field(self):
        # Older response variants may use tokenAddress instead of id
        asset = _usdc_asset()
        del asset["id"]
        asset["tokenAddress"] = USDC_MINT
        rec = _parse_jupiter_response(USDC_MINT, [asset])
        self.assertEqual(rec.symbol, "USDC")
        self.assertEqual(rec.jup_id, USDC_MINT)

    def test_normalize_picks_correct_entry_among_many(self):
        body = {
            "data": [
                {"id": "OtherMint1", "symbol": "X"},
                _usdc_asset(),
                {"id": "OtherMint2", "symbol": "Y"},
            ]
        }
        asset = _normalize_search_response(body, USDC_MINT)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["symbol"], "USDC")


# ---------------------------------------------------------------------------
# TestTokenJupStore
# ---------------------------------------------------------------------------

class TestTokenJupStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.store = TokenJupStore(self.db_path)

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db_path + sfx).unlink(missing_ok=True)

    def test_init_schema_applies_cleanly(self):
        # Re-opening should be idempotent (CREATE TABLE IF NOT EXISTS).
        self.store.close()
        self.store = TokenJupStore(self.db_path)
        self.assertEqual(self.store.count(), 0)

    def test_upsert_and_get_round_trip(self):
        rec = TokenJupRecord(
            token_mint=USDC_MINT,
            jup_id=USDC_MINT,
            symbol="USDC",
            name="USD Coin",
            decimals=6,
            organic_score=95.5,
            organic_score_label="high",
            holder_count=1_234_567,
            mcap_usd=32_500_000_000,
            launchpad=None,
            global_fees_sol=12345.67,
            audit_mint_disabled=False,
            audit_freeze_disabled=False,
            audit_top_holders_pct=18.4,
            audit_bot_holders_pct=4.2,
            audit_dev_migrations=0,
            audit_total_holders=1_234_567,
            audit_risky_holders=12,
        )
        self.store.upsert(rec)
        row = self.store.get(USDC_MINT)
        self.assertIsNotNone(row)
        self.assertEqual(row["symbol"], "USDC")
        self.assertEqual(row["organic_score"], 95.5)
        self.assertEqual(row["global_fees_sol"], 12345.67)
        self.assertEqual(row["audit_top_holders_pct"], 18.4)

    def test_upsert_is_idempotent(self):
        rec = TokenJupRecord(token_mint=USDC_MINT, symbol="USDC", organic_score=80.0)
        self.store.upsert(rec)
        # Update the same row — symbol changes, organic_score changes
        rec2 = TokenJupRecord(token_mint=USDC_MINT, symbol="USDC.v2", organic_score=82.5)
        self.store.upsert(rec2)
        self.assertEqual(self.store.count(), 1)
        row = self.store.get(USDC_MINT)
        self.assertEqual(row["symbol"], "USDC.v2")
        self.assertEqual(row["organic_score"], 82.5)

    # --- gate flag tests (v_token_jup_risk) ---------------------------------

    def _upsert_with(self, **overrides) -> dict:
        defaults = dict(
            token_mint=USDC_MINT,
            symbol="TEST",
            name="Test Token",
            organic_score=80.0,
            holder_count=10000,
            mcap_usd=1_000_000,
            global_fees_sol=100.0,
            audit_mint_disabled=True,
            audit_freeze_disabled=True,
            audit_top_holders_pct=20.0,
            audit_bot_holders_pct=10.0,
            audit_dev_migrations=0,
        )
        defaults.update(overrides)
        rec = TokenJupRecord(**defaults)
        self.store.upsert(rec)
        return self.store.jup_risk(USDC_MINT)

    def test_gate_fees_too_low_when_below_30(self):
        risk = self._upsert_with(global_fees_sol=10.0)
        self.assertEqual(risk["gate_fees_too_low"], 1)

    def test_gate_fees_not_too_low_when_above_30(self):
        risk = self._upsert_with(global_fees_sol=100.0)
        self.assertEqual(risk["gate_fees_too_low"], 0)

    def test_gate_top_holders_when_above_60(self):
        risk = self._upsert_with(audit_top_holders_pct=70.0)
        self.assertEqual(risk["gate_top_holders"], 1)

    def test_gate_top_holders_not_when_below_60(self):
        risk = self._upsert_with(audit_top_holders_pct=50.0)
        self.assertEqual(risk["gate_top_holders"], 0)

    def test_gate_bot_holders_when_above_30(self):
        risk = self._upsert_with(audit_bot_holders_pct=35.0)
        self.assertEqual(risk["gate_bot_holders"], 1)

    def test_gate_bot_holders_not_when_below_30(self):
        risk = self._upsert_with(audit_bot_holders_pct=10.0)
        self.assertEqual(risk["gate_bot_holders"], 0)

    def test_gate_organic_score_when_below_60(self):
        risk = self._upsert_with(organic_score=40.0)
        self.assertEqual(risk["gate_organic_score"], 1)

    def test_gate_organic_score_not_when_above_60(self):
        risk = self._upsert_with(organic_score=80.0)
        self.assertEqual(risk["gate_organic_score"], 0)

    def test_gate_no_mint_disable_when_disabled_false(self):
        risk = self._upsert_with(audit_mint_disabled=False)
        self.assertEqual(risk["gate_no_mint_disable"], 1)

    def test_gate_no_mint_disable_zero_when_disabled_true(self):
        risk = self._upsert_with(audit_mint_disabled=True)
        self.assertEqual(risk["gate_no_mint_disable"], 0)

    def test_is_rugged_or_unknown_set_when_error_present(self):
        rec = TokenJupRecord(
            token_mint=USDC_MINT,
            error="not_found",
            last_seen_chain=False,
        )
        self.store.upsert(rec)
        risk = self.store.jup_risk(USDC_MINT)
        self.assertEqual(risk["is_rugged_or_unknown"], 1)

    def test_gates_handle_nulls_gracefully(self):
        # If global_fees_sol is unknown (null), gate should be 0 (not 1) —
        # otherwise every unknown-token row would falsely trip "fees too low".
        rec = TokenJupRecord(
            token_mint=USDC_MINT,
            symbol="UNKNOWN",
            global_fees_sol=None,
            audit_top_holders_pct=None,
            audit_bot_holders_pct=None,
            organic_score=None,
        )
        self.store.upsert(rec)
        risk = self.store.jup_risk(USDC_MINT)
        self.assertEqual(risk["gate_fees_too_low"], 0)
        self.assertEqual(risk["gate_top_holders"], 0)
        self.assertEqual(risk["gate_bot_holders"], 0)
        self.assertEqual(risk["gate_organic_score"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
