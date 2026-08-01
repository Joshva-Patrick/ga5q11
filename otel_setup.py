"""
OpenTelemetry setup for the incident agent.

- Uses the standard W3C TraceContext propagator (traceparent/tracestate) so
  outgoing HTTP calls to the grader's tool endpoints carry correct headers.
- Exports spans to BOTH:
    1) an in-memory exporter -> dumped to incident_trace.json for exact
       replay/diffing against what the grader expects
    2) OTLP (console by default; set OTEL_EXPORTER_OTLP_ENDPOINT to send to
       a real collector, e.g. http://localhost:4318)
"""

import json
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

_PROPAGATOR = TraceContextTextMapPropagator()
_MEMORY_EXPORTER = InMemorySpanExporter()

_provider = None


def init_tracer(service_name: str = "incident-agent") -> trace.Tracer:
    global _provider
    if _provider is not None:
        return trace.get_tracer(service_name)

    resource = Resource.create({"service.name": service_name})
    _provider = TracerProvider(resource=resource)

    # In-memory: lets us dump the exact span tree for grading/replay.
    _provider.add_span_processor(SimpleSpanProcessor(_MEMORY_EXPORTER))

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        # Real OTLP HTTP exporter, only imported if actually configured so
        # the scaffold runs with zero external deps in mock mode.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        _provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint + "/v1/traces"))
        )
    else:
        # Fallback: dump spans to stdout so you can see the tree during dev.
        _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(_provider)
    return trace.get_tracer(service_name)


def inject_traceparent(headers: dict) -> dict:
    """Inject the current active span's context as a W3C traceparent header."""
    _PROPAGATOR.inject(headers)
    return headers


def extract_context(headers: dict):
    """Extract a context from an incoming traceparent header (server side)."""
    return _PROPAGATOR.extract(headers)


def dump_trace(path: str):
    """Write every finished span (attrs, kind, parent/links) as JSON for
    exact comparison against grader-expected telemetry."""
    spans = _MEMORY_EXPORTER.get_finished_spans()
    out = []
    for s in spans:
        ctx = s.get_span_context()
        out.append(
            {
                "name": s.name,
                "trace_id": format(ctx.trace_id, "032x"),
                "span_id": format(ctx.span_id, "016x"),
                "parent_span_id": format(s.parent.span_id, "016x") if s.parent else None,
                "kind": s.kind.name,
                "start_time_unix_nano": s.start_time,
                "end_time_unix_nano": s.end_time,
                "attributes": dict(s.attributes or {}),
                "events": [
                    {
                        "name": e.name,
                        "timestamp": e.timestamp,
                        "attributes": dict(e.attributes or {}),
                    }
                    for e in s.events
                ],
                "links": [
                    {
                        "trace_id": format(l.context.trace_id, "032x"),
                        "span_id": format(l.context.span_id, "016x"),
                        "attributes": dict(l.attributes or {}),
                    }
                    for l in s.links
                ],
                "status": s.status.status_code.name,
            }
        )
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    return out
