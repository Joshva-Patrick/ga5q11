"""
Tool catalog and invocation.

Each tool declares:
  - kind: "diagnostic" | "effect"
  - resolves: which evidence gaps it can confirm (used for selection)
  - category: which root-cause category an *effect* tool remediates
  - risky: whether it needs human approval before running
  - endpoint: path on GRADER_BASE_URL (edit these to match the real grader)

>>> REPLACE the TOOL_CATALOG contents and endpoint paths with the ones from
>>> your actual assignment brief / grader spec. This is a representative
>>> example set (DB connection pool exhaustion incident).

If GRADER_BASE_URL is not set, call_tool() runs in MOCK MODE so the whole
pipeline is runnable/demoable without the real grader.
"""

import json
import os
import time
import urllib.request
import urllib.error

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from otel_setup import inject_traceparent

REDACT_KEYS = {"password", "token", "secret", "api_key", "authorization"}

TOOL_CATALOG = {
    "check_db_connections": {
        "kind": "diagnostic",
        "resolves": ["connection_pool_state"],
        "endpoint": "/diag/db-connections",
    },
    "check_recent_deploys": {
        "kind": "diagnostic",
        "resolves": ["recent_deploy_correlation"],
        "endpoint": "/diag/recent-deploys",
    },
    "check_error_rate": {
        "kind": "diagnostic",
        "resolves": ["error_rate_trend"],
        "endpoint": "/diag/error-rate",
    },
    "restart_service": {
        "kind": "effect",
        "category": "connection_pool_exhaustion",
        "risky": True,
        "endpoint": "/action/restart-service",
    },
    "rollback_deploy": {
        "kind": "effect",
        "category": "bad_deploy",
        "risky": True,
        "endpoint": "/action/rollback-deploy",
    },
    "scale_up": {
        "kind": "effect",
        "category": "capacity",
        "risky": False,
        "endpoint": "/action/scale-up",
    },
}


def _redact(payload: dict) -> dict:
    out = {}
    for k, v in payload.items():
        if k.lower() in REDACT_KEYS:
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


def _mock_response(tool_name: str, incident_id: str) -> dict:
    """Deterministic fake grader responses so the pipeline runs end-to-end
    without a live grader. Delete this once GRADER_BASE_URL is real."""
    canned = {
        "check_db_connections": {"pool_used": 198, "pool_max": 200, "status": "near_exhaustion"},
        "check_recent_deploys": {"last_deploy_minutes_ago": 240, "status": "no_recent_deploy"},
        "check_error_rate": {"error_rate_pct": 42.5, "trend": "spiking"},
        "restart_service": {"result": "success", "receipt_id": "rcpt-mock-0001"},
        "rollback_deploy": {"result": "skipped_not_applicable"},
        "scale_up": {"result": "success", "receipt_id": "rcpt-mock-0002"},
    }
    return canned.get(tool_name, {"result": "unknown_tool"})


def call_tool(
    tracer: trace.Tracer,
    tool_name: str,
    incident_id: str,
    max_retries: int = 2,
    first_attempt_span_id: str | None = None,
):
    """
    Invoke a tool as a CLIENT span, injecting a real W3C traceparent header.
    Retries create sibling spans linked back to the first attempt (fan-in
    correlation), each tagged with attempt.number.

    Returns: (result_dict, span_context_of_the_span_that_produced_the_result)
    """
    tool = TOOL_CATALOG[tool_name]
    grader_base = os.environ.get("GRADER_BASE_URL")

    last_exc = None
    for attempt in range(1, max_retries + 2):  # 1 initial + max_retries
        links = []
        if first_attempt_span_id:
            links = [trace.Link(trace.SpanContext(
                trace_id=trace.get_current_span().get_span_context().trace_id,
                span_id=int(first_attempt_span_id, 16),
                is_remote=False,
            ))]

        with tracer.start_as_current_span(
            f"tool.{tool_name}", kind=SpanKind.CLIENT, links=links
        ) as span:
            span.set_attribute("incident.id", incident_id)
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.kind", tool["kind"])
            span.set_attribute("attempt.number", attempt)

            if first_attempt_span_id is None:
                first_attempt_span_id = format(
                    span.get_span_context().span_id, "016x"
                )

            headers = {"content-type": "application/json"}
            inject_traceparent(headers)  # <-- traceparent correlates to THIS span

            try:
                if grader_base:
                    body = json.dumps({"incident_id": incident_id}).encode()
                    req = urllib.request.Request(
                        grader_base + tool["endpoint"], data=body, headers=headers
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        result = json.loads(resp.read())
                else:
                    time.sleep(0.05)  # simulate latency
                    result = _mock_response(tool_name, incident_id)

                safe_result = _redact(result)
                span.set_attribute("action.result", json.dumps(safe_result))
                span.set_status(Status(StatusCode.OK))
                return result, span.get_span_context(), first_attempt_span_id

            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                continue  # retry -> next loop iteration opens a new sibling span

    raise RuntimeError(f"Tool {tool_name} failed after retries: {last_exc}")
