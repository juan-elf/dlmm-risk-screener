"""
Offline tests for dim_token_authority:
  - Parser (byte decoder): SPL + Token-2022 mint account layout
  - Store CRUD: upsert, get, authority_risk view
  - Address label store: add, bulk load, idempotent, inactive
  - Effective authority verdict view (v_effective_authority_verdict)
  - Seed file structure
"""
import string
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dim_token_authority import (
    SPL_MINT_SIZE,
    SPL_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TokenAuthority,
    TokenAuthorityStore,
    _parse_pubkey_option,
    _parse_spl_mint,
    _parse_token_2022_mint,
    parse_mint_account,
)


# --- Shared fixtures ----------------------------------------------------------

KNOWN_GOOD_MINT_AUTH  = "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG"  # Circle (USDC)
KNOWN_GOOD_FREEZE_AUTH = "Q6XprfkF8RQQKoQVG33xT88H7wi8Uk1B1CC7YAs69Gi"  # Tether (USDT)
UNKNOWN_AUTH           = "9N9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X9X"


def _make_spl_mint(mint_authority: bytes | None,
                   freeze_authority: bytes | None,
                   supply: int = 0, decimals: int = 9) -> bytes:
    buf = bytearray(SPL_MINT_SIZE)
    if mint_authority is not None:
        assert len(mint_authority) == 32
        struct.pack_into("<I", buf, 0, 1)
        buf[4:36] = mint_authority
    struct.pack_into("<Q", buf, 36, supply)
    buf[44] = decimals & 0xFF
    buf[45] = 1  # is_initialized
    if freeze_authority is not None:
        assert len(freeze_authority) == 32
        struct.pack_into("<I", buf, 46, 1)
        buf[50:82] = freeze_authority
    return bytes(buf)


def _make_token_2022_with_perm_delegate(mint_authority: bytes | None,
                                        permanent_delegate: bytes | None) -> bytes:
    base = bytearray(_make_spl_mint(mint_authority, freeze_authority=None, decimals=9))
    ext_payload = permanent_delegate or (b"\x00" * 32)
    ext = struct.pack("<HH", 22, len(ext_payload)) + ext_payload
    return bytes(base + ext)


def _make_rec(mint, mint_auth=None, freeze_auth=None,
              is_token_2022=False, has_permanent_delegate=False):
    return TokenAuthority(
        token_mint=mint,
        mint_authority=mint_auth,
        freeze_authority=freeze_auth,
        is_token_2022=is_token_2022,
        has_permanent_delegate=has_permanent_delegate,
        program_id=("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
                    if is_token_2022 else
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
        error=None,
    )


def _mint(label):
    return (label + "1" * 60)[:44]


# --- Parser tests -------------------------------------------------------------

class TestPubkeyOption(unittest.TestCase):
    def test_none_option(self):
        data = b"\x00" * 36
        self.assertIsNone(_parse_pubkey_option(data, 0, 4))

    def test_some_option(self):
        from solders.pubkey import Pubkey
        pk = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        data = struct.pack("<I", 1) + bytes(pk) + b"\x00" * 4
        self.assertEqual(_parse_pubkey_option(data, 0, 4), str(pk))

    def test_truncated_data_returns_none(self):
        self.assertIsNone(_parse_pubkey_option(b"\x01", 0, 4))


class TestSplMint(unittest.TestCase):
    def test_authority_renounced(self):
        result = _parse_spl_mint(_make_spl_mint(None, None))
        self.assertIsNone(result["mint_authority"])
        self.assertIsNone(result["freeze_authority"])
        self.assertEqual(result["decimals"], 9)

    def test_authority_present(self):
        from solders.pubkey import Pubkey
        mint_auth   = bytes(Pubkey.from_string("11111111111111111111111111111111"))
        freeze_auth = bytes(Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        result = _parse_spl_mint(_make_spl_mint(mint_auth, freeze_auth, supply=1_000_000_000, decimals=6))
        self.assertEqual(result["mint_authority"],   "11111111111111111111111111111111")
        self.assertEqual(result["freeze_authority"], "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        self.assertEqual(result["supply"], 1_000_000_000)
        self.assertEqual(result["decimals"], 6)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            _parse_spl_mint(b"\x00" * 50)


class TestToken2022Mint(unittest.TestCase):
    def test_no_extensions(self):
        from solders.pubkey import Pubkey
        data = _make_spl_mint(bytes(Pubkey.from_string("11111111111111111111111111111111")), None)
        result = _parse_token_2022_mint(data)
        self.assertFalse(result["has_permanent_delegate"])
        self.assertEqual(result["mint_authority"], "11111111111111111111111111111111")

    def test_with_permanent_delegate(self):
        from solders.pubkey import Pubkey
        delegate = bytes(Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        result = _parse_token_2022_mint(_make_token_2022_with_perm_delegate(None, delegate))
        self.assertTrue(result["has_permanent_delegate"])

    def test_zero_permanent_delegate_is_not_flagged(self):
        result = _parse_token_2022_mint(_make_token_2022_with_perm_delegate(None, None))
        self.assertFalse(result["has_permanent_delegate"])


class TestParseMintAccountDispatch(unittest.TestCase):
    def test_spl_dispatch(self):
        result = parse_mint_account(_make_spl_mint(None, None), SPL_TOKEN_PROGRAM_ID)
        self.assertFalse(result["is_token_2022"])
        self.assertFalse(result["has_permanent_delegate"])
        self.assertIsNone(result["update_authority"])
        self.assertIsNone(result["is_mutable_metadata"])

    def test_token_2022_dispatch(self):
        from solders.pubkey import Pubkey
        delegate = bytes(Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        result = parse_mint_account(_make_token_2022_with_perm_delegate(None, delegate), TOKEN_2022_PROGRAM_ID)
        self.assertTrue(result["is_token_2022"])
        self.assertTrue(result["has_permanent_delegate"])

    def test_unknown_program_raises(self):
        with self.assertRaises(ValueError):
            parse_mint_account(b"\x00" * 82, "UnknownProgramXXX")


# --- Store / v_token_authority_risk view tests --------------------------------

class TestAuthorityRiskView(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name
        self.store = TokenAuthorityStore(self.db)

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def test_safe(self):
        self.store.upsert(_make_rec("WsolLike1111111111111111111111111111111111"))
        r = self.store.authority_risk("WsolLike1111111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "safe")
        self.assertEqual(r["unrenounced_mint_warning"], 0)
        self.assertEqual(r["unrenounced_freeze_warning"], 0)
        self.assertEqual(r["t22_perm_delegate_hard_avoid"], 0)

    def test_review_mint(self):
        self.store.upsert(_make_rec("UsdcLike1111111111111111111111111111111111",
                                    mint_auth=KNOWN_GOOD_MINT_AUTH))
        r = self.store.authority_risk("UsdcLike1111111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "review_mint")
        self.assertEqual(r["unrenounced_mint_warning"], 1)
        self.assertEqual(r["unrenounced_freeze_warning"], 0)

    def test_review_freeze(self):
        self.store.upsert(_make_rec("FreezeHeld11111111111111111111111111111111",
                                    freeze_auth="SomeAuthorityPubkeyXXXXXXXXXXXXXXXXX"))
        r = self.store.authority_risk("FreezeHeld11111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "review_freeze")
        self.assertEqual(r["unrenounced_mint_warning"], 0)
        self.assertEqual(r["unrenounced_freeze_warning"], 1)

    def test_hard_avoid_t22_perm_delegate(self):
        self.store.upsert(_make_rec("T22PermDel1111111111111111111111111111111111",
                                    is_token_2022=True, has_permanent_delegate=True))
        r = self.store.authority_risk("T22PermDel1111111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "avoid")
        self.assertEqual(r["t22_perm_delegate_hard_avoid"], 1)

    def test_hard_avoid_with_all_flags_still_avoid(self):
        self.store.upsert(_make_rec("T22AllBells1111111111111111111111111111111111",
                                    mint_auth=KNOWN_GOOD_MINT_AUTH,
                                    freeze_auth=KNOWN_GOOD_MINT_AUTH,
                                    is_token_2022=True, has_permanent_delegate=True))
        r = self.store.authority_risk("T22AllBells1111111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "avoid")
        self.assertEqual(r["unrenounced_mint_warning"], 1)
        self.assertEqual(r["unrenounced_freeze_warning"], 1)

    def test_t22_without_perm_delegate_is_not_hard_avoid(self):
        self.store.upsert(_make_rec("T22SafeExt1111111111111111111111111111111111",
                                    is_token_2022=True, has_permanent_delegate=False))
        r = self.store.authority_risk("T22SafeExt1111111111111111111111111111111111")
        self.assertEqual(r["authority_verdict"], "safe")
        self.assertEqual(r["t22_perm_delegate_hard_avoid"], 0)

    def test_error_row_excluded(self):
        self.store.upsert(TokenAuthority(token_mint="ErrMint1111111111111111111111111111111111",
                                         error="not_found"))
        self.assertIsNone(self.store.authority_risk("ErrMint1111111111111111111111111111111111"))


# --- Address label store tests ------------------------------------------------

class TestAddressLabelStore(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name
        self.store = TokenAuthorityStore(self.db)

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def test_add_and_count(self):
        self.assertEqual(self.store.address_label_count(), 0)
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH, "known_good_authority",
                                     entity="Circle", source="helius_live")
        self.assertEqual(self.store.address_label_count(), 1)
        self.assertEqual(self.store.address_label_count("known_good_authority"), 1)
        self.assertEqual(self.store.address_label_count("cex_hot"), 0)

    def test_add_is_idempotent(self):
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH, "known_good_authority",
                                     entity="Circle", confidence="high")
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH, "known_good_authority",
                                     entity="Circle Inc", confidence="high", notes="Updated")
        self.assertEqual(self.store.address_label_count(), 1)
        cur = self.store._conn.execute(
            "SELECT entity, notes FROM dim_address_label WHERE address = ?",
            (KNOWN_GOOD_MINT_AUTH,),
        )
        row = cur.fetchone()
        self.assertEqual(row["entity"], "Circle Inc")
        self.assertEqual(row["notes"], "Updated")

    def test_bulk_load(self):
        entries = [
            {"address": KNOWN_GOOD_MINT_AUTH,    "kind": "known_good_authority",
             "entity": "Circle",  "source": "helius_live",   "confidence": "high"},
            {"address": KNOWN_GOOD_FREEZE_AUTH,  "kind": "known_good_authority",
             "entity": "Tether",  "source": "helius_live",   "confidence": "high"},
            {"address": "11111111111111111111111111111112", "kind": "burn",
             "entity": "System",  "source": "official_docs", "confidence": "high"},
        ]
        self.assertEqual(self.store.load_address_labels(entries), 3)
        self.assertEqual(self.store.address_label_count(), 3)
        self.assertEqual(self.store.address_label_count("known_good_authority"), 2)

    def test_inactive_label_does_not_count(self):
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH, "known_good_authority", active=True)
        self.assertEqual(self.store.address_label_count(), 1)
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH, "known_good_authority", active=False)
        self.assertEqual(self.store.address_label_count(), 0)


# --- Effective authority verdict view tests -----------------------------------

class TestEffectiveVerdict(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db = f.name
        self.store = TokenAuthorityStore(self.db)
        self.store.add_address_label(KNOWN_GOOD_MINT_AUTH,   "known_good_authority",
                                     entity="Circle", source="helius_live", confidence="high")
        self.store.add_address_label(KNOWN_GOOD_FREEZE_AUTH, "known_good_authority",
                                     entity="Tether", source="helius_live", confidence="high")

    def tearDown(self):
        self.store.close()
        for sfx in ("", "-wal", "-shm", "-journal"):
            Path(self.db + sfx).unlink(missing_ok=True)

    def test_whitelisted_mint_authority_resolves_to_safe(self):
        mint = _mint("USDC")
        self.store.upsert(_make_rec(mint,
                                    mint_auth=KNOWN_GOOD_MINT_AUTH,
                                    freeze_auth=KNOWN_GOOD_FREEZE_AUTH))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["base_verdict"], "review_mint")
        self.assertEqual(eff["effective_verdict"], "safe")
        self.assertEqual(eff["mint_authority_entity"], "Circle")
        self.assertEqual(eff["freeze_authority_entity"], "Tether")
        self.assertEqual(eff["mint_authority_confidence"], "high")

    def test_unknown_authority_stays_review(self):
        mint = _mint("UnknownAuth")
        self.store.upsert(_make_rec(mint, mint_auth=UNKNOWN_AUTH))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["base_verdict"], "review_mint")
        self.assertEqual(eff["effective_verdict"], "review_mint")
        self.assertIsNone(eff["mint_authority_entity"])

    def test_renounced_authority_is_safe(self):
        mint = _mint("Renounced")
        self.store.upsert(_make_rec(mint))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["base_verdict"], "safe")
        self.assertEqual(eff["effective_verdict"], "safe")

    def test_t22_perm_delegate_cannot_be_whitelisted(self):
        mint = _mint("T22Trap")
        self.store.upsert(_make_rec(mint,
                                    mint_auth=KNOWN_GOOD_MINT_AUTH,
                                    freeze_auth=KNOWN_GOOD_FREEZE_AUTH,
                                    is_token_2022=True, has_permanent_delegate=True))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["base_verdict"], "avoid")
        self.assertEqual(eff["effective_verdict"], "avoid")
        self.assertEqual(eff["mint_authority_entity"], "Circle")
        self.assertEqual(eff["t22_perm_delegate_hard_avoid"], 1)

    def test_only_mint_whitelisted_resolves_to_safe(self):
        mint = _mint("MixedMint")
        self.store.upsert(_make_rec(mint, mint_auth=KNOWN_GOOD_MINT_AUTH, freeze_auth=UNKNOWN_AUTH))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["effective_verdict"], "safe")
        self.assertIsNone(eff["freeze_authority_entity"])

    def test_renounced_mint_whitelisted_freeze_resolves_to_safe(self):
        mint = _mint("MixedFreeze")
        self.store.upsert(_make_rec(mint, freeze_auth=KNOWN_GOOD_FREEZE_AUTH))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["base_verdict"], "review_freeze")
        self.assertEqual(eff["effective_verdict"], "safe")
        self.assertEqual(eff["freeze_authority_entity"], "Tether")

    def test_inactive_label_does_not_resolve(self):
        inactive_addr = "InactiveAddress11111111111111111111111111111"
        self.store.add_address_label(inactive_addr, "known_good_authority",
                                     entity="Test", active=True)
        self.store.add_address_label(inactive_addr, "known_good_authority",
                                     entity="Test", active=False)
        mint = _mint("InactiveTest")
        self.store.upsert(_make_rec(mint, mint_auth=inactive_addr))
        eff = self.store.effective_authority_verdict(mint)
        self.assertEqual(eff["effective_verdict"], "review_mint")
        self.assertIsNone(eff["mint_authority_entity"])


# --- Seed file structure tests ------------------------------------------------

class TestSeedFileStructure(unittest.TestCase):
    def test_seed_loads(self):
        from seed_address_labels import get_seed
        entries = get_seed()
        self.assertIsInstance(entries, list)
        self.assertGreater(len(entries), 0)
        valid = set(string.ascii_letters + string.digits)
        for e in entries:
            self.assertIn("address", e)
            self.assertIn("kind", e)
            self.assertGreaterEqual(len(e["address"]), 32)
            self.assertLessEqual(len(e["address"]), 44)
            self.assertTrue(all(c in valid for c in e["address"]),
                            f"Non-base58 chars in address: {e['address']}")

    def test_seed_has_live_observed_addresses(self):
        from seed_address_labels import get_seed
        addrs = {e["address"] for e in get_seed()}
        for required in [
            "BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG",  # USDC mint (Circle)
            "7dGbd2QZcCKcTndnHcTL8q7SMVXAkp688NTQYwrRCrar",  # USDC freeze (Circle)
            "Q6XprfkF8RQQKoQVG33xT88H7wi8Uk1B1CC7YAs69Gi",   # USDT mint+freeze
            "3JLPCS1qM2zRw3Dp6V4hZnYHd4toMNPkNesXdX9tg6KM",  # mSOL mint (Marinade)
        ]:
            self.assertIn(required, addrs, f"Required live-observed address missing: {required}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
