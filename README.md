# Observable Incident Agent

An agent that reads a noisy incident transcript, forms ranked root-cause
hypotheses, runs **only** the diagnostics needed to confirm one, executes
**one** gated recovery action, and records both a JSON result and a full
OpenTelemetry trace for exact replay against grader output.

## Files
| File | Purpose |
|---|---|
| `agent.py` | Orchestrator: hypothesis loop, diagnostic selection, approval gate, action execution, JSON output |
| `tools.py` | Tool catalog + tool invocation (CLIENT spans, traceparent injection, retries, redaction) |
| `otel_setup.py` | Tracer setup, W3C propagator, in-memory span dump for replay |
| `llm_client.py` | Pluggable LLM call (Anthropic API or local Ollama) — model choice doesn't matter for marks |
| `transcript_sample.txt` | Example noisy transcript |
| `test_dry_run.py` | Stubs the LLM so you can verify the pipeline offline (no API key, no grader) |

## What is a placeholder you MUST replace

This scaffold ships with a **representative** example (DB connection-pool
exhaustion incident) so it's runnable end to end. Before submitting:

1. **`tools.py` → `TOOL_CATALOG`**: replace names, `endpoint` paths,
   `resolves` gaps, and effect `category` mapping with the real tool names
   and endpoints from your assignment's grader spec.
2. **`tools.py` → `_mock_response`**: delete once `GRADER_BASE_URL` is set
   to the real grader — it's only there so the pipeline runs without one.
3. **`agent.py` → prompts**: adjust the hypothesis JSON schema/categories
   to match the incident domain in your actual transcript.
4. **Output schema**: `incident_result.json`'s shape (`root_cause`,
   `diagnostics_run`, `action`, `decisions_log`) is a reasonable default —
   check your brief's exact required JSON schema and rename/restructure
   fields to match, since grading is exact-match.

## Step-by-step: run locally (mock mode, no keys needed)

```bash
cd incident_agent
pip install -r requirements.txt
python3 test_dry_run.py
```

This stubs the LLM and grader so you can see the full pipeline (spans,
decisions, retries, approval, output files) work immediately.

Outputs:
- `incident_result.json` — stored decision/action state
- `incident_trace.json` — full span tree (name, trace_id, span_id,
  parent_span_id, kind, attributes, events, links) for replay/diffing

## Step-by-step: run against a real LLM

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or for local/free: export OLLAMA_HOST=http://localhost:11434 (ollama pull llama3.1)

python3 agent.py transcript_sample.txt --auto-approve
```

Drop `--auto-approve` to be prompted interactively for the risky action.

## Step-by-step: point at the real grader

```bash
export GRADER_BASE_URL=https://your-grader-host
export ANTHROPIC_API_KEY=sk-ant-...
python3 agent.py my_real_transcript.txt --auto-approve
```

`tools.py` will now POST to `GRADER_BASE_URL + endpoint` with a
`traceparent` header derived from the CLIENT span it just opened, and use
the grader's actual JSON response as both the span's `action.result`
attribute and the stored JSON — so they match by construction, not by
copying.

## Step-by-step: export real OTLP telemetry

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # e.g. an otel-collector or Jaeger
python3 agent.py transcript_sample.txt --auto-approve
```

Without this set, spans print to the console (dev mode) — the
`incident_trace.json` dump happens either way and is the source of truth
for grading, since it captures the exact span tree, not just a summary.

## How correctness maps to the grading criteria

- **Correct decisions**: hypothesis loop only advances when confidence is
  below `CONFIDENCE_THRESHOLD` (0.85, tune in `agent.py`) — checkable via
  `decisions_log` in `incident_result.json`.
- **Only needed diagnostics**: `pick_diagnostic_for_gap` only fires a tool
  when the top hypothesis still has an open, tool-resolvable gap — no
  blanket "run everything."
- **One recovery action**: `pick_effect_for_category` returns exactly one
  tool name for the confirmed root cause's category; there is no loop over
  effect tools.
- **Safe approval handling**: risky effect tools (`risky: True` in
  `TOOL_CATALOG`) always emit `approval.requested` before execution and
  only run if approved — auditable as span events.
- **Stored state matches observed results**: `action.result` in
  `incident_result.json` is literally the grader's HTTP response body,
  never fabricated or inferred.
- **Exact telemetry**: `traceparent` headers are derived from the CLIENT
  span via the standard `TraceContextTextMapPropagator`
  (`otel_setup.inject_traceparent`), so header and span always agree by
  construction. Retries create sibling CLIENT spans linked back to the
  first attempt (fan-in via `trace.Link`). Redaction happens on the
  attribute value before `set_attribute`, never by omitting the key.

## Deploying

This is a plain Python script, deployable anywhere Python 3.10+ runs
(container, VM, serverless function). For a grader that calls your agent
as a service rather than a CLI, wrap `agent.run()` in a small HTTP handler
(Flask/FastAPI) that accepts the transcript in the request body and
returns `incident_result.json`'s contents — the OTel plumbing is
unchanged either way.
