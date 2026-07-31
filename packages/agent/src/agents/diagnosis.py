"""Reading the agent's final answer: parse, salvage, classify.

Pure functions over text and the tool trace. Deliberately stdlib-only and free
of any FastAPI/ADK import, like agents.limits and agents.cost, so the offline
smoke gate can drive them without the SDK or a running app.

The salvage path exists because of a real production failure (2026-07-30): the
model emitted a key/value pair inside the "evidence" ARRAY, json.loads rejected
the whole document, every field was lost, and the run - which had already
opened a correct fix PR - was classified "healthy". A green console over a
service returning 503, with no approval button to merge the fix that existed.

Two rules came out of that:
  1. A parse failure is not evidence of health. It is "undetermined".
  2. What the tools DID outranks what the model SAID about it. A pr_number
     returned by open_pull_request is a fact; the surrounding prose is a claim.
"""
import json
import re

# Top-level scalars in the diagnosis contract. Recovering them one field at a
# time cannot invent structure the way a whole-document "JSON repair" can.
SALVAGE_STR_FIELDS = (
    "root_cause",
    "missing_env_var",
    "action",
    "proposed_fix",
    "user_reply_draft",
    "user_reports_summary",
)


def parse_diagnosis(text: str) -> dict:
    """Tolerantly extract the agent's final JSON diagnosis (models sometimes wrap it in code fences)."""
    if not text:
        return {"error": "empty_final"}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?", "", t).strip()
        t = re.sub(r"```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except Exception as e:  # noqa: BLE001
        # Keep enough raw text for salvage_diagnosis to work on: the payload
        # carries an evidence list and a reply draft, so a 500-char cap stopped
        # short of the fields that matter.
        return {"error": f"parse_failed: {e}", "raw": text[:4000]}


def salvage_diagnosis(parsed: dict, steps: list | None) -> dict:
    """Rebuild a usable diagnosis when the model emitted invalid JSON.

    Sources, in order of authority: the tool trace (what actually happened),
    then per-field regex over the raw text (for the human-readable fields).
    A clean parse is returned untouched - this is a repair path, not a rewrite.
    """
    if not parsed.get("error"):
        return parsed
    raw = parsed.get("raw") or ""
    out: dict = {"parse_error": parsed.get("error"), "parse_recovered": False}
    for field in SALVAGE_STR_FIELDS:
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            try:  # re-wrap in quotes so json handles the escapes, not us
                out[field] = json.loads(f'"{m.group(1)}"')
            except ValueError:
                continue
    m = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', raw)
    if m:
        try:
            out["confidence"] = float(m.group(1))
        except ValueError:
            pass
    for s in steps or []:
        summary = s.get("summary") or {}
        if s.get("name") == "open_pull_request" and summary.get("ok"):
            out["pr_number"] = summary.get("pr_number")
            out["pr_url"] = summary.get("pr_url")
            out["action"] = "fix_pr"  # the PR exists; the prose does not decide
    out["parse_recovered"] = bool(out.get("pr_url") or out.get("missing_env_var"))
    return out


def classify_outcome(d: dict) -> str:
    """Classify a diagnosis into a single outcome label (frozen cross-worker contract)."""
    if d.get("pr_url"):
        return "pr_opened"
    if d.get("action") == "escalate" or d.get("escalation"):
        return "escalated"
    # Without this branch an unparseable final fell through to "healthy" purely
    # because missing_env_var was absent.
    if d.get("error") or d.get("parse_error"):
        return "undetermined"
    if d.get("missing_env_var") is None:
        return "healthy"
    return "none"
