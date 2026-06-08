"""Collect authority addresses from a broad set of known Solana mints,
to inform the dim_address_label seed. This is a research/seed-generation
script, not a production tool.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from dim_token_authority import fetch_authority

RPC = os.environ["HELIUS_RPC_URL"]

# Curated list of well-known Solana tokens across categories.
# Goal: collect authority addresses for known-good issuers so we can whitelist them.
mints = {
    # Stablecoins
    "USDC":   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT":   "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "PYUSD":  "2b1kV6DkBMAn91yxyPziCGRUTEqJQiGkCv7GRCySFc4S",
    # LSTs (Liquid Staking Tokens)
    "mSOL":     "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "JitoSOL":  "J1toso1uCk3RLmjorhTtrVwY9HJ6X1V6YGYKB7iGs8XT",
    "bSOL":     "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "INF":      "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2ZR4SmvJzr",
    # DEX / aggregator tokens
    "JUP":   "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "JTO":   "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "JLP":   "27G8MtK7VtTcCHKPASxKeg8W29BBHMp4qJ1sQTKW4Q7K",
    # Memes (mostly renounced)
    "BONK":  "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":   "EKpQGSJtjMFqKZ9KQanSqYXR2F8pB5Ak5K9hQgbYNH3F",
    # Bridge
    "W (Wormhole)": "85VBFQZC9TZkfaptBWjvL7gkX5JsdWcdnRBGaf8gSVzm",
    # Wrapped
    "Wrapped SOL":  "So11111111111111111111111111111111111111112",
    # Pyth
    "PYTH":         "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    # Misc protocols
    "MNGO":         "MangoCzJ36AjZyKwVj3VnYU4GTonjfQEnL8otFo5bh5g",
    "ORCA":         "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "SRM":          "SRMuApVNdxXokk5GT7XD5cUUgXMBCoAz2LHeuAoKWRt",
    "RENDER":       "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "DUST":         "DUSTawucrGhGU2XCx5Y5K7RpY1xjLZ5WXKz4YH5p3Bm",
    "GECKO":        "CzV7bKz8ErUVD8vYDsQntWGiGfPEHHJVDcUBZHh1pump",
}

print(f"{'NAME':22s} {'MINT_AUTH':18s} {'FREEZE_AUTH':18s} {'PROG':5s} T22")
print("-" * 90)
seen_auths = set()
for name, mint in mints.items():
    try:
        rec = fetch_authority(RPC, mint, timeout=10)
        ma = (rec.mint_authority or "-")[:18]
        fa = (rec.freeze_authority or "-")[:18]
        prog = (rec.program_id or "?")[-5:]
        t22 = rec.is_token_2022
        err = rec.error or ""
        if ma != "-": seen_auths.add((name, "mint", ma))
        if fa != "-": seen_auths.add((name, "freeze", fa))
        print(f"{name:22s} {ma:18s} {fa:18s} {prog:5s} {t22!s:5s} {err[:20]}")
    except Exception as e:
        print(f"{name:22s} ERROR: {str(e)[:60]}")

print("\n--- Unique authority addresses seen ---")
for name, kind, addr in sorted(seen_auths, key=lambda x: x[2]):
    print(f"  {addr:18s} ({kind} of {name})")
