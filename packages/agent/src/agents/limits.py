"""Cost limits for a single incident run.

Deliberately dependency-free (stdlib only) so the offline smoke gate can
exercise it without pulling ADK or any GCP SDK.
"""
import os

# ADK's default is 500 LLM calls per run, which leaves a single incident with no
# practical cost bound: a tool that keeps failing, or a prompt-injected loop,
# bills until the request times out.
#
# Measured behaviour is 5.27 tool calls on average and 7 at the worst
# (n=153, docs/eval/real-report-2026-07-10.md), and a ReAct turn is one LLM call
# per tool call plus the final answer. 12 leaves ~1.5x headroom over the observed
# maximum while capping the worst case at ~1/40 of the ADK default.
DEFAULT_MAX_LLM_CALLS = 12

_ENV_VAR = "AUTOSRE_MAX_LLM_CALLS"


def max_llm_calls() -> int:
    """Per-incident LLM call ceiling. Read at call time so deploys and the eval
    harness can override it via env without reimporting.

    Falls back to the default on anything unusable — a malformed or zero value
    must never silently turn the cost guard off (ADK treats <=0 as "no limit").
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MAX_LLM_CALLS
    try:
        limit = int(raw)
    except ValueError:
        return DEFAULT_MAX_LLM_CALLS
    return limit if limit > 0 else DEFAULT_MAX_LLM_CALLS
