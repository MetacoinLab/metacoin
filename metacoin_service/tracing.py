"""Optional OpenTelemetry tracing for the request-to-job paths.

Disabled unless METACOIN_TRACING=1. Spans carry only bounded, non-sensitive attributes (operation names, closed
enumerations, internal record ids, durations); prompts, documents, tokens, credentials, payment payloads and array
contents never become attributes, events or exception text. Export goes to a task-owned local file
(METACOIN_TRACE_FILE, JSON lines) or stays in memory; no hosted collector is contacted. Context propagation across
the API -> worker boundary uses the job record: the API stores the trace id of the admitting request in the job's
history reference and the worker starts its execution span as a child of that context (W3C traceparent), so one
trace covers admission, claim, execution, verification and usage finalization. Model inference inside the runtime
child is not instrumented (marked limit)."""
import json
import os
import threading
import time
from contextlib import contextmanager

ENABLED = os.environ.get('METACOIN_TRACING', '0') == '1'
_provider = None
_lock = threading.Lock()
SAFE_KEYS = {'job_id', 'kind', 'operation', 'workspace', 'phase', 'backend', 'state', 'outcome', 'class', 'principal_role', 'worker', 'route', 'status', 'queue_ms', 'load_ms', 'inference_ms', 'verification_ms', 'items', 'revision_id'}


class _JsonLineExporter:
    """SpanExporter writing one JSON line per span to a private local file (task-owned)."""

    def __init__(self, path):
        self.path = path

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        try:
            with open(self.path, 'a') as f:
                for s in spans:
                    ctx = s.get_span_context(); parent = s.parent
                    f.write(json.dumps({'name': s.name, 'trace_id': format(ctx.trace_id, '032x'), 'span_id': format(ctx.span_id, '016x'), 'parent_span_id': format(parent.span_id, '016x') if parent else None,
                                        'start_ns': s.start_time, 'end_ns': s.end_time, 'duration_ms': (s.end_time - s.start_time) / 1e6 if s.end_time and s.start_time else None,
                                        'attributes': {k: v for k, v in (s.attributes or {}).items() if k in SAFE_KEYS}, 'status': str(s.status.status_code.name) if s.status else None, 'service': (s.resource.attributes or {}).get('service.name')}) + '\n')
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def provider(service_name):
    """Lazily configured tracer provider (None when tracing is off or the SDK is absent)."""
    global _provider
    if not ENABLED:
        return None
    with _lock:
        if _provider is not None:
            return _provider
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError:
            return None
        p = TracerProvider(resource=Resource.create({'service.name': service_name}))
        path = os.environ.get('METACOIN_TRACE_FILE')
        if path:
            p.add_span_processor(SimpleSpanProcessor(_JsonLineExporter(path)))
        trace.set_tracer_provider(p)
        _provider = p
        return p


def tracer(service_name='metacoin-service'):
    p = provider(service_name)
    if p is None:
        return None
    from opentelemetry import trace
    return trace.get_tracer('metacoin', tracer_provider=p)


@contextmanager
def span(name, service='metacoin-service', parent_traceparent=None, **attrs):
    """Context manager yielding (span or None). Attributes are filtered to SAFE_KEYS."""
    t = tracer(service)
    if t is None:
        yield None
        return
    from opentelemetry import context as otel_context, trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    ctx = None
    if parent_traceparent:
        ctx = TraceContextTextMapPropagator().extract({'traceparent': parent_traceparent})
    with t.start_as_current_span(name, context=ctx) as s:
        for k, v in attrs.items():
            if k in SAFE_KEYS and v is not None:
                s.set_attribute(k, v if isinstance(v, (int, float, bool)) else str(v)[:120])
        try:
            yield s
        except Exception as exc:
            s.set_attribute('status', 'error:' + type(exc).__name__)     # class name only; never the message
            raise


def current_traceparent():
    """The W3C traceparent of the active span (for storing on the job record), or None."""
    if not ENABLED:
        return None
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get('traceparent')
    except Exception:
        return None


def status():
    return {'enabled': ENABLED, 'export': ('file: ' + os.environ['METACOIN_TRACE_FILE']) if os.environ.get('METACOIN_TRACE_FILE') else ('in-memory only' if ENABLED else 'off'),
            'propagation': 'W3C traceparent stored on the job at admission; worker spans join it', 'limits': 'runtime child (model inference) and node transport are not instrumented', 'attributes': sorted(SAFE_KEYS),
            'privacy': 'no prompts, documents, tokens, credentials, payment payloads or array contents in spans'}
