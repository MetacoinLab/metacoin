# Collaborate

> **THE DOC CONTRACT.** Everything in this document is [BUILT] fact,
> mechanically verified by protocol/doc_verify.py on every CI run: the
> command below is executed in a fresh-clone sandbox, and every ledger index
> cited is checked to exist on the chain with exactly the stated event type.
> This document asks for partnership in kind. It does not ask for money, and
> it has nothing to sell.

## What this project is

MetaCoin is a research-stage protocol for verifiable machine work: an
append-only public ledger on which every claim — task outputs, provenance
molecules, concentration self-measurements, economy simulations, governance
decisions, and staged attacks with their refusals — re-derives mechanically
from a fresh clone, with no token, no payments, and every limitation stated
on the record it limits. The whole stack is standard-library Python,
verified end-to-end by CI on every push. Fifteen minutes with
[`docs/TOUR.md`](TOUR.md) lets you verify everything yourself, including
the project's own negative results — that is the intended first step for
any potential partner, before any conversation.

## What we are looking for

Partnership in kind: hardware and validation contexts our single-operator
research bench honestly cannot provide. In each case the machinery is
already built and CI-exercised — your hardware plugs in and produces
anchored, independently verifiable evidence from day one.

- **Compute time on hosts with real power sensors.** Per-task energy is
  currently an ESTIMATE (CPU time × an assumed power figure), and the
  anchored records say so. The measurement machinery exists — a read-only
  sensor probe with an empirical load-response screen and a sustained-load
  characterization path (`protocol/power_telemetry.py`) — but this host's
  honest probe verdict is that no readable power sensor observes its CPU
  domain, so the telemetry debt remains open (idx 20
  <!--idx:20=metering_evidence_anchored--> fixes the estimated-energy
  claim). A host that passes the load-response screen turns "estimated"
  into "measured" — the exact debt paydown the provenance layer was built
  to absorb as a new generation.
- **Robotics simulation validation partners.** The task library covers
  NASA-taxonomy engineering domains (guidance, propulsion, ISRU chemistry,
  attitude determination, FDIR) as deterministic, bit-reproducible
  verification targets — including honest negative verdicts. A partner who
  runs, checks, or extends these against their own simulation stacks
  produces the first verification paths not operated by this project.
- **Satellite / CubeSat-class testbed access.** The flagship mission tasks
  (link budgets, access windows, orbit propagation, ascent feasibility) are
  reference calculations today. Real testbed telemetry to validate them
  against — even from a bench article — becomes anchored evidence with its
  provenance debts named, exactly like everything else on the chain.
- **Independent verification, full stop.** The cheapest partnership is
  fifteen minutes and one submission: run the public verifier on your
  machine and submit the result. Every verification path so far is the
  same operator, the anchored concentration series says so, and its
  interpretation names the first unaffiliated epoch as the number the
  whole series exists to baseline.

## What partners get

Named, verifiable participation — nothing else, and we state that plainly.
The participant pipeline ([`docs/PARTICIPATE.md`](PARTICIPATE.md)) is built
and rehearsed end-to-end on-ledger, including the rejection path (idx 46
<!--idx:46=participant_result_anchored-->, idx 47
<!--idx:47=participant_intake_rejected-->): your identity, your key, your
bundle, validated through six mechanical rungs and anchored only after a
shown verdict. Partners appear in their own MetaWork passports (idx 40
<!--idx:40=passport_catalog_anchored--> — histories, never a leaderboard),
and the first cross-machine records would be firsts the chain itself
memorializes. **No tokens exist, none are offered, and nothing here is an
investment, a security, or a sale** — see the standing disclaimers in
[`legal/`](../legal/) and [`LICENSE.md`](../LICENSE.md). Research only; not
financial, legal, or investment advice.

## What we will not do

- **No token sales, presales, allocations, or "early access"** — no token
  exists, and the fair-launch constitution (MIP-0001: no premine, no
  reserve, no discretionary minting) rules out private arrangements even at
  the design level.
- **No paid promotion.** The repository is the only marketing this project
  does; claims that cannot survive `metacoin verify` do not ship.
- **No exclusivity that violates vendor neutrality.** MIP-0002's invariants
  make attestation vendor-agnostic by rule — no vendor privileged, ever —
  and a partnership cannot buy an exception.

## How to reach us

Open a GitHub issue on this repository. The project speaks as
MetaCoin-Lab; there is no personal email and no named contact, by design.
A good first issue says what hardware or validation context you can bring
and what the tour showed you.

---

The honest state of the distance between this project and a shippable
product is mechanical and public — as of the MIP-0007 era every named
release-gate gap is closed (the full gate reports READY; approval stays
human and no release is implied), and what remains open is exactly what
partnership closes:

```verify-run
$ python3 protocol/release_readiness.py --check --fast
VERDICT: NOT-READY — no gaps found, but fast mode skipped expensive checks — fast mode can never establish READY  (trimmed)
```
<!--expect:no gaps found, but fast mode skipped expensive checks-->
<!--expect:mirror attested by a non-coordinator device-->

(Both external-reality gaps closed the same-operator way — the
cross-machine participation at idx 69–70, the signed second-device
mirror attestation at idx 72 — and the records say what that does NOT
establish: independence. The unaffiliated-participant milestone and
third-party archival remain open, and closing them is precisely the
collaboration this document invites.)
