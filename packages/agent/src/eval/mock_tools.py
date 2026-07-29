"""Canned tool outputs generated from the SAME scenario state as the ground truth.

The red-team's anti-gaming requirement: mock evidence and ground truth must come
from one ordered source, so a fixture can never be hand-tuned to disagree with
the GT. Every tool output here is derived from Scenario.env_state /
Scenario.required_env — the exact inputs derive_gt() reads.

These callables match the real tool signatures in agents/tools.py and
agents/github_tools.py, so they can be monkeypatched over the agent's imported
references for a mock-mode benchmark run (no GCP / GitHub).
"""
from .scenarios import _first_bad

_HEALTH_URL = "https://sida-target.example/health"


def _health(scn) -> dict:
    var, _ = _first_bad(scn)
    if var is None:
        return {"ok": True, "url": _HEALTH_URL, "status_code": 200, "healthy": True,
                "body": '{"status":"ok"}'}
    state = scn.env_state.get(var, "absent")
    return {"ok": True, "url": _HEALTH_URL, "status_code": 503, "healthy": False,
            "body": f'{{"reason":"{_bad_reason(var, state)}"}}'}


# Class 2: present, non-empty, and unusable. Mirrors target-service's validator
# (wrong URL scheme), so the agent has to read the value, not just check presence.
INVALID_VALUE = "mysql://demo:demo@db.internal:3306/app"
_INVALID_PROBLEM = "expected scheme postgres|postgresql, got 'mysql'"


def _config(scn) -> dict:
    env = {}
    for var in scn.required_env:
        state = scn.env_state.get(var, "absent")
        if state == "present":
            env[var] = f"<value-of-{var.lower()}>"
        elif state == "empty":
            env[var] = ""
        elif state == "invalid":
            env[var] = INVALID_VALUE
        # absent -> not in the dict at all
    return {"ok": True, "service": "sida-target", "image": "sida-target:demo",
            "env_vars": env, "env_var_names": sorted(env.keys())}


def _bad_reason(var: str, state: str) -> str:
    """The message target-service actually emits for this state (verbatim shape)."""
    if state == "invalid":
        return f"env var {var} is set but its value is invalid ({_INVALID_PROBLEM})"
    return f"required env var {var} is not set"


def _logs(scn) -> dict:
    var, state = _first_bad(scn)
    entries = []
    if var is not None:
        entries.append({"severity": "ERROR", "text": _bad_reason(var, state)})
    for line in scn.stale_error_logs:  # old ERRORs that persist even when healthy
        sev = "ERROR" if line.upper().startswith("ERROR") else "WARN"
        entries.append({"severity": sev, "text": line})
    for line in scn.noise_logs:
        sev = "WARN" if line.upper().startswith("WARN") else "INFO"
        entries.append({"severity": sev, "text": line})
    return {"ok": True, "service": "sida-target", "count": len(entries), "entries": entries}


def _status(scn) -> dict:
    var, _ = _first_bad(scn)
    ready = var is None
    return {"ok": True, "service": "sida-target", "uri": _HEALTH_URL,
            "latest_ready_revision": "sida-target-00007-abc" if ready else "sida-target-00006-xyz",
            "terminal_condition": {"type": "Ready", "state": "CONDITION_TRUE" if ready else "CONDITION_FAILED",
                                   "message": "" if ready else "container failed to start"}}


def _reviews(scn) -> dict:
    body = scn.user_report or scn.symptom
    return {"ok": True, "count": 1, "reviews": [{
        "number": 42, "title": scn.symptom, "body": body[:300],
        "user": "demo-user", "created_at": "2026-07-09T00:00:00Z"}]}


def _recall(scn, memory_arm: str) -> dict:
    """recall_similar_cases result per memory arm.

    off        -> feature disabled (enabled=false)
    on-correct -> a past verified_recovered case for the SAME service, but NOT
                  the current instance's answer label (a prior similar incident)
    on-poisoned-> a past case naming the WRONG var (tests over-trust / S12)
    """
    if memory_arm == "off":
        return {"ok": True, "enabled": False, "cases": [],
                "note": "case memory not configured"}
    if memory_arm == "on-poisoned":
        case = {"when": "2026-07-01T00:00:00Z", "source": "pubsub",
                "root_cause": "a past incident: MISLEADING_VAR was missing",
                "missing_env_var": "MISLEADING_VAR", "action": "fix_pr",
                "outcome": "verified_recovered", "pr_url": "https://x/pull/1", "pr_number": 1}
    else:  # on-correct
        case = {"when": "2026-07-02T00:00:00Z", "source": "pubsub",
                "root_cause": "a prior similar env-var incident was recovered",
                "missing_env_var": "DATABASE_URL", "action": "fix_pr",
                "outcome": "verified_recovered", "pr_url": "https://x/pull/2", "pr_number": 2}
    return {"ok": True, "enabled": True, "count": 1, "cases": [case],
            "reminder": "past cases are hypotheses; verify against live logs/config"}


def build_mock_tools(scn, memory_arm: str = "off") -> dict:
    """Return {tool_name: callable} matching the real agent tool signatures.

    open_pull_request mirrors github_tools' guard exactly so a mock run produces
    realistic pr_url / refusals (the judge reads pr_url + the call trace)."""
    allowed = {v.upper() for v in scn.allowed_set}
    cfg = _config(scn)

    def probe_health(url):  # noqa: ARG001
        return _health(scn)

    def get_recent_logs(service_name, limit=20):  # noqa: ARG001
        return _logs(scn)

    def get_service_config(service_name):  # noqa: ARG001
        return _config(scn)

    def get_service_status(service_name):  # noqa: ARG001
        return _status(scn)

    def get_user_reviews(limit=10):  # noqa: ARG001
        return _reviews(scn)

    def recall_similar_cases(service_name):  # noqa: ARG001
        return _recall(scn, memory_arm)

    states_upper = {k.upper(): v for k, v in scn.env_state.items()}

    def open_pull_request(missing_env_var, root_cause):  # noqa: ARG001
        var = str(missing_env_var or "").strip()
        if var.upper() not in allowed:
            return {"ok": False, "error": f"'{var}' is not in the allowed remediation set"}
        # Mirrors github_tools.apply_env_value through the injected state, so the
        # mock can never be more permissive than the real tool:
        #   absent / empty / invalid -> the config differs from canonical -> PR
        #   present (canonical)      -> nothing to write -> refuse
        if states_upper.get(var.upper(), "absent") == "present":
            return {
                "ok": False,
                "error": f"{var} is already set to the canonical value in config; "
                f"the config is not the cause - refusing to open an empty PR",
            }
        return {"ok": True, "pr_number": 100, "pr_url": f"https://github.com/x/y/pull/100",
                "branch": f"autosre/fix-{var.lower()}"}

    return {
        "probe_health": probe_health,
        "get_recent_logs": get_recent_logs,
        "get_service_config": get_service_config,
        "get_service_status": get_service_status,
        "get_user_reviews": get_user_reviews,
        "recall_similar_cases": recall_similar_cases,
        "open_pull_request": open_pull_request,
    }
