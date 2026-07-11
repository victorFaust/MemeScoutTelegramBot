"""Test script for safety_check.py — verify against known token addresses."""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

import safety_check

# Known tokens to test against:
# 1. USDC on Solana (should be safe)
# 2. A known token on Base (BRETT - popular memecoin)

TEST_CASES = [
    ("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC (Solana)"),
    ("base", "0x532f27101965dd16442e59d40670faf5ebb142e4", "BRETT (Base)"),
]


def main():
    print("=" * 60)
    print("Safety Check Test Script")
    print("=" * 60)

    for chain_id, token_address, label in TEST_CASES:
        print(f"\n--- Testing: {label} ---")
        print(f"Chain: {chain_id}, Address: {token_address}")

        should_alert, safety_data = safety_check.evaluate_safety(chain_id, token_address)

        print(f"Should alert: {should_alert}")
        if safety_data:
            print(f"Risk label: {safety_data.get('risk_label', 'N/A')}")
            print(f"Is honeypot: {safety_data.get('is_honeypot')}")
            print(f"Buy tax: {safety_data.get('buy_tax_pct')}%")
            print(f"Sell tax: {safety_data.get('sell_tax_pct')}%")
            print(f"Mint authority active: {safety_data.get('mint_authority_active')}")
            print(f"Has blacklist: {safety_data.get('has_blacklist')}")
            print(f"Top 10 holder %: {safety_data.get('top10_holder_pct')}")
        else:
            print("No safety data returned (API failure or unsupported chain)")

    print("\n" + "=" * 60)
    print("Test complete.")


if __name__ == "__main__":
    main()
