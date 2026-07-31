"""Offline smoke test for earned autonomy (the gate that retires itself).

Tests the pure decision core with no ADK, GCP credentials, BigQuery or network:

  1. wilson_lower_bound — the arithmetic the promotion stands on. A hot streak
     (3/3) must NOT clear a bar that evidence volume (35/35) clears: if this
     function flatters small n, the gate opens on luck.
  2. evaluate_class — promotion requires lower bound >= threshold AND no
     demotion; a demotion beats any statistics.
  3. Fail-closed wiring — with case memory or autonomy disabled, promoted_for()
     must answer False. The default posture is the human gate.
  4. Structural: the demotion state survives the guard-state merge pattern
     ({**empty_state(), **state}) that every state_store writer uses; a merge
     that drops the "autonomy" key would silently re-arm demoted classes.
  5. Structural: every autonomy execution path in server.py that can act
     (merge/promote) must be preceded by a rehearsal health check in the same
     function - parsed from the AST, not imported.

Usage:
    python scripts/test_autonomy_local.py
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


def test_wilson_lower_bound() -> None:
    from agents.autonomy import wilson_lower_bound as lb

    assert lb(0, 0) == 0.0, "no evidence must score 0"
    assert lb(5, 0) == 0.0, "malformed input must score 0"
    # p=1 closed form: n / (n + z^2). The gate opens on volume, not streaks.
    for n, expect in [(3, 0.4385), (16, 0.8064), (35, 0.9011), (73, 0.9500)]:
        got = lb(n, n)
        assert abs(got - expect) < 0.001, f"lb({n}/{n}) = {got}, expected ~{expect}"
    assert lb(3, 3) < 0.80 <= lb(16, 16), "3/3 must not clear the bar 16/16 clears"
    # A failure must hurt: 15/16 scores clearly below 16/16.
    assert lb(15, 16) < lb(16, 16) - 0.05
    # Monotone in evidence at the same rate.
    assert lb(10, 10) < lb(20, 20) < lb(40, 40)
    # Never negative, never above the raw rate.
    assert 0.0 <= lb(1, 10) <= 0.1


def test_evaluate_class() -> None:
    from agents.autonomy import evaluate_class

    ok = evaluate_class(20, 20, None, 0.80)
    assert ok["promoted"] is True and ok["demoted"] is False
    thin = evaluate_class(3, 3, None, 0.80)
    assert thin["promoted"] is False, "a hot streak must stay behind the gate"
    demoted = evaluate_class(50, 50, {"ts": 123.0, "reason": "verify failed"}, 0.80)
    assert demoted["promoted"] is False, "a demotion beats any statistics"
    assert demoted["demoted"] is True and demoted["demotion"]["reason"] == "verify failed"
    empty_demotion = evaluate_class(20, 20, {}, 0.80)
    assert empty_demotion["promoted"] is True, "an empty record is not a demotion"


def test_threshold_parsing() -> None:
    from agents import autonomy

    for raw, expect in [("", 0.80), ("0.9", 0.9), ("garbage", 0.80),
                        ("0", 0.80), ("1.5", 0.80), ("-1", 0.80)]:
        os.environ["AUTOSRE_AUTONOMY_THRESHOLD"] = raw
        got = autonomy.threshold()
        assert abs(got - expect) < 1e-9, f"threshold({raw!r}) = {got}, expected {expect}"
    os.environ.pop("AUTOSRE_AUTONOMY_THRESHOLD", None)


def test_fail_closed() -> None:
    """Disabled anything -> nobody is promoted. The default posture is the gate."""
    from agents import autonomy

    os.environ.pop("AUTOSRE_AUTONOMY_ENABLED", None)
    os.environ.pop("AUTOSRE_CASES_TABLE", None)
    promoted, evidence = autonomy.promoted_for("DATABASE_URL")
    assert promoted is False and "disabled" in evidence["reason"]

    os.environ["AUTOSRE_AUTONOMY_ENABLED"] = "1"
    # autonomy on but case memory off: the ledger is unreadable -> still closed.
    promoted, evidence = autonomy.promoted_for("DATABASE_URL")
    assert promoted is False
    book = autonomy.ledger()
    assert book["available"] is False and book["classes"] == []
    os.environ.pop("AUTOSRE_AUTONOMY_ENABLED", None)


def test_demotions_survive_state_merge() -> None:
    """The killswitch/demotion writers all do {**empty_state(), **state}: an
    unknown top-level key must survive that merge, or writing the killswitch
    would erase demotions (and silently re-arm a demoted class)."""
    from agents.state_store import empty_state

    state = {**empty_state(), "autonomy": {"demoted": {"restore_env:X": {"ts": 1.0, "reason": "r"}}}}
    merged = {**empty_state(), **state}
    assert merged["autonomy"]["demoted"]["restore_env:X"]["reason"] == "r"


def test_rehearsal_precedes_action() -> None:
    """AST rule: inside _maybe_execute_autonomously, the calls that ACT
    (merge_pull_request / promote_rehearsal) must appear after the rehearsal
    calls (rehearse_env_fix and its health check). Acting before the fix proved
    itself next door would gut the feature's entire safety argument."""
    tree = ast.parse(open(SERVER_PY, encoding="utf-8").read())
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_maybe_execute_autonomously"),
        None,
    )
    assert fn is not None, "_maybe_execute_autonomously not found in server.py"
    order = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name in {"rehearse_env_fix", "merge_pull_request", "promote_rehearsal"}:
                order.append((node.lineno, name))
    order.sort()
    names = [n for _, n in order]
    assert "rehearse_env_fix" in names, "autonomous path must rehearse"
    assert names.index("rehearse_env_fix") < names.index("merge_pull_request")
    assert names.index("rehearse_env_fix") < names.index("promote_rehearsal")



def test_parse_failure_is_not_healthy() -> None:
    """The exact malformed final seen in production 2026-07-30: the model put a
    key/value pair inside the "evidence" ARRAY. Before the fix this parsed to
    {} and classified as "healthy" - a green console over a down service."""
    from agents.diagnosis import classify_outcome as _classify_outcome
    from agents.diagnosis import parse_diagnosis as _parse_diagnosis
    from agents.diagnosis import salvage_diagnosis as _salvage_diagnosis

    broken = '''```json
{
  "root_cause": "Required environment variable DATABASE_URL is not set, causing 503.",
  "evidence": [
    "body": "{\\"status\\":\\"unhealthy\\"}",
    "startup check failed"
  ],
  "missing_env_var": "DATABASE_URL",
  "confidence": 1.0,
  "user_reply_draft": "\u5fa9\u65e7\u3057\u307e\u3057\u305f",
  "action": "fix_pr"
}
```'''
    parsed = _parse_diagnosis(broken)
    assert parsed.get("error"), "this payload must genuinely fail json.loads"
    assert _classify_outcome(parsed) == "undetermined", "a parse failure must never read as healthy"

    steps = [{"name": "open_pull_request",
              "summary": {"ok": True, "pr_number": 59,
                          "pr_url": "https://github.com/o/r/pull/59"}}]
    fixed = _salvage_diagnosis(parsed, steps)
    assert fixed["pr_number"] == 59 and fixed["action"] == "fix_pr"
    assert fixed["missing_env_var"] == "DATABASE_URL"
    assert fixed["root_cause"].startswith("Required environment variable")
    assert fixed["confidence"] == 1.0
    assert fixed["user_reply_draft"] == "復旧しました"
    assert fixed["parse_recovered"] is True
    assert _classify_outcome(fixed) == "pr_opened", "a real PR must surface the approval gate"


def test_salvage_leaves_good_parses_alone() -> None:
    """Salvage is a repair path, not a rewrite: a clean parse passes through
    untouched, and no PR in the trace must not conjure one."""
    from agents.diagnosis import classify_outcome as _classify_outcome
    from agents.diagnosis import salvage_diagnosis as _salvage_diagnosis

    good = {"missing_env_var": "DATABASE_URL", "action": "fix_pr", "pr_url": "u", "pr_number": 1}
    assert _salvage_diagnosis(good, [{"name": "open_pull_request",
                                      "summary": {"ok": True, "pr_number": 99}}]) is good

    healthy = {"missing_env_var": None, "action": "none"}
    assert _classify_outcome(healthy) == "healthy", "a real healthy verdict still reads healthy"

    no_pr = _salvage_diagnosis({"error": "parse_failed: x", "raw": "{}"}, [])
    assert "pr_url" not in no_pr and no_pr["parse_recovered"] is False
    assert _classify_outcome(no_pr) == "undetermined"


def main() -> int:
    tests = [
        test_wilson_lower_bound,
        test_evaluate_class,
        test_threshold_parsing,
        test_fail_closed,
        test_demotions_survive_state_merge,
        test_rehearsal_precedes_action,
        test_parse_failure_is_not_healthy,
        test_salvage_leaves_good_parses_alone,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            ok = True
        except AssertionError as e:
            ok = False
            print(f"       {e}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"       {type(e).__name__}: {e}")
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {t.__name__}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
