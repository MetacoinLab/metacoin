"""x402_spend_stub.py — simulated x402-class micropayment for the Phase 1 agentic demo.

================================ STUB / NO REAL VALUE ================================
This is a STUB. It simulates an x402-class micropayment to "buy compute" for an agent's
next task. There is NO real x402 network, NO real HTTP 402 flow, NO real payment, and NO
real value changes hands. The only thing moved is ZERO-VALUE Test-META, a testnet
placeholder that is not MetaCoin and not base supply (MIP-0001 paragraph 3, MIP-0002
paragraph 8). The "compute" granted is imaginary. Research-only; not financial advice.

Integrity rule: this stub NEVER touches the faucet ledger directly. It spends only by
calling the faucet's guarded spend(), which owns all balance changes — mirroring the
dispense() guarantee. If the spend is refused (insufficient balance / bad amount), no
compute is granted.
=====================================================================================

Standard library only.
"""

# Simulated, deterministic conversion: 1 zero-value Test-META buys 1 imaginary compute
# unit. This is illustrative only and has no real-world meaning.
COMPUTE_UNITS_PER_TEST_META = 1


def buy_compute(faucet, address: str, cost: int) -> dict:
    """Simulate buying compute by spending zero-value Test-META via the faucet.

    Goes through faucet.spend(address, cost) ONLY — no direct ledger access. On a
    successful spend, returns a simulated compute-purchase receipt. On a failed spend
    (e.g. insufficient balance), grants no compute and returns the failure reason.

    `faucet` is any object exposing the faucet's public API (spend / balance_of) — e.g.
    the demo.test_meta_faucet module or a _Faucet instance.
    """
    result = faucet.spend(address, cost)

    if not result.get("spent"):
        # Spend refused by the faucet — grant nothing.
        return {
            "purchased": False,
            "reason": result.get("reason", "spend refused"),
            "balance": result.get("balance"),
        }

    compute_units = cost * COMPUTE_UNITS_PER_TEST_META
    return {
        "purchased": True,
        "compute_units": compute_units,
        "cost": cost,
        "remaining_balance": result["new_balance"],
        "note": "simulated x402 micropayment, zero value",
    }


if __name__ == "__main__":
    import os
    import sys

    # Make absolute imports resolve when run directly (repo root on sys.path).
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

    # Use the faucet MODULE as the `faucet` object: it exposes dispense/spend/balance_of
    # as its public API and keeps the ledger private. The stub never reaches past spend().
    import demo.test_meta_faucet as faucet
    from demo.tasks.task_0001_lunar_link_budget import compute, output_hash

    ADDR = "agent-testnet-x402"

    # Fund the address with zero-value Test-META via a verified honest dispense.
    honest_result = compute()
    honest_submission = {
        "result": honest_result,
        "claimed_output_hash": output_hash(honest_result),
    }

    print("=== x402_spend_stub.py self-test (simulated micropayment; zero-value Test-META) ===\n")

    ok = []

    print("--- setup: fund via verified dispense ---")
    fund = faucet.dispense(ADDR, honest_submission, amount=5)  # 0 -> 5
    bal = faucet.balance_of(ADDR)
    print(f"  dispensed      : {fund['dispensed']} (amount {fund.get('amount')})")
    print(f"  balance        : {bal} (expected 5)")
    setup_ok = fund["dispensed"] is True and bal == 5
    print(f"  self-test      : {'OK' if setup_ok else 'WRONG'}")
    ok.append(setup_ok)
    print()

    # (a) buy_compute WITHIN balance -> succeeds, balance drops, compute granted.
    print("--- (a) buy_compute within balance (cost=3) ---")
    before_a = faucet.balance_of(ADDR)
    receipt_a = buy_compute(faucet, ADDR, 3)
    after_a = faucet.balance_of(ADDR)
    print(f"  balance before : {before_a}")
    print(f"  receipt        : {receipt_a}")
    print(f"  balance after  : {after_a}")
    case_a_ok = (
        before_a == 5
        and receipt_a["purchased"] is True
        and receipt_a["compute_units"] == 3
        and receipt_a["remaining_balance"] == 2
        and after_a == 2
    )
    print(f"  self-test      : {'OK (compute granted; balance 5 -> 2)' if case_a_ok else 'WRONG'}")
    ok.append(case_a_ok)
    print()

    # (b) buy_compute EXCEEDING balance -> refused, no compute, balance unchanged.
    print("--- (b) buy_compute exceeding balance (cost=10) ---")
    before_b = faucet.balance_of(ADDR)
    receipt_b = buy_compute(faucet, ADDR, 10)
    after_b = faucet.balance_of(ADDR)
    print(f"  balance before : {before_b}")
    print(f"  receipt        : {receipt_b}")
    print(f"  balance after  : {after_b}")
    case_b_ok = (
        receipt_b["purchased"] is False
        and receipt_b["reason"].startswith("insufficient balance")
        and "compute_units" not in receipt_b
        and after_b == before_b == 2
    )
    print(f"  self-test      : {'OK (refused; no compute; balance unchanged)' if case_b_ok else 'WRONG'}")
    ok.append(case_b_ok)
    print()

    all_ok = all(ok)
    print("=== self-test summary: " + ("ALL CASES BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    sys.exit(0 if all_ok else 1)
