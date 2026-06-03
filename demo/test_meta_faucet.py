"""test_meta_faucet.py — zero-value Test-META faucet for the Phase 1 agentic demo.

================================ INVARIANTS (READ ME) ================================
Test-META is a ZERO-VALUE TESTNET PLACEHOLDER. It is NOT MetaCoin. It is NOT base
supply. It has NO monetary value, NO price, and is NEVER tradable. It exists only to
model the demo's earn->spend loop in software. Dispensing Test-META mints nothing on the
real protocol and can never affect base emission (MIP-0001 paragraph 3, MIP-0002
paragraph 8). Research-only; not financial or legal advice.

The faucet dispenses ONLY when verification passes. There is no code path that grants
Test-META without a passing verify() result: crediting is a name-mangled private method
of the faucet, reachable only from inside the dispense() path, which first calls
demo.verify_gates.verify(submission) and proceeds solely when result["passed"] is True.

The faucet owns ALL balance changes — credits (dispense, gated by verify) and debits
(spend, gated by a sufficiency check). Both the credit and the debit are name-mangled
private methods; external code (e.g. the x402 stub) must call spend(), never the ledger.
=====================================================================================

Standard library only. In-memory ledger only — no persistence, no network.
"""

import os
import sys

# Make absolute imports resolve when this file is run directly (repo root on sys.path;
# `demo` is a Python 3 namespace package, no __init__.py needed).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from demo.verify_gates import verify


class _Faucet:
    """Zero-value Test-META faucet.

    The ledger and the crediting operation are private. The ONLY way balances change is
    through dispense(), which gates every credit behind a passing verify() result. There
    is deliberately no public credit method.
    """

    def __init__(self):
        # Private in-memory ledger: address -> integer Test-META balance (zero value).
        self.__ledger = {}

    def __credit(self, address: str, amount: int) -> int:
        """PRIVATE (name-mangled). Add zero-value Test-META to an address balance.

        This is intentionally unreachable as a public attribute. It must only ever be
        called from dispense(), after verification has passed. It performs no verification
        itself and must never be wired to any public entry point.
        """
        self.__ledger[address] = self.__ledger.get(address, 0) + int(amount)
        return self.__ledger[address]

    def balance_of(self, address: str) -> int:
        """Read-only: current zero-value Test-META balance for an address."""
        return self.__ledger.get(address, 0)

    def dispense(self, address: str, submission: dict, amount: int = 1, task=None) -> dict:
        """Dispense zero-value Test-META IF AND ONLY IF verification passes.

        Calls verify(submission, task) (Gate-1 stand-in + Gate-2 reproducibility) against
        the given task (module or identifier; default task-0001). Only when the combined
        result is passed=True does it credit `amount` zero-value Test-META to `address`.
        On any verification failure it credits NOTHING.
        """
        verify_result = verify(submission, task)

        if not verify_result["passed"]:
            # No-credit path. Balance is untouched.
            return {
                "dispensed": False,
                "reason": "verification failed — no Test-META dispensed (zero-value faucet only credits verified work)",
                "verify_result": verify_result,
            }

        # Verified path — the ONLY place __credit is ever called.
        new_balance = self.__credit(address, amount)
        return {
            "dispensed": True,
            "address": address,
            "amount": amount,
            "new_balance": new_balance,
            "verify_result": verify_result,
        }

    def __debit(self, address: str, amount: int) -> int:
        """PRIVATE (name-mangled). Subtract zero-value Test-META from an address balance.

        Intentionally unreachable as a public attribute. Only ever called from spend(),
        after the sufficiency and positivity checks. It performs no validation itself.
        """
        self.__ledger[address] = self.__ledger.get(address, 0) - int(amount)
        return self.__ledger[address]

    def spend(self, address: str, amount: int) -> dict:
        """Guarded debit. Spends zero-value Test-META only if the balance is sufficient.

        Rejects non-positive amounts. Debits NOTHING on rejection. The faucet owns all
        balance changes; external code (e.g. the x402 stub) must call this method rather
        than touching the ledger — mirroring the dispense() integrity guarantee.
        """
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return {
                "spent": False,
                "reason": "amount must be a positive integer",
                "balance": self.balance_of(address),
            }

        current = self.balance_of(address)
        if amount > current:
            # Insufficient-balance path. Balance is untouched.
            return {
                "spent": False,
                "reason": "insufficient balance",
                "balance": current,
            }

        # Sufficient — the ONLY place __debit is ever called.
        new_balance = self.__debit(address, amount)
        return {
            "spent": True,
            "address": address,
            "amount": amount,
            "new_balance": new_balance,
        }


# Module-level singleton faucet and public API. The public surface is exactly two
# functions: dispense() and balance_of(). There is no module-level credit function.
_FAUCET = _Faucet()


def dispense(address: str, submission: dict, amount: int = 1, task=None) -> dict:
    """Public entry point. Dispenses zero-value Test-META only on a passing verify().

    `task` (module or identifier) selects which task to verify against; default task-0001.
    """
    return _FAUCET.dispense(address, submission, amount, task)


def spend(address: str, amount: int) -> dict:
    """Public guarded debit. Spends zero-value Test-META only if the balance is sufficient."""
    return _FAUCET.spend(address, amount)


def balance_of(address: str) -> int:
    """Public read-only balance lookup."""
    return _FAUCET.balance_of(address)


if __name__ == "__main__":
    import copy

    from demo.tasks.task_0001_lunar_link_budget import compute, output_hash

    ADDR = "agent-testnet-0001"

    # Build an HONEST submission (real run + correct hash).
    honest_result = compute()
    honest_submission = {
        "result": honest_result,
        "claimed_output_hash": output_hash(honest_result),
    }

    # Build a TAMPERED submission (alter one number, keep the stale hash).
    tampered_submission = {
        "result": copy.deepcopy(honest_result),
        "claimed_output_hash": output_hash(honest_result),
    }
    tampered_submission["result"]["results"][0]["link_margin_dB"] += 1.0

    print("=== test_meta_faucet.py self-test (zero-value Test-META; credits only verified work) ===\n")

    ok = []

    # (a) HONEST -> must DISPENSE, balance 0 -> amount.
    print("--- (a) HONEST submission ---")
    before_a = balance_of(ADDR)
    res_a = dispense(ADDR, honest_submission, amount=1)
    after_a = balance_of(ADDR)
    print(f"  balance before : {before_a}")
    print(f"  dispensed      : {res_a['dispensed']}")
    print(f"  amount         : {res_a.get('amount')}")
    print(f"  balance after  : {after_a}")
    case_a_ok = (before_a == 0) and (res_a["dispensed"] is True) and (after_a == 1)
    print(f"  self-test      : {'OK (dispensed; balance 0 -> 1)' if case_a_ok else 'WRONG'}")
    ok.append(case_a_ok)
    print()

    # (b) TAMPERED -> must REFUSE, balance unchanged.
    print("--- (b) TAMPERED submission ---")
    before_b = balance_of(ADDR)
    res_b = dispense(ADDR, tampered_submission, amount=1)
    after_b = balance_of(ADDR)
    print(f"  balance before : {before_b}")
    print(f"  dispensed      : {res_b['dispensed']}")
    print(f"  reason         : {res_b.get('reason')}")
    print(f"  gate2          : {res_b['verify_result']['gate2']['reason']}")
    print(f"  balance after  : {after_b}")
    case_b_ok = (res_b["dispensed"] is False) and (after_b == before_b)
    print(f"  self-test      : {'OK (refused; balance unchanged)' if case_b_ok else 'WRONG'}")
    ok.append(case_b_ok)
    print()

    # (c) No verify-bypassing public credit path.
    print("--- (c) No verify-bypassing public credit path ---")
    import demo.test_meta_faucet as _mod

    module_has_credit = any(
        name in dir(_mod) for name in ("credit", "_credit", "grant", "mint")
    )
    print(f"  module-level credit/grant/mint function exposed? : {module_has_credit}")

    faucet_public_credit = getattr(_FAUCET, "credit", None)
    print(f"  _FAUCET.credit attribute                         : {faucet_public_credit}")

    # The private method is name-mangled; natural access fails.
    try:
        getattr(_FAUCET, "__credit")
        natural_access = "REACHABLE (unexpected)"
    except AttributeError:
        natural_access = "AttributeError (not reachable as __credit)"
    print(f"  natural access _FAUCET.__credit                  : {natural_access}")

    # Public surface that can change a balance:
    public_balance_changers = [
        name for name in dir(_mod)
        if callable(getattr(_mod, name)) and name in ("dispense",)
    ]
    print(f"  public balance-changing entry point(s)           : {public_balance_changers}")

    # Prove the gate holds: dispensing a tampered submission via the public API never credits.
    bal_before_c = balance_of("probe-addr")
    _ = dispense("probe-addr", tampered_submission, amount=999)
    bal_after_c = balance_of("probe-addr")
    print(f"  attempt to dispense tampered work (amount=999)   : balance {bal_before_c} -> {bal_after_c}")

    case_c_ok = (
        not module_has_credit
        and faucet_public_credit is None
        and natural_access.startswith("AttributeError")
        and public_balance_changers == ["dispense"]
        and bal_after_c == bal_before_c == 0
    )
    print(f"  self-test      : {'OK (only dispense() can credit, and only on verify pass)' if case_c_ok else 'WRONG'}")
    print("  note           : Python has no hard private; crediting is a name-mangled "
          "method called only inside dispense() after verify() passes. The supported "
          "public API is dispense() + balance_of() only.")
    ok.append(case_c_ok)
    print()

    # Spend cases use a freshly funded address so balances are easy to read.
    SPEND_ADDR = "agent-testnet-spend"
    fund = dispense(SPEND_ADDR, honest_submission, amount=3)  # verified credit, 0 -> 3
    print("--- spend setup ---")
    print(f"  funded {SPEND_ADDR} via verified dispense: balance = {balance_of(SPEND_ADDR)} (expected 3)")
    print()

    # (d) SPEND within balance -> succeeds, balance drops.
    print("--- (d) SPEND within balance ---")
    before_d = balance_of(SPEND_ADDR)
    res_d = spend(SPEND_ADDR, 2)
    after_d = balance_of(SPEND_ADDR)
    print(f"  balance before : {before_d}")
    print(f"  spend(2)       : {res_d}")
    print(f"  balance after  : {after_d}")
    case_d_ok = (before_d == 3) and (res_d["spent"] is True) and (after_d == 1)
    print(f"  self-test      : {'OK (spent; balance 3 -> 1)' if case_d_ok else 'WRONG'}")
    ok.append(case_d_ok)
    print()

    # (e) SPEND exceeding balance -> refused, balance unchanged.
    print("--- (e) SPEND exceeding balance ---")
    before_e = balance_of(SPEND_ADDR)
    res_e = spend(SPEND_ADDR, 5)
    after_e = balance_of(SPEND_ADDR)
    print(f"  balance before : {before_e}")
    print(f"  spend(5)       : {res_e}")
    print(f"  balance after  : {after_e}")
    case_e_ok = (res_e["spent"] is False) and (res_e["reason"] == "insufficient balance") and (after_e == before_e)
    print(f"  self-test      : {'OK (refused; balance unchanged)' if case_e_ok else 'WRONG'}")
    ok.append(case_e_ok)
    print()

    # (f) SPEND of a non-positive amount -> refused.
    print("--- (f) SPEND non-positive amount ---")
    before_f = balance_of(SPEND_ADDR)
    res_zero = spend(SPEND_ADDR, 0)
    res_neg = spend(SPEND_ADDR, -5)
    after_f = balance_of(SPEND_ADDR)
    print(f"  balance before : {before_f}")
    print(f"  spend(0)       : {res_zero}")
    print(f"  spend(-5)      : {res_neg}")
    print(f"  balance after  : {after_f}")
    case_f_ok = (res_zero["spent"] is False) and (res_neg["spent"] is False) and (after_f == before_f)
    print(f"  self-test      : {'OK (both refused; balance unchanged)' if case_f_ok else 'WRONG'}")
    ok.append(case_f_ok)
    print()

    all_ok = all(ok)
    print("=== self-test summary: " + ("ALL CASES BEHAVED CORRECTLY" if all_ok else "FAILURE — see above") + " ===")
    sys.exit(0 if all_ok else 1)
