"""
End-to-end CLI test using a mock HTTP server that pretends to be a Solana RPC.

Verifies:
  - CLI parses args correctly
  - fetch_authority() makes a real JSON-RPC POST and decodes the response
  - TokenAuthorityStore creates the schema, upserts the row, and the risk view
    returns the correct hard-gate verdict
  - Both "safe" and "unsafe" mints round-trip correctly

We do NOT need a real Helius key for this — we serve canned responses for
the two test mints. Real mainnet verification is a separate, manual step
(see README in the project folder).
"""
import base64
import http.server
import json
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# --- Fixtures (mirrored from test_dim_token_authority.py) --------------------

def spl_mint_bytes(mint_auth: bytes | None, freeze_auth: bytes | None,
                   supply: int = 0, decimals: int = 9) -> bytes:
    buf = bytearray(82)
    if mint_auth is not None:
        struct.pack_into("<I", buf, 0, 1)
        buf[4:36] = mint_auth
    struct.pack_into("<Q", buf, 36, supply)
    buf[44] = decimals
    buf[45] = 1
    if freeze_auth is not None:
        struct.pack_into("<I", buf, 46, 1)
        buf[50:82] = freeze_auth
    return bytes(buf)


SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # real: both authorities None
SOL_MINT  = "So11111111111111111111111111111111111111112"   # real: both authorities set


def mock_rpc_handler(mint_responses: dict):
    """Return a request handler that serves canned responses per mint."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            mint = body["params"][0]
            if mint in mint_responses:
                resp = mint_responses[mint]
            else:
                resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "not found"}}
            out = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args, **kwargs):
            pass  # silence
    return Handler


def make_rpc_response(mint: str, data: bytes) -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "result": {
            "context": {"slot": 1},
            "value": {
                "data": [base64.b64encode(data).decode(), "base64"],
                "executable": False,
                "lamports": 1000000,
                "owner": SPL_TOKEN_PROGRAM_ID,
                "rentEpoch": 0,
                "space": len(data),
            }
        }
    }


# --- Test ---------------------------------------------------------------------

class TestCliWithMockRpc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # USDC: BOTH authorities held by Circle (live Helius confirmed 2026-06-06).
        # Earlier fixture had both None — that was based on a wrong assumption.
        # This is the canonical "soft warning, not auto-avoid" case.
        usdc_authority = b"\x07" * 32  # placeholder, not parsed in test
        usdc_data = spl_mint_bytes(mint_auth=usdc_authority, freeze_auth=usdc_authority,
                                   supply=1_000_000_000_000, decimals=6)
        # Wrapped SOL: BOTH authorities renounced (live Helius confirmed 2026-06-06).
        # Earlier fixture had both set — that was wrong; the canonical "safe" case.
        sol_data = spl_mint_bytes(mint_auth=None, freeze_auth=None,
                                  supply=0, decimals=9)
        cls.mock_responses = {
            USDC_MINT: make_rpc_response(USDC_MINT, usdc_data),
            SOL_MINT:  make_rpc_response(SOL_MINT, sol_data),
            "NotFoundMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX": {
                "jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": None}
            },
        }

        # Start a free-port server
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), mock_rpc_handler(cls.mock_responses))
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.rpc_url = f"http://127.0.0.1:{cls.port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _run_cli(self, mint: str, db_path: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "dim_token_authority.py",
            "--mint", mint, "--db", db_path, "--rpc", self.rpc_url,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent, timeout=30)

    def test_usdc_authority_renounced_is_safe(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            p = self._run_cli(USDC_MINT, db)
            self.assertEqual(p.returncode, 0, f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            # Live-test refined: USDC keeps mint+freeze authority (Circle),
            # so the new view should mark it 'review_mint' (soft warning),
            # NOT 'avoid'. Original test asserted 'safe' — that was wrong.
            self.assertIn("verdict: review_mint", p.stdout)
            self.assertIn("soft_warnings: mint=True", p.stdout)
            self.assertIn("hard_avoid (T22 perm_delegate): False", p.stdout)
        finally:
            Path(db).unlink(missing_ok=True)
            for sfx in ("-wal", "-shm", "-journal"):
                Path(db + sfx).unlink(missing_ok=True)

    def test_wsol_authority_renounced_is_safe(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            p = self._run_cli(SOL_MINT, db)
            self.assertEqual(p.returncode, 0, f"CLI failed:\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
            # wSOL on mainnet: both authorities renounced.
            self.assertIn("verdict: safe", p.stdout)
            self.assertIn("flags: (none)", p.stdout)
            self.assertIn("soft_warnings: mint=False freeze=False", p.stdout)
        finally:
            Path(db).unlink(missing_ok=True)
            for sfx in ("-wal", "-shm", "-journal"):
                Path(db + sfx).unlink(missing_ok=True)

    def test_not_found_mint_records_error(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            p = self._run_cli("NotFoundMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", db)
            self.assertEqual(p.returncode, 0, f"CLI failed:\nSTDERR:\n{p.stderr}")
            self.assertIn("error: not_found", p.stdout)
            self.assertIn("verdict: <unknown", p.stdout)
        finally:
            Path(db).unlink(missing_ok=True)
            for sfx in ("-wal", "-shm", "-journal"):
                Path(db + sfx).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
