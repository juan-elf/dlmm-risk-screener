"""Pull the FULL mint/freeze authority addresses from mainnet, with NO truncation.
Output is a JSON manifest we can use to build the seed file.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
from dim_token_authority import fetch_authority

RPC = os.environ["HELIUS_RPC_URL"]

mints = {
    "USDC":  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT":  "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSOL":  "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSOL":  "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "ORCA":  "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "RENDER":"rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
}

result = {}
for name, mint in mints.items():
    rec = fetch_authority(RPC, mint, timeout=10)
    result[name] = {
        "mint": mint,
        "mint_authority_full": rec.mint_authority,
        "freeze_authority_full": rec.freeze_authority,
    }
    print(f"{name}:")
    print(f"  mint_authority:   {rec.mint_authority}")
    print(f"  freeze_authority: {rec.freeze_authority}")

# Save to JSON for the seed builder to consume
out_path = os.path.join(os.path.dirname(__file__), "authority_seeds_live.json")
with open(out_path, "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to {out_path}")
