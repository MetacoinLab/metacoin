"""Stable, machine-readable refusal codes for operators; no private values.

Every refusal raised by this package and by the receipt module carries a
constant message written by the maintainers (a test asserts that no raise
site interpolates data). The CLI maps that message to a code here and prints
both. Other exceptions (OS errors, SQLite errors, defects) are reported by
category only, because their text can contain paths or private input.
"""
import sqlite3
from experiments.private_receipts import receipt as merkle

# message -> code. Codes are the contract; messages may be reworded.
CODES = {
    # input / evidence structure
    'unexpected object fields': 'INPUT_INVALID', 'unexpected fields': 'INPUT_INVALID',
    'expected a 32-byte lowercase hex value': 'INPUT_INVALID',
    'field name must be lowercase ASCII snake_case': 'INPUT_INVALID',
    'invalid leaf index': 'EVIDENCE_INVALID', 'JSON nesting/node limit exceeded': 'INPUT_INVALID',
    'JSON object keys must be strings': 'INPUT_INVALID',
    'integer exceeds interoperable JSON range': 'INPUT_INVALID',
    'unsupported JSON type (floats are forbidden)': 'INPUT_INVALID', 'invalid JSON': 'INPUT_INVALID',
    'JSON size limit exceeded': 'INPUT_TOO_LARGE', 'duplicate JSON object key': 'INPUT_INVALID',
    'file size limit exceeded': 'INPUT_TOO_LARGE', 'invalid or unsupported JSON': 'INPUT_INVALID',
    'field value size limit exceeded': 'INPUT_TOO_LARGE', 'evidence size limit exceeded': 'INPUT_TOO_LARGE',
    'invalid identifier': 'INPUT_INVALID', 'integer outside declared domain': 'MODEL_DOMAIN',
    'unsupported units or assumptions': 'MODEL_DOMAIN', 'unsupported provenance': 'MODEL_DOMAIN',
    'invalid private label': 'INPUT_INVALID', 'reversed available bounds': 'MODEL_DOMAIN',
    'segment limit exceeded': 'MODEL_DOMAIN', 'reversed power bounds': 'MODEL_DOMAIN',
    'margin decomposition does not reconcile': 'INTERNAL_DEFECT',
    # commitments and disclosures
    'unsupported receipt schema, kind, or tree size': 'EVIDENCE_VERSION',
    'unsupported vault schema': 'EVIDENCE_VERSION', 'vault must cover exactly 64 slots': 'EVIDENCE_INVALID',
    'duplicate vault field or slot': 'EVIDENCE_INVALID', 'duplicate padding slot': 'EVIDENCE_INVALID',
    'vault does not match its receipt': 'EVIDENCE_INVALID',
    'evidence must contain between 1 and 64 fields': 'EVIDENCE_INVALID',
    'select between 1 and 64 field names explicitly': 'DISCLOSURE_POLICY',
    'duplicate disclosure request': 'DISCLOSURE_POLICY', 'requested field is absent': 'DISCLOSURE_POLICY',
    'receipt root does not match the independently trusted pin': 'PIN_MISMATCH',
    'invalid disclosure count': 'EVIDENCE_INVALID',
    'Merkle path must contain exactly six siblings': 'EVIDENCE_INVALID',
    'disclosure does not belong to the trusted receipt': 'EVIDENCE_INVALID',
    'required disclosure is missing': 'DISCLOSURE_POLICY',
    'duplicate disclosed field or slot': 'EVIDENCE_INVALID',
    'input, receipt, and vault paths must differ': 'INPUT_INVALID',
    'output already exists; choose fresh paths': 'OUTPUT_EXISTS',
    'private evidence root does not match trusted pin': 'PIN_MISMATCH',
    'evidence differs from the complete recomputed result': 'EVIDENCE_MISMATCH',
    'prohibited public disclosure': 'DISCLOSURE_POLICY', 'wrong evidence binding': 'EVIDENCE_MISMATCH',
    'unsupported outcome': 'EVIDENCE_INVALID', 'malformed disclosed explanation': 'EVIDENCE_INVALID',
    # contract terms and versions
    'unknown verifier bundle': 'VERIFIER_UNKNOWN',
    'verifier bundle superseded; read-only verification only': 'VERIFIER_SUPERSEDED',
    'unsupported contract semantics or installed verifier': 'CONTRACT_VERSION',
    'invalid policy list': 'CONTRACT_INVALID', 'unsupported completion outcome': 'CONTRACT_INVALID',
    'incompatible disclosure policy': 'DISCLOSURE_POLICY', 'contract does not match owner pin': 'PIN_MISMATCH',
    'unsupported adapter capability or destination': 'ADAPTER_CAPABILITY',
    'expiration must be in the future': 'EXPIRED', 'contract expired': 'EXPIRED',
    # journal authority and budget
    'journal must be a private regular file': 'JOURNAL_UNSAFE',
    'journal directory must not be writable by others': 'JOURNAL_UNSAFE',
    'campaign configuration is immutable': 'CAMPAIGN_MISMATCH',
    'unknown campaign journal': 'JOURNAL_MISSING',
    'campaign identifier and limit required': 'INPUT_INVALID',
    'provider state missing; initial balance required to create it': 'ADAPTER_CAPABILITY',
    'unauthorized or expired contract': 'UNAUTHORIZED_OR_EXPIRED',
    'job entitlement cannot be reissued': 'ENTITLEMENT_CONSUMED', 'unregistered job': 'JOB_UNKNOWN',
    'unauthorized or expired audit': 'UNAUTHORIZED_OR_EXPIRED',
    'accepted evidence root is immutable': 'EVIDENCE_IMMUTABLE',
    'registered contract changed during audit': 'JOURNAL_CONFLICT',
    'work not accepted under the contract': 'WORK_NOT_ACCEPTED',
    'spend authorization refused': 'SPEND_REFUSED', 'action differs from the authorized binding': 'BINDING_MISMATCH',
    'idempotency identifier rebound': 'REQUEST_ID_REBOUND',
    'job action entitlement already used': 'ENTITLEMENT_CONSUMED',
    'campaign budget exhausted': 'BUDGET_EXHAUSTED', 'authorization expired': 'EXPIRED',
    'invalid adapter conclusion': 'ADAPTER_RESPONSE_INVALID',
    'invalid settlement reference': 'ADAPTER_RESPONSE_INVALID', 'unknown action': 'ACTION_UNKNOWN',
    'unbound adapter response': 'ADAPTER_RESPONSE_UNBOUND',
    'inconsistent adapter response': 'ADAPTER_RESPONSE_INVALID',
    'conflicting terminal outcome': 'OUTCOME_CONFLICT', 'unauthorized reconciliation': 'UNAUTHORIZED',
    'unauthorized status access': 'UNAUTHORIZED', 'action must be an object': 'INPUT_INVALID',
    'adapter idempotency conflict': 'REQUEST_ID_REBOUND',
    'adapter reconciliation conflict': 'REQUEST_ID_REBOUND',
    'reconciliation unavailable for this adapter session': 'RECONCILIATION_UNAVAILABLE',
    # packages
    'package member not permitted': 'PACKAGE_INVALID', 'package manifest mismatch': 'PACKAGE_INVALID',
    'package member too large': 'PACKAGE_TOO_LARGE', 'package contains private material': 'PACKAGE_PRIVATE',
    'unsupported package version': 'PACKAGE_VERSION', 'package member duplicated': 'PACKAGE_INVALID',
    'package member is not a regular file': 'PACKAGE_INVALID', 'package pin differs from operator pin': 'PIN_MISMATCH',
}

# Codes whose meaning an operator may act on without seeing any value.
ACTIONS = {
    'INPUT_INVALID': 'fix the JSON input (strict: no floats, duplicate keys, unknown fields)',
    'INPUT_TOO_LARGE': 'reduce the input or evidence size',
    'MODEL_DOMAIN': 'input outside the declared integer domain or units of this model',
    'EVIDENCE_INVALID': 'the vault or bundle is malformed; regenerate from the original inputs',
    'EVIDENCE_VERSION': 'unsupported receipt/vault schema version',
    'EVIDENCE_MISMATCH': 'evidence differs from recomputation or contract bindings; not accepted',
    'PIN_MISMATCH': 'operator pin differs from the artifact; obtain the pin from the trusted channel',
    'DISCLOSURE_POLICY': 'disclosure request conflicts with the contract policy',
    'VERIFIER_UNKNOWN': 'the contract names a verifier bundle this checkout does not allowlist',
    'VERIFIER_SUPERSEDED': 'historical bundle: verify/inspect only; no new registration, audit or spend',
    'CONTRACT_VERSION': 'unsupported contract schema/semantics for this checkout',
    'CONTRACT_INVALID': 'contract policy lists are malformed',
    'ADAPTER_CAPABILITY': 'adapter cannot serve this capability/destination; nothing dispatched',
    'EXPIRED': 'authorization window passed; no new spend; status/reconcile remain available',
    'JOURNAL_UNSAFE': 'journal file/directory permissions are not private',
    'CAMPAIGN_MISMATCH': 'journal belongs to a different campaign or limit',
    'JOURNAL_MISSING': 'no campaign journal at that path; run campaign init first',
    'UNAUTHORIZED_OR_EXPIRED': 'actor is not the contract owner/auditor, or the contract expired',
    'UNAUTHORIZED': 'actor does not match the action',
    'ENTITLEMENT_CONSUMED': 'this job already has its single action or was registered with other terms',
    'JOB_UNKNOWN': 'job is not registered in this journal',
    'EVIDENCE_IMMUTABLE': 'an accepted evidence root is already recorded for this job',
    'JOURNAL_CONFLICT': 'journal state changed during the operation; retry',
    'WORK_NOT_ACCEPTED': 'no accepted audit under this contract (policy or missing audit)',
    'SPEND_REFUSED': 'actor, capability, acceptance or expiry check failed; nothing dispatched',
    'BINDING_MISMATCH': 'request differs from the journal-authorized binding; nothing dispatched',
    'REQUEST_ID_REBOUND': 'request identifier already bound to different terms',
    'BUDGET_EXHAUSTED': 'campaign exposure plus this amount exceeds the fixed cap; nothing dispatched',
    'ADAPTER_RESPONSE_INVALID': 'adapter answer is malformed/inconsistent; exposure retained',
    'ADAPTER_RESPONSE_UNBOUND': 'adapter answer does not bind this request; exposure retained',
    'OUTCOME_CONFLICT': 'a different terminal outcome is already recorded; not replaced',
    'ACTION_UNKNOWN': 'no such action request in this journal',
    'RECONCILIATION_UNAVAILABLE': 'this adapter session has no authoritative record; exposure retained',
    'PACKAGE_INVALID': 'package structure/manifest invalid; nothing imported',
    'PACKAGE_TOO_LARGE': 'package exceeds size limits; nothing imported',
    'PACKAGE_PRIVATE': 'package contains private material; refused',
    'PACKAGE_VERSION': 'unsupported package version',
    'OUTPUT_EXISTS': 'output path already exists; choose a fresh path (existing files are never overwritten)',
    'FILE_MISSING': 'an input file was not found',
    'PERMISSION_DENIED': 'filesystem permission denied',
    'JOURNAL_BUSY': 'journal locked by another process; retry',
    'LOCAL_OPERATION_FAILED': 'a local file/database operation failed (no details echoed)',
    'INTERNAL_DEFECT': 'internal consistency check failed; report with the command used, not the data',
    'REFUSED': 'refused for a reason without a specific code',
}


def classify(exc):
    """(code, safe_message). Only constant maintainer-written messages are echoed."""
    if isinstance(exc, merkle.Invalid):
        message = str(exc)
        return CODES.get(message, 'REFUSED'), message
    if isinstance(exc, FileExistsError):
        return 'OUTPUT_EXISTS', None
    if isinstance(exc, FileNotFoundError):
        return 'FILE_MISSING', None
    if isinstance(exc, PermissionError):
        return 'PERMISSION_DENIED', None
    if isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc):
        return 'JOURNAL_BUSY', None
    if isinstance(exc, (OSError, sqlite3.Error)):
        return 'LOCAL_OPERATION_FAILED', None
    return 'INTERNAL_DEFECT', None
