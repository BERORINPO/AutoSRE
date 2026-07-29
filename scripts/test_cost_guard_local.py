"""Offline smoke test for the per-incident cost guards.

Two independent guards, both regression-tested here without ADK, FastAPI, GCP
credentials or network:

  1. agents.limits.max_llm_calls() — the LLM call ceiling and its env override.
     A malformed or <=0 value must fall back to the default, never disable the
     cap (ADK treats max_llm_calls<=0 as "no limit").

  2. Structural: every HTTP route that starts an agent run must take a `request`
     parameter and call _check_console_key(request). This is checked by parsing
     server.py's AST rather than by importing it, so it stays stdlib-only.

     Guard 2 exists because /incident/stream shipped without it: the handler was
     declared `async def incident_stream()` with no `request` parameter, so the
     key check was structurally impossible to call. Every other mutating route
     had the gate. A per-route unit test would not have caught the missing one —
     only an "all routes of this class" rule does.

Usage:
    python scripts/test_cost_guard_local.py
"""
import ast
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

SERVER_PY = os.path.join(
    os.path.dirname(__file__), "..", "packages", "agent", "src", "agents", "server.py"
)
AGENT_PY = os.path.join(
    os.path.dirname(__file__), "..", "packages", "agent", "src", "agents", "agent.py"
)

# Functions that actually start a billable ReAct loop.
RUN_ENTRYPOINTS = {"run_incident", "run_incident_events"}
GATE = "_check_console_key"

# Routes that legitimately start a run without the console key. /pubsub/incident
# is authenticated by OIDC verification instead (AUTOSRE_PUBSUB_AUDIENCE) and is
# rate-limited by the auto-trigger cooldown.
GATE_EXEMPT_ROUTES = {"/pubsub/incident"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


# --------------------------------------------------------------- guard 1: cap
def _reload_limits():
    for mod in [m for m in sys.modules if m == "agents.limits"]:
        del sys.modules[mod]
    from agents.limits import DEFAULT_MAX_LLM_CALLS, max_llm_calls

    return max_llm_calls, DEFAULT_MAX_LLM_CALLS


def test_limits() -> None:
    max_llm_calls, default = _reload_limits()
    saved = os.environ.get("AUTOSRE_MAX_LLM_CALLS")
    try:
        cases = [
            (None, default, "unset -> default"),
            ("", default, "empty -> default"),
            ("  ", default, "whitespace -> default"),
            ("25", 25, "valid override"),
            (" 25 ", 25, "override is stripped"),
            ("abc", default, "malformed -> default (not a crash)"),
            ("0", default, "zero must NOT disable the cap"),
            ("-1", default, "negative must NOT disable the cap"),
        ]
        for raw, expected, label in cases:
            if raw is None:
                os.environ.pop("AUTOSRE_MAX_LLM_CALLS", None)
            else:
                os.environ["AUTOSRE_MAX_LLM_CALLS"] = raw
            got = max_llm_calls()
            check(f"max_llm_calls: {label}", got == expected, f"expected {expected}, got {got}")

        # Read at call time, not import time: the eval harness mutates env per run.
        os.environ["AUTOSRE_MAX_LLM_CALLS"] = "7"
        first = max_llm_calls()
        os.environ["AUTOSRE_MAX_LLM_CALLS"] = "9"
        second = max_llm_calls()
        check(
            "max_llm_calls: re-read at call time",
            (first, second) == (7, 9),
            f"expected (7, 9), got ({first}, {second})",
        )
    finally:
        if saved is None:
            os.environ.pop("AUTOSRE_MAX_LLM_CALLS", None)
        else:
            os.environ["AUTOSRE_MAX_LLM_CALLS"] = saved

    default_ok = 0 < default <= 50
    check(
        "DEFAULT_MAX_LLM_CALLS is a sane ceiling",
        default_ok,
        f"expected 0 < default <= 50, got {default}",
    )


# ------------------------------------------------- guard 2: route auth (AST)
def _route_path(decorator: ast.expr) -> str | None:
    """Return the URL path if the decorator is @app.<method>("/path")."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return None
    if func.value.id != "app" or func.attr not in {"get", "post", "put", "delete", "patch"}:
        return None
    if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
        return None
    return decorator.args[0].value


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                names.add(alias.asname or alias.name)
    return names


def test_route_gates() -> None:
    with open(SERVER_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    run_routes: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            path = _route_path(dec)
            if path is None:
                continue
            if _called_names(node) & RUN_ENTRYPOINTS:
                run_routes.append((path, node))

    check(
        "found the agent-starting routes",
        len(run_routes) >= 3,
        f"expected >=3 routes calling {sorted(RUN_ENTRYPOINTS)}, found "
        f"{sorted(p for p, _ in run_routes)}",
    )

    for path, node in run_routes:
        if path in GATE_EXEMPT_ROUTES:
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        has_request = "request" in params
        calls_gate = GATE in _called_names(node)
        check(
            f"{path}: takes a request parameter",
            has_request,
            f"params={sorted(params)} - without it {GATE}() cannot be called",
        )
        check(f"{path}: calls {GATE}()", calls_gate, f"calls={sorted(_called_names(node))}")

    paths = {p for p, _ in run_routes}
    check(
        "/incident/stream is among the checked routes",
        "/incident/stream" in paths,
        f"routes found: {sorted(paths)}",
    )


# --------------------------------------------- guard 2b: run_config is passed
def test_run_config_wired() -> None:
    with open(AGENT_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    run_async_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_async"
    ]
    check(
        "both run_async call sites found",
        len(run_async_calls) == 2,
        f"expected 2, found {len(run_async_calls)}",
    )
    for i, call in enumerate(run_async_calls):
        kwargs = {kw.arg for kw in call.keywords}
        check(
            f"run_async #{i + 1} passes run_config",
            "run_config" in kwargs,
            f"kwargs={sorted(k for k in kwargs if k)} - without it ADK defaults to 500 LLM calls",
        )


def main() -> int:
    test_limits()
    test_route_gates()
    test_run_config_wired()

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f" - {detail}"
        print(line)
    print(f"\n{passed}/{len(results)} pass")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
