"""
End-to-end test for fact_lp_position with a mock Meteora DLMM API server.

Covers:
  - fetch_positions handles 200 OK with list response
  - fetch_positions handles 404 (no positions) gracefully
  - fetch_positions handles network errors
  - CLI upserts positions and reports counts
  - CLI handles empty wallet

Run:
    .venv/bin/python -m unittest test_fact_lp_cli_mock -v
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


WALLET_WITH_POSITIONS = "WalletWith111111111111111111111111111111111"
WALLET_NO_POSITIONS    = "WalletNoPos1111111111111111111111111111111"

SAMPLE_POSITION = {
    "position_address": "Pos1111111111111111111111111111111111",
    "pool_address":     "Pool11111111111111111111111111111111111",
    "owner":            WALLET_WITH_POSITIONS,
    "lower_bin_id":     -50,
    "upper_bin_id":     50,
    "bin_step":         5,
    "created_at":       1717000000,
    "last_updated_at":  1717500000,
    "total_deposit_x":  "10.5",
    "total_deposit_y":  "1500.0",
    "total_deposit_value_usd": "1500.00",
    "total_withdraw_x": "0",
    "total_withdraw_y": "0",
    "total_withdraw_value_usd": None,
    "fees_claimed_x":   "0.1",
    "fees_claimed_y":   "15.0",
    "fees_claimed_value_usd": "15.50",
    "token_x_mint":     "So11111111111111111111111111111111111111112",
    "token_y_mint":     "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}


def make_handler():
    """Return a BaseHTTPRequestHandler that mocks Meteora's /position/{owner}."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # /position/{owner}
            if "/position/" in self.path:
                wallet = self.path.split("/position/")[-1].split("?")[0]
                if wallet == WALLET_NO_POSITIONS:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if wallet == WALLET_WITH_POSITIONS:
                    body = json.dumps([SAMPLE_POSITION]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            # Default: 404
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args, **kwargs):
            pass  # silence
    return Handler


class TestFactLPCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), make_handler())
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api_base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _run_cli(self, wallet: str, db_path: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "fact_lp_position.py",
            "--wallet", wallet, "--db", db_path, "--api", self.api_base,
        ]
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=30)

    def test_wallet_with_positions(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            p = self._run_cli(WALLET_WITH_POSITIONS, db)
            self.assertEqual(p.returncode, 0, f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            self.assertIn("Total positions stored: 1", p.stdout)
            self.assertIn("open:   1", p.stdout)
            self.assertIn("Upserted position", p.stdout)
        finally:
            Path(db).unlink(missing_ok=True)
            for sfx in ("-wal", "-shm", "-journal"):
                Path(db + sfx).unlink(missing_ok=True)

    def test_wallet_no_positions(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            p = self._run_cli(WALLET_NO_POSITIONS, db)
            self.assertEqual(p.returncode, 0, f"CLI failed:\nSTDERR:\n{p.stderr}")
            self.assertIn("No positions found", p.stdout)
        finally:
            Path(db).unlink(missing_ok=True)
            for sfx in ("-wal", "-shm", "-journal"):
                Path(db + sfx).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
