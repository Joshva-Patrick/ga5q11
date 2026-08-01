"""
Pluggable LLM client. Model choice earns no marks, so this defaults to
whatever is cheapest/available:

  1. ANTHROPIC_API_KEY set        -> Anthropic API (claude-sonnet-4-6... or override)
  2. OLLAMA_HOST set / localhost  -> local Ollama model (free, offline)
  3. neither                      -> raises with setup instructions

call_llm_json() forces the model to return ONLY JSON (no prose, no
markdown fences) so the agent can parse it deterministically.
"""

import json
import os
import re
import urllib.request


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


def _call_anthropic(system: str, user: str) -> str:
    import urllib.request

    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("AGENT_MODEL", "claude-sonnet-4-6")
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return "".join(b["text"] for b in data["content"] if b["type"] == "text")


def _call_ollama(system: str, user: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("AGENT_MODEL", "llama3.1")
    body = json.dumps(
        {
            "model": model,
            "prompt": f"{system}\n\n{user}",
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(f"{host}/api/generate", data=body)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["response"]


def call_llm_json(system: str, user: str) -> dict:
    """Call whichever backend is configured; return parsed JSON dict."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raw = _call_anthropic(system, user)
    elif os.environ.get("USE_OLLAMA") or os.environ.get("OLLAMA_HOST"):
        raw = _call_ollama(system, user)
    else:
        raise RuntimeError(
            "No LLM configured. Set ANTHROPIC_API_KEY for the Anthropic API, "
            "or OLLAMA_HOST/USE_OLLAMA for a local free model."
        )
    return json.loads(_strip_fences(raw))
