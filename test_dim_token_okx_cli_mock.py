"""
End-to-end CLI test using a mock HTTP server that pretends to be RugCheck.

Verifies:
  - CLI parses args correctly
  - fetch_risk() GETs /v1/tokens/<mint>/report on the configured base URL
  - TokenOkxStore creates the schema, upserts the row, and v_token_okx_risk
    materializes the right gate flags
  - A "clean" mint and a "scam" mint (creator history + honeypot/rugged) both
    round-trip
  - An unknown mint (404) records an error row but does NOT crash the CLI
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
SCAM_MINT = "ScamMintForDevRugHoneypotXXXXXXXXXXXXXXXXXXX"
UNKNOWN_MINT = "NotFoundOkxMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


# ---------------------------------------------------------------------------
# Canned RugCheck responses
# ---------------------------------------------------------------------------

def usdc_report() -> dict:
    return {
        "mint": USDC_MINT,
        "score": 1,
        "score_normalised": 1,
        "rugged": False,
        "risks": [],
        "topHolders": None,
        "insiderNetworks": None,
        "creatorTokens": None,
        "totalHolders": 350000,
    }


def scam_report() -> dict:
    """rugged=True, score_normalised=95, multiple risks, 5 creator tokens,
    top10 concentration 75%, insider clusters 45%."""
    return {
        "mint": SCAM_MINT,
        "score": 95,
        "score_normalised": 95,
        "rugged": True,
        "risks": [
            {"name": "Honeypot detected", "type": "honeypot", "level": "danger"},
            {"name": "Wash trading", "type": "wash", "level": "warn"},
            {"name": "Sniper concentration", "type": "sniper", "pct": 45.0},
        ],
        "topHolders": [{"pct": p} for p in
                       (12.0, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0, 5.5, 5.0)],
        "insiderNetworks": [
            {"id": "n1", "tokenAmountPct": 25.0},
            {"id": "n2", "tokenAmountPct": 20.0},
        ],
        "creatorTokens": [{"mint": f"m{i}"} for i in range(5)],
        "totalHolders": 1200,
    }


# ---------------------------------------------------------------------------
# Mock RugCheck HTTP server
# ---------------------------------------------------------------------------

def rugcheck_handler(canned: dict):
    """Build a request handler that serves canned responses keyed by mint."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Expect /v1/tokens/<mint>/report
            path = self.path.split("?", 1)[0]
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "v1" or parts[1] != "tokens" \
                    or parts[3] != "report":
                self.send_response(400)
                self.end_headers()
                return
            mint = parts[2]

            if mint in canned:
                body = canned[mint]
                status = 200
            else:
                body = {"error": "not found"}
                status = 404

            out = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a, **kw):
            pass  # silence

    return Handler


class TestCliWithMockRugcheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canned = {
            USDC_MINT: usdc_report(),
            SCAM_MINT: scam_report(),
            # UNKNOWN_MINT intentionally omitted -> 404
        }
        handler = rugcheck_handler(cls.canned)
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.server_thread.start()
        cls.api_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _run_cli(self, mint: str, db_path: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "dim_token_okx.py",
            "--mint", mint, "--db", db_path, "--api", self.api_url,
            "--timeout", "5",
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=Path(__file__).parent, timeout=30,
        )

    def _with_tmp_db(self) -> str:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        return f.name

    def _cleanup_db(self, path: str):
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(path + sfx).unlink(missing_ok=True)

    def test_clean_token_passes_all_gates(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(USDC_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            self.assertIn("hard_flags: (none)", p.stdout)
            self.assertIn("soft_flags: (none)", p.stdout)
            self.assertIn("dev_rug_count: 0", p.stdout)
            self.assertIn("risk_level: 1", p.stdout)
            self.assertIn("is_unknown: False", p.stdout)
        finally:
            self._cleanup_db(db)

    def test_scam_token_trips_dev_rug_count_and_honeypot(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(SCAM_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            # Hard gates from honeypot/rugged/wash + creator-history + bundle + top10
            self.assertIn("dev_rug_count>=1", p.stdout)
            self.assertIn("is_honeypot", p.stdout)
            self.assertIn("is_rugpull", p.stdout)
            self.assertIn("is_wash", p.stdout)
            self.assertIn("bundle_pct>30", p.stdout)
            self.assertIn("top10_pct>60", p.stdout)
            self.assertIn("dev_rug_count: 5", p.stdout)
            # Soft gates
            self.assertIn("sniper_pct>30", p.stdout)
            self.assertIn("risk_level>=4", p.stdout)
        finally:
            self._cleanup_db(db)

    def test_unknown_mint_records_error_without_crashing(self):
        db = self._with_tmp_db()
        try:
            p = self._run_cli(UNKNOWN_MINT, db)
            self.assertEqual(p.returncode, 0,
                             f"CLI failed:\nSTDERR:\n{p.stderr}")
            # 404 maps to error='not_found'
            self.assertIn("error: not_found", p.stdout)
            self.assertIn("last_seen_chain: False", p.stdout)
            self.assertIn("is_unknown: True", p.stdout)
        finally:
            self._cleanup_db(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
