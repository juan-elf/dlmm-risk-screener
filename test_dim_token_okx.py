"""
Offline unit tests for dim_token_okx parser + store.

The fetcher source is RugCheck (migrated from OKX on 2026-06-08); the
table/view names and dataclass are unchanged for schema continuity. Tests
exercise the RugCheck JSON parser against hand-crafted bodies and verify the
SQLite schema/view round trip correctly.

Run:
    .venv/bin/python -m unittest test_dim_token_okx -v
or
    .venv/bin/python test_dim_token_okx.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dim_token_okx import (
    TokenOkxRecord,
    TokenOkxStore,
    _parse_rugcheck_report,
    _bucket_risk_level,
)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SCAM_MINT = "ScamMintForDevRugCountRejectionXXXXXXXXXXXXX"
UNKNOWN_MINT = "NotFoundOkxMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


# ---------------------------------------------------------------------------
# RugCheck payload builders
# ---------------------------------------------------------------------------

def _rugcheck_clean(**overrides):
    body = {
        "mint": USDC_MINT,
        "score": 1,
        "score_normalised": 1,
        "rugged": False,
        "risks": [],
        "topHolders": None,
        "insiderNetworks": None,
        "creatorTokens": None,
        "totalHolders": 350000,
        "transferFee": {"pct": 0, "maxAmount": 0},
    }
    body.update(overrides)
    return body


def _rugcheck_rugged():
    return {
        "mint": SCAM_MINT,
        "score": 100,
        "score_normalised": 100,
        "rugged": True,
        "risks": [
            {"name": "High Owner Concentration", "type": "high_owner",
             "level": "danger"},
            {"name": "Honeypot detected", "type": "honeypot_warning",
             "level": "danger"},
            {"name": "Wash trading", "type": "wash_trade", "level": "warn"},
            {"name": "Sniper bots present", "type": "sniper", "pct": 45.0},
        ],
        "topHolders": [
            {"pct": 12.0}, {"pct": 9.0}, {"pct": 8.5}, {"pct": 8.0},
            {"pct": 7.5}, {"pct": 7.0}, {"pct": 6.5}, {"pct": 6.0},
            {"pct": 5.5}, {"pct": 5.0},
        ],
        "insiderNetworks": [
            {"id": "n1", "tokenAmountPct": 22.0},
            {"id": "n2", "tokenAmountPct": 13.0},
        ],
        "creatorTokens": [
            {"mint": "m1"}, {"mint": "m2"}, {"mint": "m3"}, {"mint": "m4"},
            {"mint": "m5"},
        ],
        "totalHolders": 1200,
    }


# ---------------------------------------------------------------------------
# TestParseRugcheck — defensive parsing
# ---------------------------------------------------------------------------

class TestParseRugcheck(unittest.TestCase):

    def test_bucket_risk_level(self):
        self.assertEqual(_bucket_risk_level(0), 1)
        self.assertEqual(_bucket_risk_level(1), 1)
        self.assertEqual(_bucket_risk_level(20), 2)
        self.assertEqual(_bucket_risk_level(40), 3)
        self.assertEqual(_bucket_risk_level(60), 4)
        self.assertEqual(_bucket_risk_level(80), 5)
        self.assertEqual(_bucket_risk_level(100), 5)
        self.assertIsNone(_bucket_risk_level(None))
        self.assertIsNone(_bucket_risk_level("garbage"))

    def test_parse_rugcheck_clean_token(self):
        """USDC-like response: score_normalised=1, no risks, creatorTokens=null."""
        body = _rugcheck_clean()
        out = _parse_rugcheck_report(USDC_MINT, body)
        self.assertEqual(out["risk_level"], 1)
        self.assertFalse(out["is_honeypot"])
        self.assertFalse(out["is_rugpull"])
        self.assertFalse(out["is_wash"])
        # creatorTokens=null -> dev_rug_count=0
        self.assertEqual(out["dev_rug_count"], 0)
        # insiderNetworks=null -> bundle_pct=None (don't trip gate)
        self.assertIsNone(out["bundle_pct"])
        # topHolders=null -> top10_pct=None
        self.assertIsNone(out["top10_pct"])
        self.assertEqual(out["dev_token_count"], 350000)  # totalHolders
        # tags_json present even when no risks
        self.assertEqual(json.loads(out["tags_json"]), [])
        # raw body captured
        self.assertIsNotNone(out["raw_advanced_info_json"])
        # RugCheck doesn't provide these
        self.assertIsNone(out["ath_usd"])
        self.assertIsNone(out["current_price_usd"])
        self.assertIsNone(out["lp_burned_pct"])

    def test_parse_rugcheck_rugged_token(self):
        """Severe rug: rugged=True, score_normalised=100, multiple risks."""
        body = _rugcheck_rugged()
        out = _parse_rugcheck_report(SCAM_MINT, body)
        self.assertEqual(out["risk_level"], 5)
        self.assertTrue(out["is_honeypot"])  # matched on "honeypot"
        self.assertTrue(out["is_rugpull"])   # rugged=True
        self.assertTrue(out["is_wash"])      # matched on "wash"
        # Sniper percentage extracted from the risks[] entry
        self.assertEqual(out["sniper_pct"], 45.0)
        # creatorTokens of length 5 -> dev_rug_count=5
        self.assertEqual(out["dev_rug_count"], 5)
        # Sum of top-10 holder pct
        self.assertAlmostEqual(out["top10_pct"], 75.0)
        # Sum of insiderNetworks pct -> bundle_pct
        self.assertAlmostEqual(out["bundle_pct"], 35.0)
        # tags_json carries all four risk names
        tags = json.loads(out["tags_json"])
        self.assertEqual(len(tags), 4)
        self.assertEqual(out["dev_token_count"], 1200)

    def test_parse_rugcheck_with_creator_history(self):
        """A creator with 5 prior tokens -> dev_rug_count=5 (Meridian's
        single strongest rug predictor proxy)."""
        body = _rugcheck_clean(creatorTokens=[
            {"mint": f"m{i}"} for i in range(5)
        ])
        out = _parse_rugcheck_report(SCAM_MINT, body)
        self.assertEqual(out["dev_rug_count"], 5)
        # Clean score still buckets to risk_level=1
        self.assertEqual(out["risk_level"], 1)

    def test_parse_rugcheck_404_returns_not_found(self):
        """fetch_risk surfaces 404 as error='not_found'. The parser itself
        must not crash on a body that's None or an empty dict."""
        out = _parse_rugcheck_report(UNKNOWN_MINT, None)
        self.assertIsNone(out["risk_level"])
        self.assertIsNone(out["is_honeypot"])
        self.assertIsNone(out["is_rugpull"])
        self.assertIsNone(out["dev_rug_count"])
        # And empty dict also parses cleanly
        out2 = _parse_rugcheck_report(UNKNOWN_MINT, {})
        self.assertIsNone(out2["risk_level"])
        self.assertFalse(out2["is_rugpull"])   # rugged defaults False
        self.assertEqual(out2["dev_rug_count"], 0)

    def test_parse_rugcheck_malformed_risks_not_a_list(self):
        body = _rugcheck_clean(risks="not_a_list")
        out = _parse_rugcheck_report(USDC_MINT, body)
        # Doesn't crash; flags stay false
        self.assertFalse(out["is_honeypot"])
        self.assertFalse(out["is_wash"])

    def test_parse_rugcheck_insider_networks_as_dict(self):
        """RugCheck can return insiderNetworks as a dict keyed by network id."""
        body = _rugcheck_clean(insiderNetworks={
            "net1": {"tokenAmountPct": 18.0},
            "net2": {"tokenAmountPct": 14.5},
        })
        out = _parse_rugcheck_report(USDC_MINT, body)
        self.assertAlmostEqual(out["bundle_pct"], 32.5)


# ---------------------------------------------------------------------------
# TestTokenOkxStore — SQLite schema + view tests (unchanged from OKX era)
# ---------------------------------------------------------------------------

class TestTokenOkxStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.store = TokenOkxStore(self.db_path)

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db_path + sfx).unlink(missing_ok=True)

    def test_init_schema_applies_cleanly(self):
        self.store.close()
        self.store = TokenOkxStore(self.db_path)
        self.assertEqual(self.store.count(), 0)

    def test_upsert_and_get_round_trip(self):
        rec = TokenOkxRecord(
            token_mint=USDC_MINT,
            risk_level=2,
            bundle_pct=18.5,
            sniper_pct=4.0,
            top10_pct=20.0,
            lp_burned_pct=95.0,
            dev_rug_count=0,
            dev_token_count=1,
            is_honeypot=False,
            is_rugpull=False,
            is_wash=False,
            ath_usd=1.0,
            atl_usd=0.95,
            current_price_usd=0.999,
            price_vs_ath_pct=-0.1,
            tags_json=json.dumps(["smartMoneyBuy"]),
        )
        self.store.upsert(rec)
        row = self.store.get(USDC_MINT)
        self.assertIsNotNone(row)
        self.assertEqual(row["risk_level"], 2)
        self.assertEqual(row["bundle_pct"], 18.5)
        self.assertEqual(row["dev_rug_count"], 0)
        self.assertEqual(row["current_price_usd"], 0.999)
        self.assertEqual(json.loads(row["tags_json"]), ["smartMoneyBuy"])

    def test_upsert_is_idempotent(self):
        rec = TokenOkxRecord(token_mint=USDC_MINT, risk_level=2, dev_rug_count=0)
        self.store.upsert(rec)
        rec2 = TokenOkxRecord(token_mint=USDC_MINT, risk_level=4, dev_rug_count=2)
        self.store.upsert(rec2)
        self.assertEqual(self.store.count(), 1)
        row = self.store.get(USDC_MINT)
        self.assertEqual(row["risk_level"], 4)
        self.assertEqual(row["dev_rug_count"], 2)

    # --- v_token_okx_risk gate-flag tests -----------------------------------

    def _upsert_with(self, **overrides) -> dict:
        defaults = dict(
            token_mint=USDC_MINT,
            risk_level=2,
            bundle_pct=10.0,
            sniper_pct=5.0,
            suspicious_pct=2.0,
            dev_holding_pct=4.0,
            top10_pct=30.0,
            lp_burned_pct=95.0,
            dev_rug_count=0,
            dev_token_count=1,
            is_honeypot=False,
            is_rugpull=False,
            is_wash=False,
            ath_usd=1.0,
            atl_usd=0.9,
            current_price_usd=0.95,
            price_vs_ath_pct=-5.0,
        )
        defaults.update(overrides)
        rec = TokenOkxRecord(**defaults)
        self.store.upsert(rec)
        return self.store.okx_risk(USDC_MINT)

    def test_gate_is_honeypot_when_true(self):
        risk = self._upsert_with(is_honeypot=True)
        self.assertEqual(risk["gate_is_honeypot"], 1)

    def test_gate_is_honeypot_when_false(self):
        risk = self._upsert_with(is_honeypot=False)
        self.assertEqual(risk["gate_is_honeypot"], 0)

    def test_gate_is_rugpull_when_true(self):
        risk = self._upsert_with(is_rugpull=True)
        self.assertEqual(risk["gate_is_rugpull"], 1)

    def test_gate_is_wash_when_true(self):
        risk = self._upsert_with(is_wash=True)
        self.assertEqual(risk["gate_is_wash"], 1)

    def test_gate_dev_rug_count_when_one(self):
        risk = self._upsert_with(dev_rug_count=1)
        self.assertEqual(risk["gate_dev_rug_count"], 1)

    def test_gate_dev_rug_count_when_zero(self):
        risk = self._upsert_with(dev_rug_count=0)
        self.assertEqual(risk["gate_dev_rug_count"], 0)

    def test_gate_dev_rug_count_when_many(self):
        risk = self._upsert_with(dev_rug_count=7)
        self.assertEqual(risk["gate_dev_rug_count"], 1)

    def test_gate_bundle_high_when_above_30(self):
        risk = self._upsert_with(bundle_pct=35.0)
        self.assertEqual(risk["gate_bundle_high"], 1)

    def test_gate_bundle_high_when_below_30(self):
        risk = self._upsert_with(bundle_pct=20.0)
        self.assertEqual(risk["gate_bundle_high"], 0)

    def test_gate_top10_concentrated_when_above_60(self):
        risk = self._upsert_with(top10_pct=70.0)
        self.assertEqual(risk["gate_top10_concentrated"], 1)

    def test_gate_top10_concentrated_when_below_60(self):
        risk = self._upsert_with(top10_pct=55.0)
        self.assertEqual(risk["gate_top10_concentrated"], 0)

    def test_gate_sniper_high_when_above_30(self):
        risk = self._upsert_with(sniper_pct=45.0)
        self.assertEqual(risk["gate_sniper_high"], 1)

    def test_gate_risk_level_high_when_four(self):
        risk = self._upsert_with(risk_level=4)
        self.assertEqual(risk["gate_risk_level_high"], 1)

    def test_gate_risk_level_high_when_three(self):
        risk = self._upsert_with(risk_level=3)
        self.assertEqual(risk["gate_risk_level_high"], 0)

    def test_is_unknown_when_error_present(self):
        rec = TokenOkxRecord(
            token_mint=UNKNOWN_MINT,
            error="not_found",
            last_seen_chain=False,
        )
        self.store.upsert(rec)
        risk = self.store.okx_risk(UNKNOWN_MINT)
        self.assertEqual(risk["is_unknown"], 1)

    def test_is_unknown_zero_when_error_null(self):
        risk = self._upsert_with()
        self.assertEqual(risk["is_unknown"], 0)

    def test_all_gates_clear_for_clean_token(self):
        risk = self._upsert_with()
        self.assertEqual(risk["gate_is_honeypot"], 0)
        self.assertEqual(risk["gate_is_rugpull"], 0)
        self.assertEqual(risk["gate_is_wash"], 0)
        self.assertEqual(risk["gate_dev_rug_count"], 0)
        self.assertEqual(risk["gate_bundle_high"], 0)
        self.assertEqual(risk["gate_top10_concentrated"], 0)
        self.assertEqual(risk["gate_sniper_high"], 0)
        self.assertEqual(risk["gate_risk_level_high"], 0)
        self.assertEqual(risk["is_unknown"], 0)

    def test_nulls_do_not_falsely_trigger_gates(self):
        rec = TokenOkxRecord(
            token_mint=USDC_MINT,
            risk_level=None,
            bundle_pct=None,
            sniper_pct=None,
            top10_pct=None,
            dev_rug_count=None,
            is_honeypot=None,
            is_rugpull=None,
            is_wash=None,
        )
        self.store.upsert(rec)
        risk = self.store.okx_risk(USDC_MINT)
        self.assertEqual(risk["gate_is_honeypot"], 0)
        self.assertEqual(risk["gate_dev_rug_count"], 0)
        self.assertEqual(risk["gate_bundle_high"], 0)
        self.assertEqual(risk["gate_top10_concentrated"], 0)
        self.assertEqual(risk["gate_sniper_high"], 0)
        self.assertEqual(risk["gate_risk_level_high"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
