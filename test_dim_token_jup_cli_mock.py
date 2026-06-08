"""
End-to-end CLI test using a mock HTTP server that pretends to be Jupiter datapi.

Verifies:
  - CLI parses args correctly
  - fetch_jupiter() makes a real HTTPS-equivalent GET and decodes the response
  - TokenJupStore creates the schema, upserts the row, and v_token_jup_risk
    materializes the right gate flags
  - "safe" and "rejected" mints both round-trip
  - Unknown mint produces an error row but does NOT crash the CLI

Mirrors the style of test_cli_mock_rpc.py.
"""
import http.server
import json
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SCAM_MINT = "ScamMintForBundlerRejectionTestXXXXXXXXXXXX"
UNKNOWN_MINT = "NotFoundMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


def usdc_asset() -> dict:
    """USDC: blue-chip, high organic, lots of fees, low concentration."""
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


def scam_asset() -> dict:
    """A token that should trip multiple Meridian gates."""
    return {
        "id": SCAM_MINT,
        "name": "Scam Token",
        "symbol": "SCAM",
        "decimals": 9,
        "organicScore": 12.0,            # < 60 -> gate_organic_score
        "organicScoreLabel": "low",
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": True,
            "topHoldersPercentage": 78.5,  # > 60 -> gate_top_holders
            "botHoldersPercentage": 45.0,  # > 30 -> gate_bot_holders
            "devMigrations": 7,
            "totalHolders": 400,
            "riskyHolders": 320,
        },
        "holderCount": 400,
        "mcap": 25_000,
        "launchpad": "pump.fun",
        "fees": 2.5,                     # < 30 -> gate_fees_too_low
    }


def jupiter_handler(asset_responses: dict):
    """Return a request handler that serves canned /v1/assets/search responses
    keyed by the query string mint."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Path looks like /v1/assets/search?query=<mint>
            if "query=" not in self.path:
                self.send_response(400)
                self.end_headers()
                return
            mint = self.path.split("query=", 1)[1].split("&", 1)[0]
            if mint in asset_responses:
                body = asset_responses[mint]
                status = 200
            else:
                # Jupiter returns 200 with an empty list for misses, not 404.
                body = {"data": []}
                status = 200
            out = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args, **kwargs):
            pass  # silence

    return Handler


class TestCliWithMockJupiter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.responses = {
            USDC_MINT: {"data": [usdc_asset()]},
            SCAM_MINT: {"data": [scam_asset()]},
            # UNKNOWN_MINT intentionally omitted -> default empty-list reply
        }
        cls.server = socketserver.TCPServer(
            ("127.0.0.1", 0), jupiter_handler(cls.responses)
        )
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.api_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _run_cli(self, mint: str, db_path: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "dim_token_jup.py",
            "--mint", mint, "--db", db_path, "--api", self.api_url,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=Path(__file__).parent, timeout=30,
        )

    def _with_tmp_db(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        return f.name

    def _cleanup_db(self, path: str):
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(path + sfx).unlink(missing_ok=True)

    def test_usdc_passes_most_gates(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(USDC_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            self.assertIn("symbol: USDC", p.stdout)
            self.assertIn("organic_score: 95.5", p.stdout)
            self.assertIn("global_fees_sol: 12345.67", p.stdout)
            # USDC: high fees, low concentration, high organic -> only flag is
            # mint authority not disabled (Circle keeps it).
            self.assertIn("mint_authority_not_disabled", p.stdout)
            self.assertNotIn("fees_too_low", p.stdout)
            self.assertNotIn("top_holders_too_concentrated", p.stdout)
            self.assertNotIn("too_many_bot_holders", p.stdout)
            self.assertNotIn("low_organic_score", p.stdout)
        finally:
            self._cleanup_db(db)

    def test_scam_token_trips_all_hard_gates(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(SCAM_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            self.assertIn("symbol: SCAM", p.stdout)
            # All four Meridian gates should be tripped.
            self.assertIn("fees_too_low", p.stdout)
            self.assertIn("top_holders_too_concentrated", p.stdout)
            self.assertIn("too_many_bot_holders", p.stdout)
            self.assertIn("low_organic_score", p.stdout)
            # And dev_migrations surfaced
            self.assertIn("dev_migrations: 7", p.stdout)
        finally:
            self._cleanup_db(db)

    def test_unknown_mint_records_not_found_without_crashing(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(UNKNOWN_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDERR:\n{p.stderr}")
            self.assertIn("error: not_found", p.stdout)
            self.assertIn("last_seen_chain: False", p.stdout)
            # is_rugged_or_unknown should be True in the risk view
            self.assertIn("is_rugged_or_unknown: True", p.stdout)
        finally:
            self._cleanup_db(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
