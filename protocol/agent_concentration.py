"""agent_concentration.py — MetaCoin AGENT CONCENTRATION INDEX v0 (schema "aci-report/0.1").

================== HONESTY / SCOPE (READ ME) ==================
Research-stage, ZERO-VALUE, no token, no money, no mainnet, no networking, no payments.

The Agent Concentration Index (ACI) measures how DEPENDENT the protocol's verification
paths are on shared infrastructure: shared operators, shared hardware, shared execution
environments, shared source lineage, shared model/tool classes. A verification path is
one (molecule, verifier actor) pair drawn from the Work Molecules' verification_events —
every distinct attestation/verification event is a path, carrying its dependency
signature. For every unordered pair of paths (i, j), K=5 evidence-based dimensions are
scored sigma_k(i,j) in {0.0, 0.5, 1.0} (coarse and honest — no invented precision), and

    S(i,j) = sum_k w_k * sigma_k(i,j)          (uniform w_k = 0.2 in v0)
    ACI    = (2 / n(n-1)) * sum_{i<j} S(i,j)   (pairwise mean dependence)
    EIS    = 1 - ACI                           (Effective Independence Score)

UNKNOWN-METADATA RULE (the research spec's rule, applied everywhere in this module):
missing metadata LOWERS confidence, never counts as independence. An unknown sigma_k
contributes 1.0 (worst case) to the conservative ACI and is flagged in the
missing-metadata list, so the report always shows BOTH the conservative score AND how
much of the evidence base was actually observed.

WHAT THIS MEASUREMENT IS (and is not):
  * Measured on the protocol's OWN molecule catalog, where every path today is operated
    by the SAME operator on a single repository — the maximal-concentration baseline,
    measured and published deliberately as the protocol's honest starting point.
  * DESCRIPTIVE evidence for readers and future calibration. NOT a minting trigger,
    NOT a reward signal, and a LOW score is NOT proof of independence.
  * The PAIRWISE report is ACI_2 only; higher-order concentration (dependencies
    shared by 3+ paths but invisible at the pair level) is measured separately by
    the k-order machinery in this same module (see the HIGHER-ORDER section below;
    anchored as its own aci-korder baseline record).
  * The model dimension is DEGENERATE on current data: every path is mechanical-no-LLM
    with models=[] (an asserted-empty match). A shared "no-model" toolchain is still a
    shared dependency class, so it scores 1.0 — but the dimension only becomes
    informative when model-using actors exist.
  * Uniform weights are v0 placeholders, versioned "aci-weights/0.1-uniform";
    category-specific weights are future calibration work.

================== HIGHER-ORDER ACI_k (THE MATHEMATICAL HONESTY CONTRACT) ==================
Pairwise ACI (ACI_2) PROVABLY misses group dependency: k paths can share one
hidden common ancestor while every pair looks partially independent — the
pairwise marginals of a hidden-ancestor triple and of a genuinely diverse
triple can be IDENTICAL while their triple-level structure differs (the
self-test's pairwise_blindspot_exposed fixture demonstrates this mechanically,
with hand-computed arithmetic). This module therefore implements ONE concrete,
evidence-based S_k construction — the ANCESTRY-WITNESS construction — and
labels it exactly that: one candidate construction, not THE definition. The
calibration question stays open, and the anchored record says so.

The construction (schema "aci-korder-report/0.1"): for a subset B of k
verification paths and each dependency dimension d,

    shared_d(B) = 1.0  if ALL k paths declare the SAME value in d
                  0.5  if all k share membership in one declared GROUP
                       without identical values (e.g. the same operator's
                       fleet with differing fingerprints)
                  0.0  otherwise
    UNKNOWN in any path -> that dimension scores worst-case 1.0 for B and is
                  flagged (the standing rule: missing metadata never counts
                  as independence)

    S_k(B)   = sum_d w_d * shared_d(B)      (uniform weights,
                                             "aci-korder-weights/0.1-uniform")
    ACI_k(A) = mean of S_k(B) over ALL k-subsets B of the path set A

S_2 CONSISTENCY: the per-dimension k-order scorers restrict at k=2 to exactly
the pairwise sigma semantics above, so ACI_2 computed via the S_k machinery
equals the anchored pairwise ACI on the same paths TO THE DIGIT (asserted in
the self-test against the anchored ledger:18 baseline).

EXACT MODE + SAMPLED MODE (the honesty contract, upgraded): ACI_k is computed
by enumerating every k-subset whenever C(n,k) <= ENUM_LIMIT (200,000) — exact
mode remains the DEFAULT and its report (schema "aci-korder-report/0.1") is
byte-identical to v0 forever (anchored baselines keep re-deriving to the
digit). Above the limit, v0 REFUSED because "a sampled estimate without
variance analysis would be fabricated precision". v0.2 DELIVERS the variance
analysis instead of the refusal (schema "aci-korder-report/0.2", additive):

  * simple random sampling of k-subsets WITHOUT replacement;
  * DETERMINISTIC PRNG: seed = sha256(canonical path-set | k | sample_size)
    — reproducible by anyone from the report alone, no hidden randomness.
    Determinism here is LEGITIMATE (unlike security nonces, where
    unpredictability is the security property): the seed COMMITS to the
    population and the declared sample size, so the sample cannot be
    re-drawn until a flattering one appears — changing any input changes
    the seed, and every re-run draws the identical sample;
  * estimator: the sample mean of S_k(B); finite-population-corrected
    standard error SE = sqrt((1 - m/M) * s^2 / m) with m = sample size,
    M = C(n,k), s^2 = the unbiased sample variance;
  * every sampled figure ALWAYS travels with its interval: a sampled row
    reports {estimate, sample_size, population_size, standard_error,
    ci95 = estimate +/- 1.96*SE, fpc_applied: true} and has NO field for a
    bare point number (the exact-mode field aci_k does not exist on sampled
    rows, BY CONSTRUCTION — asserted in code and in the self-test).

The DELIVERABLE is the profile {ACI_2, ..., ACI_k_max} plus the per-dimension
breakdown — never a single collapsed number (the no-combined-scalar idiom);
sampled rows are typed "sampled" so exact and estimated figures can never be
confused.

The multi-scale concentration profile Gamma(r) is computed for partitions
r in {operator, machine_fingerprint, repo}: Gamma = the maximum share of paths in one
block (evidential weight q_i = 1 per path in v0). Paths whose partition key is unknown
are counted toward the LARGEST known block (worst case, per the unknown rule).

DETERMINISM: no timestamps anywhere; paths are sorted; canonical serialization; the
report_hash is the SHA-256 of the canonical report with the report_hash field excluded
(same anti-circularity pattern as the WMID and the ledger entry hash). Two runs over
the same inputs are byte-identical.

READ-ONLY: this module NEVER writes to the ledger. It reads the ledger, REBUILDS the
Work Molecules via protocol/work_molecule.py (self-contained — no dependence on a
pre-built wm_catalog.json), and derives the report.

Standard library only (json, hashlib, argparse, os, sys; plus tempfile/shutil in the
self-test). Molecule construction is REUSED from protocol/work_molecule.py — nothing is
reimplemented. The canonical-JSON helper is deliberately per-module (house style).
Not legal, financial, investment, or security-certification advice.

Usage:
    python3 protocol/agent_concentration.py --report
    python3 protocol/agent_concentration.py --report --out aci_report.json
    python3 protocol/agent_concentration.py --korder --kmax 4 --as-of 17 --out aci_korder_report.json
    python3 protocol/agent_concentration.py --korder --kmax 6   # k above ENUM_LIMIT: sampled + 95% CI
    python3 protocol/agent_concentration.py --selftest   # fixtures + temp files only
"""

# Suppress __pycache__/*.pyc so importing protocol modules below leaves no stray files.
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import itertools
import json
import math
import os

# Make `from protocol...` resolve when run directly (repo root on path).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# REUSE the Work Molecule assembler — paths are derived from rebuilt molecules.
import protocol.work_molecule as work_molecule

SCHEMA_VERSION = "aci-report/0.1"
WEIGHTS_VERSION = "aci-weights/0.1-uniform"
KORDER_SCHEMA_VERSION = "aci-korder-report/0.1"
# Additive sampled-mode schema: emitted ONLY when at least one k was sampled;
# an exact-only report stays byte-identical to 0.1 (anchored baselines keep
# re-deriving to the digit — regression-asserted in the self-test).
KORDER_SAMPLED_SCHEMA_VERSION = "aci-korder-report/0.2"
KORDER_WEIGHTS_VERSION = "aci-korder-weights/0.1-uniform"
# Exact enumeration bound: above this many k-subsets exact mode is infeasible.
# v0 refused here ("a sampled estimate without variance analysis would be
# fabricated precision"); v0.2 delivers the variance analysis — sampled mode
# with the finite-population-corrected standard error — instead of a refusal.
ENUM_LIMIT = 200_000
# Declared default sample size for sampled mode (part of the seed commitment).
DEFAULT_SAMPLE_SIZE = 20_000

BLINDSPOT_STATEMENT = (
    "This is ONE candidate S_k construction (ancestry-witness: per-dimension "
    "all-shared / group / else scoring with worst-case unknowns), not THE "
    "definition of higher-order concentration — the calibration question stays "
    "open. Higher-order scores depend entirely on declared metadata quality: a "
    "low ACI_k under missing declarations is meaningless, because the standing "
    "rule scores every unknown dimension worst-case 1.0 and flags it — missing "
    "metadata never counts as independence. Pairwise ACI_2 provably misses "
    "group dependency (k paths can share one hidden common ancestor while "
    "every pair looks partially independent); the k-order profile makes that "
    "structure measurable, and the deliverable is the full profile plus the "
    "per-dimension breakdown, never a single collapsed number."
)

# K=5 evidence-based dimensions, uniform weights for v0 (category-specific weights are
# future calibration — the report says so explicitly).
DIMENSIONS = ("operator", "hardware", "environment", "source_repo", "model")
WEIGHTS = {dim: 0.2 for dim in DIMENSIONS}

# Declared operator relationships -> dependence score. Conservative pairwise rule: the
# MOST dependent of the two declarations wins; an unrecognized declaration scores 1.0.
_OPERATOR_SCORES = {
    "same-operator": 1.0,
    "same-organization": 0.5,
    "independent-operator": 0.0,
}

# Constants derived from the current repository for real paths (declared derivations,
# not measurements — commented where used):
#   * every recorded path executes THIS repository's stdlib-only code -> one shared
#     toolchain class and one shared source lineage;
#   * every recorded path is mechanical-no-LLM with models=[] -> one shared "no-model"
#     class (the DEGENERATE dimension on current data).
_REPO_LINEAGE = "metacoin-protocol-repo"
_TOOLCHAIN = "python-stdlib-only"
_MODEL_CLASS = "no-model-mechanical"

# Which path field feeds each dimension (used for missing-metadata flagging).
_DIM_PRIMARY_FIELD = {
    "operator": "operator_relationship",
    "hardware": "machine_fingerprint",
    "environment": "platform",
    "source_repo": "repo_lineage",
    "model": "model_class",
}

INTERPRETATION = (
    "Measured on the protocol's own molecule catalog: every verification path today is "
    "operated by the SAME operator on a single repository — the maximal-concentration "
    "baseline, measured and published deliberately as the protocol's own honest starting "
    "point. ACI is DESCRIPTIVE evidence for readers and future calibration, NOT a minting "
    "trigger, NOT a reward signal, and a low value is NOT proof of independence. This v0 "
    "is pairwise only (ACI_2) and cannot see higher-order concentration structure. "
    "Missing metadata is scored as worst-case dependence (1.0) and flagged, never as "
    "independence. Zero-value research-stage; not consensus, not mainnet, not payment, "
    "not a token."
)

LIMITATION_NOTES = [
    "model dimension is DEGENERATE on current data: all paths are mechanical-no-LLM "
    "with models=[] — a shared no-model toolchain is still a shared dependency class "
    "(scored 1.0), but the dimension only becomes informative when model-using actors "
    "exist",
    "weights are uniform v0 placeholders (aci-weights/0.1-uniform); category-specific "
    "weights are future calibration work",
    "pairwise metric (ACI_2) only — higher-order concentration (dependencies shared by "
    "3+ paths but invisible at the pair level) is not captured",
    "unknown metadata contributes worst-case dependence (1.0) to the conservative ACI "
    "and is flagged; coverage is reported so readers can see how much was observed",
]


# ----------------------------------------------------------------------------
# Canonical JSON + report hash (per-module helper, house discipline)
# ----------------------------------------------------------------------------
def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, ASCII — byte-stable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_report_hash(report: dict) -> str:
    """SHA-256 hex of the canonical report WITHOUT its report_hash field (the same
    anti-circularity pattern as the WMID and the ledger entry hash)."""
    content = {k: v for k, v in report.items() if k != "report_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Pairwise dimension scoring — sigma_k(i,j) in {0.0, 0.5, 1.0}
# Every scorer applies the spec's rule: missing metadata LOWERS confidence, never
# counts as independence — an unknown input yields 1.0 (worst case).
# ----------------------------------------------------------------------------
def _score_operator(a: dict, b: dict) -> float:
    """1.0 if both declared same-operator (all current paths are); otherwise per the
    declared relationships, taking the MOST dependent of the two. Unknown -> 1.0."""
    ra, rb = a.get("operator_relationship"), b.get("operator_relationship")
    if ra is None or rb is None:
        return 1.0
    return max(_OPERATOR_SCORES.get(ra, 1.0), _OPERATOR_SCORES.get(rb, 1.0))


def _score_hardware(a: dict, b: dict) -> float:
    """1.0 shared fingerprint; 0.5 differing fingerprints with no independence evidence
    beyond that (the same operator's fleet); 0.0 ONLY with declared independent
    ownership on both sides (none exist today). Unknown fingerprint -> 1.0."""
    if (a.get("hardware_ownership") == "declared-independent"
            and b.get("hardware_ownership") == "declared-independent"):
        return 0.0
    fa, fb = a.get("machine_fingerprint"), b.get("machine_fingerprint")
    if fa is None or fb is None:
        return 1.0
    return 1.0 if fa == fb else 0.5


def _score_environment(a: dict, b: dict) -> float:
    """1.0 same platform string; 0.5 different platform on the same (or unknown —
    conservatively treated as shared) toolchain; 0.0 declared independent toolchains.
    Unknown platform -> 1.0."""
    pa, pb = a.get("platform"), b.get("platform")
    if pa is None or pb is None:
        return 1.0
    if pa == pb:
        return 1.0
    ta, tb = a.get("toolchain"), b.get("toolchain")
    if ta is not None and tb is not None and ta != tb:
        return 0.0
    return 0.5


def _score_source_repo(a: dict, b: dict) -> float:
    """1.0 same repository lineage (all current paths — one repository); 0.0 for
    independent implementations. Unknown -> 1.0."""
    ra, rb = a.get("repo_lineage"), b.get("repo_lineage")
    if ra is None or rb is None:
        return 1.0
    return 1.0 if ra == rb else 0.0


def _score_model(a: dict, b: dict) -> float:
    """1.0 same model class — including the shared 'no-model' class all current paths
    share (DEGENERATE on current data; see module docstring); 0.0 for independent
    model classes. Unknown -> 1.0."""
    ma, mb = a.get("model_class"), b.get("model_class")
    if ma is None or mb is None:
        return 1.0
    return 1.0 if ma == mb else 0.0


_SCORERS = {
    "operator": _score_operator,
    "hardware": _score_hardware,
    "environment": _score_environment,
    "source_repo": _score_source_repo,
    "model": _score_model,
}


def score_pair(a: dict, b: dict) -> dict:
    """Return {dimension: sigma} for one unordered path pair."""
    return {dim: _SCORERS[dim](a, b) for dim in DIMENSIONS}


# ----------------------------------------------------------------------------
# Higher-order (k-subset) dimension scoring — the ancestry-witness S_k.
# Each scorer returns (sigma, tag); tag in {"identical", "group",
# "independent", "unknown", "mixed-worst-case"} feeds the per-dimension
# breakdown. RESTRICTION CONTRACT: at k=2 every scorer reproduces the pairwise
# sigma above EXACTLY (asserted in the self-test), so ACI_2 via this machinery
# equals the pairwise ACI to the digit. The unknown rule is unchanged: an
# unknown anywhere in the subset scores worst-case 1.0 and is flagged.
# ----------------------------------------------------------------------------
def _k_operator(subset):
    """max of the declared-relationship scores over the subset (the most
    dependent declaration wins — the pairwise rule, k-generalized)."""
    vals = [p.get("operator_relationship") for p in subset]
    if any(v is None for v in vals):
        return (1.0, "unknown")
    sigma = max(_OPERATOR_SCORES.get(v, 1.0) for v in vals)
    if sigma == 1.0:
        return (1.0, "identical" if len(set(vals)) == 1 else "mixed-worst-case")
    return (sigma, "group" if sigma == 0.5 else "independent")


def _k_hardware(subset):
    """0.0 only if ALL declare independent ownership; unknown fingerprint ->
    1.0; ALL fingerprints identical -> 1.0; else 0.5 (one declared fleet
    GROUP without identical values)."""
    if all(p.get("hardware_ownership") == "declared-independent"
           for p in subset):
        return (0.0, "independent")
    fps = [p.get("machine_fingerprint") for p in subset]
    if any(f is None for f in fps):
        return (1.0, "unknown")
    if len(set(fps)) == 1:
        return (1.0, "identical")
    return (0.5, "group")


def _k_environment(subset):
    """unknown platform -> 1.0; ALL platforms identical -> 1.0; differing
    platforms with two KNOWN different toolchains -> 0.0; else 0.5 (one
    toolchain group, unknowns conservatively treated as shared)."""
    plats = [p.get("platform") for p in subset]
    if any(v is None for v in plats):
        return (1.0, "unknown")
    if len(set(plats)) == 1:
        return (1.0, "identical")
    known = {p.get("toolchain") for p in subset if p.get("toolchain") is not None}
    if len(known) > 1:
        return (0.0, "independent")
    return (0.5, "group")


def _k_same_value(field):
    """Scorer factory for the identical-or-nothing dimensions (source_repo,
    model): unknown -> 1.0; all identical -> 1.0; else 0.0."""
    def scorer(subset):
        vals = [p.get(field) for p in subset]
        if any(v is None for v in vals):
            return (1.0, "unknown")
        if len(set(vals)) == 1:
            return (1.0, "identical")
        return (0.0, "independent")
    return scorer


_KORDER_SCORERS = {
    "operator": _k_operator,
    "hardware": _k_hardware,
    "environment": _k_environment,
    "source_repo": _k_same_value("repo_lineage"),
    "model": _k_same_value("model_class"),
}


def score_subset(subset) -> dict:
    """{dimension: {"sigma": float, "tag": str}} for one k-subset of paths."""
    out = {}
    for dim in DIMENSIONS:
        sigma, tag = _KORDER_SCORERS[dim](subset)
        out[dim] = {"sigma": sigma, "tag": tag}
    return out


def subset_score(subset) -> float:
    """S_k(B) = sum_d w_d * shared_d(B) for one subset."""
    sub = score_subset(subset)
    return sum(WEIGHTS[d] * sub[d]["sigma"] for d in DIMENSIONS)


# ----------------------------------------------------------------------------
# Sampled ACI_k: SRS without replacement + finite-population variance honesty
# ----------------------------------------------------------------------------
class _DetPRNG:
    """Deterministic sha256-counter PRNG for subset sampling.

    LEGITIMATE determinism (contrast the security-nonce rule elsewhere in this
    project, where unpredictability IS the security property): the seed
    COMMITS to the population (canonical path-set), k, and the declared sample
    size, so anyone can re-draw the identical sample from the report alone and
    the sampler cannot be re-rolled until a flattering sample appears."""

    def __init__(self, seed_hex: str):
        self._seed = seed_hex
        self._counter = 0
        self._buf = b""

    def _take(self, nbytes: int) -> bytes:
        while len(self._buf) < nbytes:
            self._buf += hashlib.sha256(
                f"{self._seed}:{self._counter}".encode("utf-8")).digest()
            self._counter += 1
        out, self._buf = self._buf[:nbytes], self._buf[nbytes:]
        return out

    def randbelow(self, bound: int) -> int:
        """Uniform integer in [0, bound) via rejection sampling (unbiased)."""
        if bound <= 0:
            raise ValueError("bound must be positive")
        span = 1 << 64
        limit = (span // bound) * bound
        while True:
            v = int.from_bytes(self._take(8), "big")
            if v < limit:
                return v % bound


def sampling_seed(paths: list, k: int, sample_size: int,
                  seed_salt=None) -> str:
    """seed = sha256(canonical path-set | k | declared sample_size). The salt
    exists ONLY for the validation fixtures (repeated draws on populations
    where exact is computable); when used it is carried in the report row."""
    material = canonical_json(paths) + f"|k={k}|m={sample_size}"
    if seed_salt is not None:
        material += f"|salt={seed_salt}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sample_subset_indices(n: int, k: int, m: int, rng: _DetPRNG) -> list:
    """m DISTINCT k-subsets of range(n), uniform, WITHOUT replacement.

    m == M returns the whole population (a full-population 'sample'); a dense
    request (m > M/2 on an enumerable population) uses a partial Fisher-Yates
    over the enumerated population (rejection would thrash near exhaustion);
    the sparse case (the real regime, m << M) rejection-samples on subset
    identity."""
    M = math.comb(n, k)
    if m >= M:
        return list(itertools.combinations(range(n), k))
    if M <= ENUM_LIMIT and m > M // 2:
        pop = list(itertools.combinations(range(n), k))
        for i in range(m):
            j = i + rng.randbelow(len(pop) - i)
            pop[i], pop[j] = pop[j], pop[i]
        return pop[:m]
    seen = set()
    chosen = []
    while len(chosen) < m:
        idxs = set()
        while len(idxs) < k:
            idxs.add(rng.randbelow(n))
        t = tuple(sorted(idxs))
        if t not in seen:
            seen.add(t)
            chosen.append(t)
    return chosen


def sampled_aci_k(paths: list, k: int, sample_size: int = DEFAULT_SAMPLE_SIZE,
                  seed_salt=None) -> dict:
    """One sampled profile row: SRS of k-subsets without replacement, sample
    mean of S_k as the estimator, and the finite-population-corrected standard
    error SE = sqrt((1 - m/M) * s^2 / m). The row ALWAYS carries its interval
    and NEVER carries the exact-mode point field aci_k — a bare sampled point
    number is impossible by construction (asserted here and in the self-test).
    """
    n = len(paths)
    M = math.comb(n, k)
    m = min(sample_size, M)
    seed = sampling_seed(paths, k, sample_size, seed_salt)
    subsets = _sample_subset_indices(n, k, m, _DetPRNG(seed))
    scores = [subset_score(tuple(paths[i] for i in t)) for t in subsets]
    mean = sum(scores) / m
    s2 = (sum((x - mean) ** 2 for x in scores) / (m - 1)) if m > 1 else 0.0
    fpc = 1.0 - m / M
    se = math.sqrt(fpc * s2 / m)
    row = {
        "k": k,
        "mode": "sampled",
        "exact": False,
        "estimate": mean,
        "sample_size": m,
        "population_size": M,
        "subset_count": M,
        "standard_error": se,
        "ci95": [mean - 1.96 * se, mean + 1.96 * se],
        "fpc_applied": True,
        "seed": seed,
    }
    if seed_salt is not None:
        row["seed_salt"] = seed_salt
    # BY CONSTRUCTION: no bare sampled point number, ever.
    assert "aci_k" not in row and "ci95" in row and "standard_error" in row
    return row


def compute_korder_report(paths: list, k_max: int = 4,
                          as_of_ledger_index: int = None,
                          sample_size: int = DEFAULT_SAMPLE_SIZE,
                          force_sample: bool = False,
                          seed_salt=None) -> dict:
    """The higher-order concentration report over a path list. Pure function
    of its inputs; deterministic, no timestamps; two runs byte-identical.

    For each k in 2..k_max: EXACT enumeration of all C(n,k) subsets when that
    count is within ENUM_LIMIT (the default, unchanged from v0 — an exact-only
    report is byte-identical to schema 0.1), else a SAMPLED row carrying its
    finite-population-corrected interval (schema 0.2, additive; see
    sampled_aci_k). `force_sample` samples even below the limit (validation
    fixtures only). The per-dimension breakdown is taken at the largest k
    actually enumerated exactly (sampled subsets never feed it). The float
    accumulation at k=2 replicates the pairwise report's summation order
    exactly, so ACI_2 here equals pairwise_aci to the digit.
    """
    n = len(paths)
    if n < 2:
        raise ValueError(f"need at least 2 verification paths (got {n})")
    if k_max < 2:
        raise ValueError(f"k_max must be at least 2 (got {k_max})")

    profile = []
    refused = []
    computed = []
    sampled = []
    top = None  # (k, subset_count, tag_counts) at the largest EXACT k
    for k in range(2, k_max + 1):
        if k > n:
            refused.append({"k": k, "subset_count": 0,
                            "reason": f"k={k} exceeds path_count {n} — no "
                                      "k-subsets exist"})
            continue
        count = math.comb(n, k)
        if count > ENUM_LIMIT or force_sample:
            row = sampled_aci_k(paths, k, sample_size=sample_size,
                                seed_salt=seed_salt)
            profile.append(row)
            sampled.append(k)
            continue
        s_total = 0.0
        tag_counts = {d: {"identical": 0, "group": 0, "unknown": 0}
                      for d in DIMENSIONS}
        for subset in itertools.combinations(paths, k):
            sub = score_subset(subset)
            s_total += sum(WEIGHTS[d] * sub[d]["sigma"] for d in DIMENSIONS)
            for d in DIMENSIONS:
                tag = sub[d]["tag"]
                if tag in tag_counts[d]:
                    tag_counts[d][tag] += 1
        profile.append({"k": k, "aci_k": s_total / count,
                        "subset_count": count, "exact": True})
        computed.append(k)
        top = (k, count, tag_counts)

    per_dimension = {}
    if top is not None:
        _k_top, count_top, tc = top
        per_dimension = {
            d: {
                "all_identical_fraction_at_kmax": tc[d]["identical"] / count_top,
                "group_fraction": tc[d]["group"] / count_top,
                "unknown_flag_count": tc[d]["unknown"],
            }
            for d in DIMENSIONS
        }

    report = {
        "schema": (KORDER_SAMPLED_SCHEMA_VERSION if sampled
                   else KORDER_SCHEMA_VERSION),
        "weights_version": KORDER_WEIGHTS_VERSION,
        "weights": dict(WEIGHTS),
        "dimensions": list(DIMENSIONS),
        "enum_limit": ENUM_LIMIT,
        "path_count": n,
        "k_max": k_max,
        "as_of_ledger_index": as_of_ledger_index,
        "k_values_computed": computed,
        "k_values_refused": refused,
        "profile": profile,
        "per_dimension": per_dimension,
        "blindspot_statement": BLINDSPOT_STATEMENT,
        "zero_value": True,
        "no_token": True,
    }
    if sampled:
        # ADDITIVE sampled-mode keys — absent from exact-only reports, which
        # therefore stay byte-identical to schema 0.1 forever.
        report["k_values_sampled"] = sampled
        report["sampling"] = {
            "method": "simple random sampling of k-subsets WITHOUT "
                      "replacement",
            "estimator": "sample mean of S_k(B)",
            "standard_error": "SE = sqrt((1 - m/M) * s^2 / m) — "
                              "finite-population corrected (m = sample size, "
                              "M = C(n,k), s^2 = unbiased sample variance)",
            "declared_sample_size": sample_size,
            "seed_rule": "sha256(canonical path-set | k | sample_size"
                         "[ | salt]) — reproducible by anyone; the seed "
                         "commits to the population, so determinism here is "
                         "legitimate (unlike security nonces)",
            "honesty_note": "every sampled figure travels with its 95% "
                            "interval; the schema has NO field for a bare "
                            "sampled point number — sampled rows carry "
                            "estimate/standard_error/ci95, never aci_k",
        }
    report["report_hash"] = compute_report_hash(report)
    return report


# ----------------------------------------------------------------------------
# Path extraction from Work Molecules
# ----------------------------------------------------------------------------
def extract_paths(molecules: list) -> list:
    """Derive the verification paths (one per verification event) from molecules.

    Every value is copied or derived from the molecule; anything the molecule does not
    record for that actor stays None (unknown -> conservative scoring + flag).
    """
    paths = []
    for m in molecules:
        task_id = m["task_spec"]["task_id"]
        # actor -> fingerprint, as recorded in the molecule's hardware evidence
        fp_by_actor = {}
        for mf in m["hardware_evidence"].get("machine_fingerprints", []):
            fp_by_actor.setdefault(mf.get("actor_id"), mf.get("fingerprint"))
        # the actor that produced the submission is the only one whose environment
        # summary the molecule records
        submission_actor = None
        for rec in m.get("execution_records", []):
            if str(rec.get("source", "")).startswith("submission:"):
                submission_actor = rec.get("actor_id")
                break
        env = m["manifests"].get("environment_summary")
        for ve in m["verification_events"]:
            actor = ve.get("verifier_id")
            platform = None
            if env is not None and submission_actor is not None and actor == submission_actor:
                platform = env.get("platform")
            paths.append({
                "task_id": task_id,
                "ledger_index": ve.get("ledger_index"),
                "actor_id": actor,
                "operator_relationship": ve.get("operator_relationship"),
                "machine_fingerprint": fp_by_actor.get(actor),
                "hardware_ownership": None,  # no declared independent ownership exists
                "platform": platform,
                # declared derivations shared by every recorded path (see constants)
                "toolchain": _TOOLCHAIN,
                "repo_lineage": _REPO_LINEAGE,
                "model_class": _MODEL_CLASS,
            })
    paths.sort(key=lambda p: (p["task_id"], p["ledger_index"], str(p["actor_id"])))
    return paths


def build_paths(ledger_path: str = work_molecule.DEFAULT_LEDGER_PATH,
                as_of_index: int = None) -> list:
    """Rebuild all Work Molecules from the ledger (read-only) and extract the paths.

    `as_of_index` is the generation-lock rebuild mode (see work_molecule): the ACI
    baseline anchored at ledger index N re-measures exactly with as_of_index = N-1,
    while an unbounded re-measurement reflects the current chain."""
    molecules = []
    for tid in sorted(work_molecule.TASK_MODULES):
        submission_path = None
        special = work_molecule._CATALOG_SUBMISSIONS.get(tid)
        if special is not None:
            # same discovery as the catalog builder (repo root, then the published
            # evidence bundle) — citations stay basename-only, so location never
            # leaks into molecule content or the ACI measurement
            submission_path = work_molecule.find_evidence_file(special)
        molecule = work_molecule.build_molecule(tid, ledger_path=ledger_path,
                                                submission_path=submission_path,
                                                as_of_index=as_of_index)
        ok, reasons = work_molecule.validate(molecule, ledger_path=ledger_path)
        if not ok:
            raise ValueError(f"molecule for {tid} does not validate: {reasons}")
        molecules.append(molecule)
    return extract_paths(molecules)


# ----------------------------------------------------------------------------
# Concentration profile Gamma(r)
# ----------------------------------------------------------------------------
_PARTITION_KEYS = {
    "operator": "operator_relationship",
    "machine_fingerprint": "machine_fingerprint",
    "repo": "repo_lineage",
}


def concentration_profile(paths: list) -> dict:
    """Gamma(r) for r in {operator, machine_fingerprint, repo}: the maximum share of
    paths in one partition block (q_i = 1 per path in v0). Paths with an unknown key
    are counted toward the LARGEST known block — worst case, per the unknown rule.

    The operator partition applies the unknown rule to UNRECOGNIZED declarations
    too: a value outside the recognized set (e.g. a '-claimed' self-declaration
    from participant intake) is declared metadata, not a verified distinct
    operator — it joins the unknown pool instead of forming its own partition
    block, so an unverifiable claim can never LOWER measured concentration.
    (The pairwise and k-order scorers already apply the same rule: score 1.0.)"""
    profile = {}
    n = len(paths)
    for name, key in sorted(_PARTITION_KEYS.items()):
        blocks = {}
        unknown = 0
        for p in paths:
            v = p.get(key)
            if v is None or (name == "operator"
                             and v not in _OPERATOR_SCORES):
                unknown += 1
            else:
                blocks[v] = blocks.get(v, 0) + 1
        if n == 0:
            profile[name] = 0.0
        elif not blocks:
            profile[name] = 1.0  # nothing observed at all -> one worst-case block
        else:
            profile[name] = (max(blocks.values()) + unknown) / n
    return profile


# ----------------------------------------------------------------------------
# Report computation (pure, deterministic, no timestamps)
# ----------------------------------------------------------------------------
def compute_report(paths: list) -> dict:
    """Compute the full ACI report over a path list. Pure function of its input."""
    n = len(paths)
    if n < 2:
        raise ValueError(f"need at least 2 verification paths to compute ACI (got {n})")

    pair_count = n * (n - 1) // 2
    s_total = 0.0
    dim_sums = {dim: 0.0 for dim in DIMENSIONS}
    dim_counts = {dim: {"1.0": 0, "0.5": 0, "0.0": 0} for dim in DIMENSIONS}
    for i in range(n):
        for j in range(i + 1, n):
            sigmas = score_pair(paths[i], paths[j])
            s_total += sum(WEIGHTS[d] * sigmas[d] for d in DIMENSIONS)
            for d in DIMENSIONS:
                dim_sums[d] += sigmas[d]
                dim_counts[d][f"{sigmas[d]:.1f}"] += 1

    aci = s_total / pair_count
    breakdown = {
        d: {
            "pairs_at_1.0": dim_counts[d]["1.0"],
            "pairs_at_0.5": dim_counts[d]["0.5"],
            "pairs_at_0.0": dim_counts[d]["0.0"],
            "mean_sigma": dim_sums[d] / pair_count,
        }
        for d in DIMENSIONS
    }

    # Missing-metadata flags: one flag per (path, dimension) whose primary evidence
    # field is unknown. Unknowns scored worst-case above; here they are made visible.
    flags = []
    for p in paths:
        for dim in DIMENSIONS:
            if p.get(_DIM_PRIMARY_FIELD[dim]) is None:
                flags.append({
                    "path": f"{p['task_id']}/{p['actor_id']}",
                    "ledger_index": p["ledger_index"],
                    "dimension": dim,
                })
    flags.sort(key=lambda f: (f["path"], str(f["ledger_index"]), f["dimension"]))
    observed = n * len(DIMENSIONS) - len(flags)

    report = {
        "schema": SCHEMA_VERSION,
        "weights_version": WEIGHTS_VERSION,
        "weights": dict(WEIGHTS),
        "path_count": n,
        "pair_count": pair_count,
        "paths": paths,
        "pairwise_aci": aci,
        "eis": 1.0 - aci,
        "dimension_breakdown": breakdown,
        "concentration_profile": concentration_profile(paths),
        "missing_metadata_flags": flags,
        "missing_metadata_count": len(flags),
        "metadata_coverage": {
            "observed_path_dimensions": observed,
            "total_path_dimensions": n * len(DIMENSIONS),
            "coverage_ratio": observed / (n * len(DIMENSIONS)),
        },
        "interpretation": INTERPRETATION,
        "limitation_notes": list(LIMITATION_NOTES),
        "zero_value": True,
        "no_token": True,
    }
    report["report_hash"] = compute_report_hash(report)
    return report


def profile_row_text(row: dict) -> str:
    """One-line human rendering of a profile row, exact vs sampled ALWAYS
    typed — a sampled figure is never printed without its interval."""
    if row.get("exact"):
        return f"ACI_{row['k']}={row['aci_k']:.6f} (exact)"
    lo, hi = row["ci95"]
    return (f"ACI_{row['k']}~={row['estimate']:.6f} "
            f"(sampled m={row['sample_size']}/M={row['population_size']}, "
            f"SE={row['standard_error']:.6f}, 95% CI [{lo:.6f}, {hi:.6f}])")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_concentration.py",
        description=(
            "MetaCoin Agent Concentration Index v0 (research-stage, ZERO-VALUE, no "
            "token). Rebuilds the Work Molecules from the ledger (read-only — NEVER "
            "writes to it) and reports the pairwise ACI, EIS, and Gamma profile."
        ),
        epilog=(
            "The measured value is the deliverable: today's catalog is a same-operator "
            "maximal-concentration baseline and the report says so. Missing metadata "
            "scores worst-case and is flagged, never counted as independence. Not "
            "consensus, not mainnet, not payment, not a token."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--report", action="store_true",
                        help="compute the ACI report over the real molecule catalog")
    parser.add_argument("--korder", action="store_true",
                        help="compute the higher-order ACI_k profile (exact "
                             "below ENUM_LIMIT; sampled WITH the "
                             "finite-population interval above it)")
    parser.add_argument("--kmax", type=int, default=4,
                        help="with --korder: largest subset size k (default 4)")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help="with --korder: declared sample size for sampled "
                             f"mode (default {DEFAULT_SAMPLE_SIZE}; part of "
                             "the deterministic seed commitment)")
    parser.add_argument("--force-sample", action="store_true",
                        help="with --korder: sample even when exact "
                             "enumeration is feasible (validation/testing "
                             "only — exact mode is the default below "
                             "ENUM_LIMIT)")
    parser.add_argument("--as-of", type=int, default=None, dest="as_of",
                        help="with --korder: generation-lock chain point (paths "
                             "drawn from ledger entries with index <= N)")
    parser.add_argument("--ledger", default=work_molecule.DEFAULT_LEDGER_PATH,
                        help=f"ledger JSONL to read (default: {work_molecule.DEFAULT_LEDGER_PATH})")
    parser.add_argument("--out",
                        help="also write the report JSON to this file "
                             "(defaults: aci_report.json / aci_korder_report"
                             ".json; gitignored)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the fixture self-test (temp files only; writes "
                             "nothing into the repo and never touches the ledger)")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not (args.report or args.korder):
        parser.error("one of --report, --korder or --selftest is required")

    if args.korder:
        try:
            paths = build_paths(ledger_path=args.ledger, as_of_index=args.as_of)
            report = compute_korder_report(paths, k_max=args.kmax,
                                           as_of_ledger_index=args.as_of,
                                           sample_size=args.sample_size,
                                           force_sample=args.force_sample)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        text = json.dumps(report, indent=2, sort_keys=True)
        print(text)
        out = args.out or "aci_korder_report.json"
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        prof = ", ".join(profile_row_text(row) for row in report["profile"])
        print(f"wrote k-order report ({report['path_count']} paths, {prof}; "
              f"refused: {[r['k'] for r in report['k_values_refused']]}) "
              f"to {out}", file=sys.stderr)
        return 0

    try:
        paths = build_paths(ledger_path=args.ledger)
        report = compute_report(paths)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    out = args.out or "aci_report.json"
    with open(out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"wrote ACI report ({report['path_count']} paths, "
          f"ACI={report['pairwise_aci']:.6f}) to {out}", file=sys.stderr)
    return 0


# ============================== SELF-TEST ====================================
def _fixture_path(actor, fingerprint, platform, task="task-fixture", idx=1,
                  operator="same-operator", ownership=None, toolchain=_TOOLCHAIN,
                  repo=_REPO_LINEAGE, model=_MODEL_CLASS):
    """Compact fixture-path builder for the self-test."""
    return {
        "task_id": task, "ledger_index": idx, "actor_id": actor,
        "operator_relationship": operator, "machine_fingerprint": fingerprint,
        "hardware_ownership": ownership, "platform": platform,
        "toolchain": toolchain, "repo_lineage": repo, "model_class": model,
    }


def blindspot_fixture_paths() -> list:
    """The pairwise_blindspot_exposed fixture: 6 paths whose two triples have
    IDENTICAL pairwise structure but different triple-level structure.

    Triple {1,2,3} — HIDDEN COMMON ANCESTOR: pairwise-different operators
    (all independent-operator, score 0), fingerprints (fleet 0.5), platforms +
    toolchains (0), models (0) — but ONE shared source-repo lineage R* (1.0).
    Triple {4,5,6} — GENUINELY DIVERSE control: each pair shares exactly one
    dimension, a DIFFERENT one per pair (4,5: repo RA; 4,6: model MX;
    5,6: platform PL), so NO dimension is shared by all three.

    HAND-COMPUTED (uniform w_d = 0.2; exact grid arithmetic, no tuning):
      every within-triple pair, BOTH triples:
          S = 0.2*(op 0 + hw 0.5 + env 0 + repo-or-equivalent 1.0 + 0)
            = 0.3                       <- pairwise CANNOT tell the triples apart
      every cross-triple pair: S = 0.2*(hw 0.5) = 0.1
      S_3({1,2,3}) = 0.2*(hw 0.5 + repo 1.0) = 0.3   <- repo shared_3 = 1.0
      S_3({4,5,6}) = 0.2*(hw 0.5)            = 0.1   <- no dimension survives k=3
      separation at k=3: 0.2 exactly — invisible at k=2 (identical marginals)
      ACI_2 = (3*0.3 + 3*0.3 + 9*0.1) / 15 = 2.7/15 = 0.18
      ACI_3 = (1*0.3 + 19*0.1) / 20       = 2.2/20 = 0.11
    """
    def bs(actor, fp, platform, toolchain, repo, model):
        return _fixture_path(actor, fp, platform,
                             operator="independent-operator",
                             toolchain=toolchain, repo=repo, model=model)
    return [
        # the hidden-ancestor triple: everything pairwise-different EXCEPT repo
        bs("bs1", "F1", "P1", "T1", "R*", "M1"),
        bs("bs2", "F2", "P2", "T2", "R*", "M2"),
        bs("bs3", "F3", "P3", "T3", "R*", "M3"),
        # the genuinely diverse control triple: one shared dimension per pair,
        # a different dimension each time — nothing shared by all three
        bs("bs4", "F4", "P4", "T4", "RA", "MX"),
        bs("bs5", "F5", "PL", "T5", "RA", "MY"),
        bs("bs6", "F6", "PL", "T6", "RB", "MX"),
    ]


def _selftest() -> int:
    """Fixture-based self-test: hand-computed expectations, worst-case unknown scoring,
    determinism, and Gamma correctness. Temp/pure only; the ledger is never written and
    the repo gains no files (existence-delta check)."""
    print("=== protocol/agent_concentration.py self-test (fixtures; read-only) ===")
    print("Hand-computed ACI expectations, unknown-metadata worst-case scoring, and")
    print("determinism. The real ledger is read at most (never written).\n")

    root_before = set(os.listdir(_REPO_ROOT))
    proto_before = set(os.listdir(os.path.dirname(os.path.abspath(__file__))))
    checks = []
    tol = 1e-12

    # (a) all-identical paths -> every sigma = 1.0 -> ACI exactly 1.0, EIS 0.0, no flags
    identical = [_fixture_path(f"actor-{i}", "sha256:" + "a" * 64, "fixture-platform")
                 for i in range(3)]
    rep_a = compute_report(identical)
    checks.append(("all-identical paths give ACI == 1.0 exactly",
                   rep_a["pairwise_aci"] == 1.0 and rep_a["eis"] == 0.0))
    checks.append(("all-identical paths give zero missing-metadata flags",
                   rep_a["missing_metadata_count"] == 0))

    # (b) declared-independence fixture, expected value HAND-COMPUTED:
    #   P: independent-operator, fp F1 + declared-independent hw, platform L1/toolchain
    #      T1, repo R1, model M1
    #   Q: independent-operator, fp F2 + declared-independent hw, platform L2/toolchain
    #      T2, repo R2, model M2
    #   R: same-operator, fp F3 (no ownership declaration), platform L1/toolchain T1,
    #      repo R1, model M1
    #   S(P,Q): op max(0,0)=0; hw both declared-independent=0; env L1!=L2 with declared
    #           different toolchains=0; repo R1!=R2=0; model M1!=M2=0     -> S=0.0
    #   S(P,R): op max(0,1)=1; hw F1!=F3 not both declared=0.5; env L1==L1=1;
    #           repo R1==R1=1; model M1==M1=1                             -> S=0.9
    #   S(Q,R): op max(0,1)=1; hw F2!=F3=0.5; env L2!=L1, toolchains T2!=T1=0;
    #           repo R2!=R1=0; model M2!=M1=0                             -> S=0.3
    #   ACI = (0.0 + 0.9 + 0.3) / 3 = 0.4
    P = _fixture_path("P", "F1", "L1", operator="independent-operator",
                      ownership="declared-independent", toolchain="T1",
                      repo="R1", model="M1")
    Q = _fixture_path("Q", "F2", "L2", operator="independent-operator",
                      ownership="declared-independent", toolchain="T2",
                      repo="R2", model="M2")
    R = _fixture_path("R", "F3", "L1", operator="same-operator",
                      toolchain="T1", repo="R1", model="M1")
    rep_b = compute_report([P, Q, R])
    checks.append(("declared-independence fixture gives hand-computed ACI 0.4",
                   abs(rep_b["pairwise_aci"] - 0.4) < tol))
    checks.append(("declared-independence fixture gives EIS 0.6",
                   abs(rep_b["eis"] - 0.6) < tol))

    # (c) unknown metadata scores WORST CASE (never independence) and is flagged:
    # U has unknown fingerprint + unknown platform; paired with two identical known
    # paths, every one of its pair dimensions must score 1.0 -> ACI stays exactly 1.0.
    known = [_fixture_path(f"K{i}", "sha256:" + "b" * 64, "fixture-platform")
             for i in range(2)]
    U = _fixture_path("U", None, None)
    rep_c = compute_report(known + [U])
    flagged = {(f["path"], f["dimension"]) for f in rep_c["missing_metadata_flags"]}
    checks.append(("unknown fingerprint/platform score worst-case (ACI stays 1.0)",
                   rep_c["pairwise_aci"] == 1.0))
    checks.append(("unknown metadata is flagged per path+dimension",
                   ("task-fixture/U", "hardware") in flagged
                   and ("task-fixture/U", "environment") in flagged
                   and rep_c["missing_metadata_count"] == 2))

    # (d) determinism: two computations over the same paths are byte-identical, and
    # the report_hash recomputes (anti-circularity pattern)
    rep_d1 = compute_report([P, Q, R])
    rep_d2 = compute_report([P, Q, R])
    checks.append(("two runs byte-identical (canonical JSON)",
                   canonical_json(rep_d1) == canonical_json(rep_d2)))
    checks.append(("report_hash recomputes from content",
                   compute_report_hash(rep_d1) == rep_d1["report_hash"]))

    # (e) Gamma profile on a fixture: fingerprints [A, A, B, unknown] -> largest known
    # block (2) absorbs the unknown -> Gamma(machine) = 3/4; all same-operator on one
    # repo -> Gamma(operator) = Gamma(repo) = 1.0.
    g_paths = [
        _fixture_path("g1", "A", "L"), _fixture_path("g2", "A", "L"),
        _fixture_path("g3", "B", "L"), _fixture_path("g4", None, "L"),
    ]
    prof = concentration_profile(g_paths)
    checks.append(("Gamma(machine_fingerprint) = 3/4 on the fixture",
                   abs(prof["machine_fingerprint"] - 0.75) < tol))
    checks.append(("Gamma(operator) = Gamma(repo) = 1.0 on the fixture",
                   prof["operator"] == 1.0 and prof["repo"] == 1.0))

    # (e2) '-claimed' HONESTY (participant intake): an UNRECOGNIZED operator
    # declaration (a self-claimed relationship) joins the unknown pool instead
    # of forming its own partition block — an unverifiable claim can never
    # LOWER Gamma(operator). Same rule pairwise: the '-claimed' path scores
    # worst-case 1.0 against every other path.
    c_path = dict(_fixture_path("g5", "A", "L"))
    c_path["operator_relationship"] = "unaffiliated-claimed"
    prof_c = concentration_profile(g_paths + [c_path])
    checks.append(("'-claimed' declaration cannot lower Gamma(operator) "
                   "(unknown rule applied to unrecognized values)",
                   prof_c["operator"] == 1.0
                   and score_pair(c_path, g_paths[0])["operator"] == 1.0))

    # ---------------- higher-order ACI_k fixtures (aci-korder/0.1) ----------------
    # (k1) S_2 RESTRICTION CONTRACT: on every pair drawn from a mixed pool the
    # k-order scorers reproduce the pairwise sigmas EXACTLY (same floats)
    pool = identical + [P, Q, R, U] + blindspot_fixture_paths()
    restriction_ok = True
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            pairwise = score_pair(pool[i], pool[j])
            korder2 = {d: v["sigma"]
                       for d, v in score_subset((pool[i], pool[j])).items()}
            if pairwise != korder2:
                restriction_ok = False
    checks.append(("S_k restricts to the pairwise sigmas exactly at k=2",
                   restriction_ok))

    # (k2) pairwise_blindspot_exposed — THE HEADLINE FIXTURE (hand-computed in
    # blindspot_fixture_paths' docstring): two triples with IDENTICAL pairwise
    # marginals (every within-triple pair S = 0.3) that k=3 separates — the
    # hidden-ancestor triple keeps S_3 = 0.3 (repo shared_3 = 1.0) while the
    # genuinely diverse control drops to S_3 = 0.1. ACI_2 = 0.18, ACI_3 = 0.11.
    bs = blindspot_fixture_paths()
    hidden, control = bs[:3], bs[3:]
    within_pairs_ok = all(
        abs(subset_score(pair) - 0.3) < 1e-9
        for triple in (hidden, control)
        for pair in itertools.combinations(triple, 2))
    s3_hidden = subset_score(hidden)
    s3_control = subset_score(control)
    repo_shared3 = score_subset(hidden)["source_repo"]
    korder_bs = compute_korder_report(bs, k_max=3)
    aci2_bs = korder_bs["profile"][0]["aci_k"]
    aci3_bs = korder_bs["profile"][1]["aci_k"]
    checks.append(("pairwise_blindspot_exposed: identical pairwise marginals "
                   "(every within-triple pair S=0.3)", within_pairs_ok))
    checks.append(("pairwise_blindspot_exposed: k=3 separates the hidden "
                   "ancestor (0.3) from the diverse control (0.1)",
                   abs(s3_hidden - 0.3) < 1e-9
                   and abs(s3_control - 0.1) < 1e-9
                   and repo_shared3["sigma"] == 1.0
                   and repo_shared3["tag"] == "identical"
                   and abs((s3_hidden - s3_control) - 0.2) < 1e-9))
    checks.append(("pairwise_blindspot_exposed: ACI_2 = 0.18 and ACI_3 = 0.11 "
                   "(hand-computed)",
                   abs(aci2_bs - 0.18) < 1e-9 and abs(aci3_bs - 0.11) < 1e-9))

    # (k3) S_2 consistency at the report level: ACI_2 via the S_k machinery
    # EQUALS the pairwise report's ACI (exact float equality, both fixtures)
    checks.append(("ACI_2 via S_k machinery == pairwise ACI to the digit "
                   "(fixture reports)",
                   compute_korder_report([P, Q, R], k_max=2)["profile"][0]
                   ["aci_k"] == compute_report([P, Q, R])["pairwise_aci"]
                   and aci2_bs == compute_report(bs)["pairwise_aci"]))

    # (k4) UNKNOWN worst-case at k=3: one unknown fingerprint in a triple
    # forces hardware to 1.0 (never independence) and is counted in the
    # per-dimension unknown flags at k_max
    u3 = [_fixture_path("u1", "sha256:" + "c" * 64, "PU"),
          _fixture_path("u2", "sha256:" + "d" * 64, "PU"),
          _fixture_path("u3", None, "PU")]
    hw_u3 = score_subset(u3)["hardware"]
    korder_u3 = compute_korder_report(u3, k_max=3)
    checks.append(("unknown fingerprint at k=3 scores worst-case 1.0 and is "
                   "flagged at k_max",
                   hw_u3["sigma"] == 1.0 and hw_u3["tag"] == "unknown"
                   and korder_u3["per_dimension"]["hardware"]
                   ["unknown_flag_count"] == 1))

    # (k5) ABOVE THE ENUMERATION LIMIT: C(1000,2) = 499,500 > 200,000 — v0
    # refused here; v0.2 delivers the variance analysis instead: a SAMPLED
    # row that ALWAYS carries its interval, never a bare point (the schema
    # has no aci_k field on sampled rows, by construction), and the report
    # flips to the additive 0.2 schema
    many = [_fixture_path(f"m{i}", "sha256:" + "e" * 64, "PM")
            for i in range(1000)]
    korder_many = compute_korder_report(many, k_max=2)
    row_many = korder_many["profile"][0]
    checks.append(("above ENUM_LIMIT: sampled row with interval replaces the "
                   "v0 refusal (schema 0.2, typed 'sampled')",
                   korder_many["schema"] == KORDER_SAMPLED_SCHEMA_VERSION
                   and korder_many["k_values_sampled"] == [2]
                   and korder_many["k_values_refused"] == []
                   and row_many["mode"] == "sampled"
                   and row_many["fpc_applied"] is True
                   and "aci_k" not in row_many
                   and "estimate" in row_many
                   and "standard_error" in row_many
                   and "ci95" in row_many
                   and "95% CI" in profile_row_text(row_many)))
    # ...and on 1000 IDENTICAL paths every subset scores exactly 1.0, so the
    # estimate is 1.0 with zero variance — an honest degenerate case
    checks.append(("sampled estimate on identical paths: 1.0 with SE 0",
                   row_many["estimate"] == 1.0
                   and row_many["standard_error"] == 0.0))

    # (k6) determinism + hash integrity of the k-order report
    checks.append(("k-order report deterministic and hash-consistent",
                   canonical_json(compute_korder_report(bs, k_max=3))
                   == canonical_json(korder_bs)
                   and compute_report_hash(korder_bs)
                   == korder_bs["report_hash"]))

    # ---------------- SAMPLED-MODE VALIDATION (the scientific proof) --------
    # (v1) THE TINY HAND CASE — n=6 (blindspot fixture), k=3, M=C(6,3)=20,
    # m=10, --force-sample semantics. The population's S_3 values are known
    # exactly (blindspot docstring): subset {0,1,2} scores 0.3; every other
    # of the 19 subsets scores 0.1. The deterministic seed is
    # sha256(canonical(paths)|k=3|m=10); the dense sampler (m > M/2) draws,
    # in order: (0,1,5) (0,2,5) (1,3,4) (2,4,5) (1,2,3) (3,4,5) (0,2,3)
    # (2,3,4) (0,3,4) (1,2,5) — NOTE the lone 0.3-subset {0,1,2} is NOT
    # drawn (under SRSWOR that happens with probability C(19,10)/C(20,10)
    # = 1/2; this draw is the unlucky half). HAND ARITHMETIC:
    #   sample scores = ten times 0.1
    #   mean = 10 * 0.1 / 10                = 0.1
    #   s^2  = sum (x - 0.1)^2 / (10 - 1)   = 0
    #   SE   = sqrt((1 - 10/20) * 0 / 10)   = 0
    #   ci95 = [0.1, 0.1]  (degenerate: zero OBSERVED variance)
    # The exact ACI_3 = 0.11 falls OUTSIDE this degenerate interval — an
    # honest demonstration of why a single small-m draw proves nothing and
    # the coverage guarantee is DISTRIBUTIONAL: it is asserted across 50
    # seeded repetitions in (v3), never from one sample.
    tiny = sampled_aci_k(bs, 3, 10)
    checks.append(("tiny hand case (n=6,k=3,M=20,m=10): mean 0.1, s2=0, "
                   "SE=0 — exactly as hand-computed",
                   abs(tiny["estimate"] - 0.1) < 1e-12
                   and tiny["standard_error"] == 0.0
                   and tiny["sample_size"] == 10
                   and tiny["population_size"] == 20
                   and tiny["fpc_applied"] is True
                   and tiny["ci95"] == [tiny["estimate"], tiny["estimate"]]))

    # (v2) fpc -> 0 as m -> M: a FULL-POPULATION 'sample' (m = M = 20) has
    # finite-population correction (1 - m/M) = 0, so SE = 0 exactly, and the
    # estimate equals the exact enumeration (same subsets, float-ulp equal)
    full_pop = sampled_aci_k(bs, 3, 20)
    checks.append(("full-population sample: fpc gives SE = 0 and the "
                   "estimate equals exact ACI_3",
                   full_pop["standard_error"] == 0.0
                   and full_pop["sample_size"] == 20
                   and abs(full_pop["estimate"] - aci3_bs) < 1e-12))

    # (v3)+(v4) COVERAGE and 1/sqrt(m) CONVERGENCE on a heterogeneous
    # population where exact is computable: n=50 mixed paths, k=3,
    # M = C(50,3) = 19,600 subsets enumerated exactly; then 50 seeded
    # repetitions (seed_salt 0..49) at each m in {50, 200, 800}.
    #   (v3) the exact value must fall inside the 95% CI in the expected
    #        proportion: binomially, 50 reps at p=0.95 give >= 44 successes
    #        with probability > 0.996 — the stated tolerance is >= 44/50
    #        (observed on these deterministic seeds: 49/50 at m=200).
    #   (v4) SE must shrink ~ 1/sqrt(m): the mean SE ratio across the 50
    #        seeds for m=50 vs 200 and 200 vs 800 is sqrt(4 * fpc-ratio)
    #        ~= 2.01 and 2.03; asserted within [1.8, 2.2] (tolerance for
    #        sample-variance noise in s; deterministic seeds make the
    #        observed values 2.010 and 1.995, stable forever).
    def _het(i):
        ops = ("same-operator", "same-organization", "independent-operator")
        return _fixture_path(f"h{i}", f"F{i % 7}", f"P{i % 5}",
                             operator=ops[i % 3], toolchain=f"T{i % 3}",
                             repo=f"R{i % 4}", model=f"M{i % 2}")
    het_pop = [_het(i) for i in range(50)]
    het_M = math.comb(50, 3)
    het_total = 0.0
    for subset in itertools.combinations(het_pop, 3):
        het_total += subset_score(subset)
    het_exact = het_total / het_M
    mean_se = {}
    coverage_200 = 0
    for m in (50, 200, 800):
        ses = []
        for salt in range(50):
            r = sampled_aci_k(het_pop, 3, m, seed_salt=salt)
            ses.append(r["standard_error"])
            if m == 200 and r["ci95"][0] <= het_exact <= r["ci95"][1]:
                coverage_200 += 1
        mean_se[m] = sum(ses) / len(ses)
    ratio_a = mean_se[50] / mean_se[200]
    ratio_b = mean_se[200] / mean_se[800]
    print(f"    sampled-mode validation: coverage {coverage_200}/50 at "
          f"m=200 (>=44 required); SE-shrink ratios {ratio_a:.3f} "
          f"(50->200) and {ratio_b:.3f} (200->800), expected ~2")
    checks.append(("CI coverage across 50 seeded repetitions (m=200): exact "
                   "inside the 95% CI >= 44/50 (binomial tolerance)",
                   coverage_200 >= 44))
    checks.append(("SE shrinks ~1/sqrt(m) across m in {50,200,800} "
                   "(both ratios within [1.8, 2.2])",
                   1.8 <= ratio_a <= 2.2 and 1.8 <= ratio_b <= 2.2))

    # (v5) BY-CONSTRUCTION honesty: in a mixed exact+sampled report, exact
    # rows carry aci_k and never estimate; sampled rows carry the full
    # interval and never aci_k; the sampled ks are typed in k_values_sampled
    mixed = compute_korder_report(het_pop, k_max=3, sample_size=100,
                                  force_sample=True)
    exact_only = compute_korder_report(bs, k_max=3)
    checks.append(("by construction: sampled rows have no bare point field; "
                   "exact rows have no estimate; exact-only reports keep "
                   "schema 0.1 with no sampling keys",
                   all("aci_k" not in r and "ci95" in r
                       for r in mixed["profile"])
                   and mixed["schema"] == KORDER_SAMPLED_SCHEMA_VERSION
                   and all("estimate" not in r and "aci_k" in r
                           for r in exact_only["profile"])
                   and exact_only["schema"] == KORDER_SCHEMA_VERSION
                   and "sampling" not in exact_only
                   and "k_values_sampled" not in exact_only))

    # (v6) sampling determinism + seed commitment: same inputs re-draw the
    # byte-identical report; changing the salt changes the seed (and, on a
    # heterogeneous population, the drawn sample)
    checks.append(("sampled report deterministic; seed commits to "
                   "population+k+m (salt changes the draw)",
                   canonical_json(compute_korder_report(
                       het_pop, k_max=3, sample_size=100, force_sample=True))
                   == canonical_json(mixed)
                   and sampled_aci_k(het_pop, 3, 100, seed_salt=1)["seed"]
                   != sampled_aci_k(het_pop, 3, 100, seed_salt=2)["seed"]))

    # (f) real-ledger report (READ-ONLY) when the runtime ledger exists locally:
    # deterministic across two full rebuilds, and the same-operator baseline holds.
    if os.path.exists(work_molecule.DEFAULT_LEDGER_PATH):
        r1 = compute_report(build_paths())
        r2 = compute_report(build_paths())
        checks.append(("real-ledger report deterministic across two rebuilds",
                       canonical_json(r1) == canonical_json(r2)))
        checks.append(("real-ledger Gamma(operator) and Gamma(repo) are 1.0",
                       r1["concentration_profile"]["operator"] == 1.0
                       and r1["concentration_profile"]["repo"] == 1.0))
        # snapshot-source equivalence: the published snapshot (what a fresh clone
        # has) must yield the byte-identical ACI report
        _snap = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ledger_published.json")
        if os.path.exists(_snap):
            r_snap = compute_report(build_paths(ledger_path=_snap))
            checks.append(("snapshot-source ACI report byte-identical to "
                           "live-ledger report",
                           canonical_json(r_snap) == canonical_json(r1)))
        else:
            print("    (no published snapshot present — snapshot-source ACI check "
                  "SKIPPED)")
    else:
        print("    (no runtime ledger present — real-ledger report SKIPPED; "
              "fixture checks above cover the same code paths)")

    # (g) REAL S_2 CONSISTENCY against the ANCHORED pairwise baseline: on the
    # same as-of paths the anchored baseline measured, ACI_2 via the S_k
    # machinery must equal the anchored pairwise ACI TO THE DIGIT. Source is
    # the live ledger when present, else the published snapshot (fresh clone).
    _proto = os.path.dirname(os.path.abspath(__file__))
    src = next((p for p in (work_molecule.DEFAULT_LEDGER_PATH,
                            os.path.join(_proto, "ledger_published.json"))
                if os.path.exists(p)), None)
    if src is not None:
        baseline = None
        for e in work_molecule._read_ledger(src):
            pl = e.get("payload", {})
            if (pl.get("event") == "aci_baseline_anchored"
                    and pl.get("status") == "aci-baseline-confirmed"):
                baseline = e
        if baseline is not None:
            b_idx = baseline["index"]
            asof_paths = build_paths(ledger_path=src, as_of_index=b_idx - 1)
            korder_real = compute_korder_report(asof_paths, k_max=3,
                                                as_of_ledger_index=b_idx - 1)
            checks.append((f"real ACI_2 via S_k == anchored ledger:{b_idx} "
                           "pairwise baseline to the digit",
                           korder_real["profile"][0]["aci_k"]
                           == baseline["payload"]["pairwise_aci"]
                           and korder_real["path_count"]
                           == baseline["payload"]["path_count"]))
        else:
            print("    (no anchored pairwise baseline on the source — real "
                  "S_2-consistency check SKIPPED)")
        # (g2) EXACT-MODE REGRESSION LOCK: the anchored k-order baseline must
        # keep re-deriving BYTE-IDENTICALLY under the sampled-mode upgrade —
        # an exact-only report still emits schema 0.1 with the anchored hash
        ko_anchor = None
        for e in work_molecule._read_ledger(src):
            pl = e.get("payload", {})
            if (pl.get("event") == "aci_korder_baseline_anchored"
                    and pl.get("status") == "aci-korder-confirmed"):
                ko_anchor = pl
        if ko_anchor is not None:
            ko_ks = (list(ko_anchor.get("k_values_computed", []))
                     + list(ko_anchor.get("k_values_refused", [])))
            ko_re = compute_korder_report(
                build_paths(ledger_path=src,
                            as_of_index=ko_anchor.get("as_of_ledger_index")),
                k_max=max(ko_ks),
                as_of_ledger_index=ko_anchor.get("as_of_ledger_index"))
            checks.append(("anchored k-order baseline re-derives "
                           "byte-identically (schema 0.1, hash to the digit) "
                           "under the sampled-mode upgrade",
                           ko_re["schema"] == KORDER_SCHEMA_VERSION
                           and ko_re["report_hash"]
                           == ko_anchor.get("report_hash")))
        else:
            print("    (no anchored k-order baseline on the source — "
                  "exact-mode regression check SKIPPED)")
    else:
        print("    (no ledger source at all — real S_2-consistency check "
              "SKIPPED)")

    stray_root = sorted(set(os.listdir(_REPO_ROOT)) - root_before)
    proto_dir = os.path.dirname(os.path.abspath(__file__))
    stray_proto = sorted(set(os.listdir(proto_dir)) - proto_before)
    checks.append(("no stray files in repo root", not stray_root))
    checks.append(("no stray files in protocol/", not stray_proto))

    print("--- self-test invariants ---")
    failures = 0
    for name, passed in checks:
        print(f"{name:58s}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
    if stray_root:
        print(f"    stray in repo root: {stray_root}")
    if stray_proto:
        print(f"    stray in protocol/: {stray_proto}")

    ok = failures == 0
    print("\n=== self-test summary: " +
          ("ALL CHECKS BEHAVED CORRECTLY" if ok else "FAILURE — see above") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
