"""
Stubs call_llm_json so the whole pipeline (spans, retries, fan-in, redaction,
approval, JSON output) can be verified without any real LLM or grader.
Delete/ignore this file once you're wired to the real LLM + grader.
"""
import json
import llm_client

_CALL_COUNT = {"n": 0}


def fake_call_llm_json(system, user):
    _CALL_COUNT["n"] += 1
    if _CALL_COUNT["n"] == 1:
        # initial hypotheses: ambiguous, needs diagnostics
        return {
            "hypotheses": [
                {
                    "id": "connection_pool_exhaustion",
                    "description": "DB connection pool exhausted causing timeouts",
                    "confidence": 0.5,
                    "category": "connection_pool_exhaustion",
                    "evidence_gaps": ["connection_pool_state", "recent_deploy_correlation"],
                },
                {
                    "id": "bad_deploy",
                    "description": "Recent deploy introduced regression",
                    "confidence": 0.3,
                    "category": "bad_deploy",
                    "evidence_gaps": ["recent_deploy_correlation"],
                },
            ]
        }
    elif _CALL_COUNT["n"] == 2:
        # after check_db_connections -> near_exhaustion, confidence jumps
        return {
            "hypotheses": [
                {
                    "id": "connection_pool_exhaustion",
                    "description": "DB connection pool exhausted causing timeouts",
                    "confidence": 0.9,
                    "category": "connection_pool_exhaustion",
                    "evidence_gaps": [],
                },
                {
                    "id": "bad_deploy",
                    "description": "Recent deploy introduced regression",
                    "confidence": 0.1,
                    "category": "bad_deploy",
                    "evidence_gaps": [],
                },
            ]
        }
    else:
        raise AssertionError("Should have stopped after confidence >= threshold")


llm_client.call_llm_json = fake_call_llm_json

import agent

agent.run("transcript_sample.txt", auto_approve=True)
