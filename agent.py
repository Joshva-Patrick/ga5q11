"""
Incident Response Agent — main entry point.

Usage:
    python agent.py transcript.txt [--auto-approve]

Pipeline:
  1. Root span "incident.response" (incident.id attribute).
  2. LLM reads transcript -> ranked root-cause hypotheses + evidence gaps.
     Logged as a decision EVENT (not a span) on the root span.
  3. Diagnostic loop: only call a diagnostic tool if it resolves an open
     evidence gap for the CURRENT leading hypothesis. Each call is a CLIENT
     span (see tools.py) fed back into the LLM to update confidence.
     Every re-evaluation is logged as a decision event, with span LINKS to
     every diagnostic span that fed into it (fan-in correlation).
  4. Stop once confidence clears CONFIDENCE_THRESHOLD or no diagnostics left.
  5. Map confirmed root-cause category -> exactly one effect tool.
  6. If risky, emit approval.requested event and block on approval
     (stdin prompt, or auto-approved via --auto-approve / AUTO_APPROVE=1
     for CI/grading runs).
  7. Execute the effect tool once. Store the grader's actual result.
  8. Write incident_result.json (decisions + receipts) and
     incident_trace.json (full OTel span tree) for exact replay/grading.
"""

import argparse
import json
import sys
import uuid

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from otel_setup import init_tracer, dump_trace
from llm_client import call_llm_json
from tools import TOOL_CATALOG, call_tool

CONFIDENCE_THRESHOLD = 0.85

HYPOTHESIS_SYSTEM_PROMPT = """You are an SRE incident triage assistant.
Read the incident transcript and return ONLY JSON (no prose, no markdown):

{
  "hypotheses": [
    {
      "id": "short_snake_case_id",
      "description": "...",
      "confidence": 0.0-1.0,
      "category": "one of: connection_pool_exhaustion | bad_deploy | capacity | unknown",
      "evidence_gaps": ["connection_pool_state", "recent_deploy_correlation", "error_rate_trend"]
    }
  ]
}

List evidence_gaps only for facts NOT already stated in the transcript that
would raise or lower your confidence. Order hypotheses by confidence, highest first.
"""

UPDATE_SYSTEM_PROMPT = """You are an SRE incident triage assistant.
You previously proposed hypotheses. New diagnostic evidence has arrived.
Return ONLY JSON in the same shape as before, with updated confidences and
evidence_gaps (remove gaps that are now resolved). Do not invent tool names.
"""


def build_transcript_prompt(transcript: str) -> str:
    return f"INCIDENT TRANSCRIPT:\n{transcript}\n\nReturn the JSON now."


def build_update_prompt(hypotheses: list, tool_name: str, result: dict) -> str:
    return (
        f"PRIOR HYPOTHESES:\n{json.dumps(hypotheses, indent=2)}\n\n"
        f"NEW EVIDENCE from tool '{tool_name}':\n{json.dumps(result, indent=2)}\n\n"
        "Return the updated JSON now."
    )


def pick_diagnostic_for_gap(gap: str):
    for name, spec in TOOL_CATALOG.items():
        if spec["kind"] == "diagnostic" and gap in spec.get("resolves", []):
            return name
    return None


def pick_effect_for_category(category: str):
    for name, spec in TOOL_CATALOG.items():
        if spec["kind"] == "effect" and spec.get("category") == category:
            return name
    return None


def request_approval(tool_name: str, auto_approve: bool) -> bool:
    if auto_approve:
        return True
    resp = input(f"Approve risky action '{tool_name}'? [y/N]: ").strip().lower()
    return resp == "y"


def run(transcript_path: str, auto_approve: bool):
    tracer = init_tracer()
    incident_id = str(uuid.uuid4())
    diagnostics_run = []
    decisions_log = []

    with open(transcript_path) as f:
        transcript = f.read()

    with tracer.start_as_current_span(
        "incident.response", kind=SpanKind.INTERNAL
    ) as root_span:
        root_span.set_attribute("incident.id", incident_id)

        # --- Step 1: initial hypotheses ---
        hyp_data = call_llm_json(
            HYPOTHESIS_SYSTEM_PROMPT, build_transcript_prompt(transcript)
        )
        hypotheses = hyp_data["hypotheses"]
        root_span.add_event(
            "decision.hypotheses_generated",
            {"hypotheses": json.dumps(hypotheses)},
        )
        decisions_log.append({"step": "initial_hypotheses", "hypotheses": hypotheses})

        contributing_span_ids = []  # for fan-in links on later decision events

        # --- Step 2: diagnostic loop, only what's needed ---
        while True:
            top = hypotheses[0]
            if top["confidence"] >= CONFIDENCE_THRESHOLD or not top.get("evidence_gaps"):
                break

            gap = top["evidence_gaps"][0]
            tool_name = pick_diagnostic_for_gap(gap)
            if tool_name is None:
                # No tool can resolve this gap; drop it and continue with next gap.
                top["evidence_gaps"].pop(0)
                continue

            result, span_ctx, first_attempt_id = call_tool(tracer, tool_name, incident_id)
            diagnostics_run.append({"tool": tool_name, "gap_resolved": gap, "result": result})
            contributing_span_ids.append(format(span_ctx.span_id, "016x"))

            hyp_data = call_llm_json(
                UPDATE_SYSTEM_PROMPT,
                build_update_prompt(hypotheses, tool_name, result),
            )
            hypotheses = hyp_data["hypotheses"]

            # Decision event with fan-in links to every diagnostic span so far.
            event_span = trace.get_current_span()
            event_span.add_event(
                "decision.hypotheses_updated",
                {
                    "hypotheses": json.dumps(hypotheses),
                    "fed_by_spans": json.dumps(contributing_span_ids),
                },
            )
            decisions_log.append(
                {
                    "step": "update_after_diagnostic",
                    "tool": tool_name,
                    "hypotheses": hypotheses,
                }
            )

        root_cause = hypotheses[0]
        root_span.set_attribute("root_cause.id", root_cause["id"])
        root_span.set_attribute("root_cause.confidence", root_cause["confidence"])
        root_span.add_event(
            "decision.root_cause_confirmed",
            {"root_cause": json.dumps(root_cause)},
        )

        # --- Step 3: choose exactly one effect tool ---
        effect_tool = pick_effect_for_category(root_cause["category"])
        action_record = {"tool": None, "approved": None, "result": None}

        if effect_tool is None:
            root_span.add_event("decision.no_action_available")
        else:
            tool_spec = TOOL_CATALOG[effect_tool]
            approved = True
            if tool_spec.get("risky"):
                root_span.add_event("approval.requested", {"tool": effect_tool})
                approved = request_approval(effect_tool, auto_approve)
                root_span.add_event(
                    "approval.granted" if approved else "approval.denied",
                    {"tool": effect_tool},
                )

            action_record["tool"] = effect_tool
            action_record["approved"] = approved

            if approved:
                result, span_ctx, _ = call_tool(tracer, effect_tool, incident_id)
                action_record["result"] = result
                root_span.add_event(
                    "action.executed",
                    {"tool": effect_tool, "result": json.dumps(result)},
                )
            else:
                root_span.add_event("action.skipped_not_approved", {"tool": effect_tool})

    # --- Step 4: persist state for grading/replay ---
    final_record = {
        "incident_id": incident_id,
        "root_cause": root_cause,
        "diagnostics_run": diagnostics_run,
        "decisions_log": decisions_log,
        "action": action_record,
    }
    with open("incident_result.json", "w") as f:
        json.dump(final_record, f, indent=2)

    dump_trace("incident_trace.json")
    print(json.dumps(final_record, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transcript")
    parser.add_argument("--auto-approve", action="store_true")
    args = parser.parse_args()
    run(args.transcript, args.auto_approve or bool(__import__("os").environ.get("AUTO_APPROVE")))
