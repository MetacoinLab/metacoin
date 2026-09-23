"""Stable safe error codes for the service; reuses the experiments' refusal codes."""
from experiments.work_contracts import refusals

HTTP_STATUS = {
    'UNAUTHENTICATED': 401, 'FORBIDDEN': 403, 'NOT_FOUND': 404, 'CONFLICT': 409, 'IDEMPOTENCY_CONFLICT': 409,
    'STATE_CONFLICT': 409, 'RATE_LIMITED': 429, 'PAYLOAD_TOO_LARGE': 413, 'CSRF': 403, 'CAPABILITY_UNAVAILABLE': 501,
    'PROVIDER_UNAVAILABLE': 503, 'VALIDATION': 422, 'JOURNAL_BUSY': 503,
}
ACTIONS = dict(refusals.ACTIONS, **{
    'UNAUTHENTICATED': 'present a valid bearer credential or sign in',
    'FORBIDDEN': 'the authenticated principal may not perform this operation on this resource',
    'NOT_FOUND': 'no such resource in your workspace',
    'CONFLICT': 'the resource is not in a state that permits this operation',
    'IDEMPOTENCY_CONFLICT': 'this idempotency key was used with different content',
    'STATE_CONFLICT': 'the object changed underneath this request; reload and retry',
    'RATE_LIMITED': 'too many queued jobs for this workspace; wait for completion',
    'PAYLOAD_TOO_LARGE': 'request body or upload exceeds the configured limit',
    'CSRF': 'form token missing or stale; reload the page',
    'CAPABILITY_UNAVAILABLE': 'this capability is not installed or not configured; nothing was written',
    'PROVIDER_UNAVAILABLE': 'the payment provider could not be reached; exposure retained',
    'VALIDATION': 'request fields are invalid; see details',
})


class ServiceError(Exception):
    def __init__(self, code, detail=None, status=None):
        super().__init__(code)
        self.code = code
        self.detail = detail          # constant strings or field names only, never values
        self.status = status or HTTP_STATUS.get(code, 400)

    def body(self):
        return {'error': True, 'code': self.code, 'action': ACTIONS.get(self.code, 'refused'), 'detail': self.detail}


def from_exception(exc):
    if isinstance(exc, ServiceError):
        return exc
    code, message = refusals.classify(exc)
    status = 422 if code in ('INPUT_INVALID', 'MODEL_DOMAIN', 'CONTRACT_INVALID', 'DISCLOSURE_POLICY') else 409
    if code in ('EXPIRED', 'BUDGET_EXHAUSTED', 'ENTITLEMENT_CONSUMED', 'REQUEST_ID_REBOUND', 'OUTCOME_CONFLICT'):
        status = 409
    if code in ('UNAUTHORIZED', 'UNAUTHORIZED_OR_EXPIRED', 'SPEND_REFUSED'):
        status = 403
    if code == 'JOURNAL_BUSY':
        status = 503
    return ServiceError(code, message, status)
