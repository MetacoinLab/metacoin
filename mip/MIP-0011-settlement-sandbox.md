# MIP-0011 — The settlement sandbox: a proposal to keep an EVM experiment outside the money layer (DRAFT, not ratified)

**Status:** Draft · **Layer:** Protocol · **Supersedes:** none · **Depends on:** MIP-0001, MIP-0002
**Note:** Research specification only. No token exists. Not financial or legal advice. This file is a proposal for discussion; no decision on it has been recorded or anchored, and nothing in it is law.

> **THE DOC CONTRACT.** This draft makes no typed ledger citation and carries no executable verification block; it describes code that lives outside the protocol core and states what a future ratifying MIP would have to contain. It becomes immutable-by-citation only if a decision on it is ever anchored.

## Summary

An EVM settlement experiment (x402 `upto` on a private py-evm chain) exists inside the optional work service. This draft records that it is not the protocol's money layer, restates the two laws that keep it outside (zero-value, chain-agnostic), and lists what a future ratifying MIP would have to contain before any settlement adapter could become law. It proposes no change to any ratified MIP.

## Motivation

A reader of the repository can now find Permit2 authorizations, a facilitator and settlement transactions next to the protocol's "no token, zero-value" statements. Without a recorded position, that juxtaposition invites the wrong inference. The honest description is: *a settlement sandbox exists as an experiment on a private test chain; the money layer remains a specification and zero-value.* This draft makes that position explicit and checkable.

## Specification

### 1. What exists today (facts, not law)

- An optional application stack, `metacoin_service/`, ships as the `service` extra of the same repository. The protocol core (`protocol/`, `metacoin_cli/`) is standard-library only and never imports it.
- Inside that stack a **settlement sandbox** exists: the pinned x402 Python SDK's `upto` scheme (a Permit2 witness authorization up to a ceiling, settled later for a measured amount) runs against a **private py-evm chain** deployed in-process from pinned contract sources (Permit2, x402UptoPermit2Proxy, a mock ERC-20). The chain id is private, nothing is broadcast, the mock token is minted to synthetic accounts, and the SDK's canonical contract addresses are redirected to the local deployments for the run.
- A second mode labels requests with the CAIP-2 id of a public testnet (`eip155:84532`) but talks only to an in-process facilitator double; no network is contacted. A production mode exists as code (an HTTP facilitator client) and has never been configured or exercised.
- Every such file carries the label: *EXPERIMENTAL — private test chain only — not the protocol's money layer, which remains [SPEC] and zero-value by MIP-0001/0002; chain-agnostic law unchanged.*

Nothing above mints base supply, transfers value, creates a token, or touches the published ledger or the identity text.

### 2. The two laws this proposal keeps

1. **Zero-value law.** MIP-0001 §3 and §6 fix that base supply mints only for objectively verifiable work and that no premine or allocation exists; MIP-0002 §8 fixes that nothing in the verification engine mints base supply and that payouts, if any, come from the fee-funded treasury (MIP-0001 §5). Test-META is a zero-value placeholder. The sandbox's mock token is not Test-META, not META, and not convertible into either.
2. **Chain-agnostic requirement.** No ratified MIP names a chain. MIP-0002 §1 keeps integrity proofs vendor-agnostic; the whitepaper's "fast, cheap chain with x402-class micropayments" is a deployment sketch, not law. This proposal states the requirement explicitly: protocol law may specify a **settlement interface** (amounts, recipients, verification gates, receipts) but never a chain, a token contract, or a signing scheme. Any adapter is one implementation among several and lives outside the protocol core.

### 3. What would have to change for the sandbox to become the money layer

The sandbox is not that layer, and this draft does not propose making it one. If a future coordinator wanted to, all of the following would be required, each as its own recorded decision:

1. A ratified MIP defining the settlement **interface** (chain-agnostic, CAIP-2 pluggable), with the three MIP-0002 gates as preconditions of any payout and the treasury separation of MIP-0001 §5 preserved.
2. The securities and compliance review MIP-0001 §9 requires before any test-to-live conversion; the current license (SML-1.0) forbids operating a public testnet or mainnet and would need an amendment recorded by its owner.
3. Removal of the service from the zero-dependency install path (today `metacoin_service/` is listed in the wheel's packages although unused by the protocol) so that the protocol package never carries settlement code.
4. Independent operators: every settlement observed so far is same-operator on one host, which proves reproducibility, not a market.
5. Evidence from a real facilitator on a named network under a ratified adapter, separated in the ledger from the private-chain evidence, with the reserved/assessed/settled amounts reconciled per job.
6. An explicit repeal or amendment of the zero-value framing for whatever asset settles, which MIP-0001 §7 says can only happen by a transparent on-chain MIP.

Until every item is met, the honest public description is: *a settlement sandbox exists as an experiment on a private test chain; the protocol's money layer remains a specification and zero-value.*

## Backwards compatibility

Nothing changes for any ratified MIP, the published ledger, the identity text or the zero-dependency install: the protocol core does not import the sandbox and the sandbox writes only to its own service home. Removing the sandbox would remove no protocol capability.

## Honest limitations

- The sandbox's evidence is same-operator, single-host, private-chain evidence; it proves that the pinned SDK and contracts behave as documented locally, not that any market, facilitator or network exists.
- One mode labels requests with a public testnet's CAIP-2 id while talking to an in-process double; the id is a label, not a connection.
- The production branch (HTTP facilitator client) is unexercised code.
- This draft carries no decision; a coordinator may reject it, and rejecting it changes nothing either.

## Verification

The protocol core never imports the sandbox (checked on every CI run by the block below, which needs only the standard library). The sandbox's own behaviour (private chain id is not a public network id; over-maximum, wrong-recipient, wrong-domain and replayed authorizations are refused; a failed job leaves its authorization unused) is asserted by `integrations/x402/local_chain/test_local_chain.py` and `metacoin_service/tests/test_upto_route.py`, which run in the service environment (`pip install -e '.[service]'`), not here.

```verify-run
$ python3 -c "import pathlib; print(sorted({p.name for d in ('protocol', 'metacoin_cli') for p in pathlib.Path(d).glob('*.py') if 'metacoin_service' in p.read_text() or 'integrations.x402' in p.read_text()}))"
[]
```
<!--expect:[]-->

## Decision

None recorded. This is a draft for the coordinator's decision; it is not ratified, not anchored, and creates no obligation.
