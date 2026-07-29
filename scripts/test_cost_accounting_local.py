"""Offline smoke test for the agent's self-metering (agents.cost).

Runs without ADK, FastAPI, GCP credentials or network. Covers three things:

  1. The arithmetic: token accumulation across calls, thinking tokens billed at
     the output rate, and longest-prefix price lookup so a "-lite" model can
     never be priced as the more expensive family it prefixes.

  2. The honesty flags. `priced` is False for an unknown model (report tokens,
     no dollars - a dollar figure from the wrong price row is worse than none),
     and `accounted` is False when our sum of the parts disagrees with the
     model's own reported total, which is the signature of double counting.

  3. Structural: both agent run loops must call cost.observe(), and the two
     server terminal frames must carry the cost forward. Checked by parsing the
     AST rather than importing, so this stays stdlib-only.

     Guard 3 exists because the failure mode is silent: if observe() is dropped
     from one loop, every number still renders - as zero - and a zero-cost
     claim on stage is worse than having shipped no meter at all.

Usage:
    python scripts/test_cost_accounting_local.py
"""
import ast
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

from agents.cost import PRICING_ASOF, RunCost, price_for  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


class FakeUsage:
    """Stands in for genai's GenerateContentResponseUsageMetadata."""

    def __init__(self, prompt=0, candidates=0, thoughts=0, total=None):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.total_token_count = (
            total if total is not None else prompt + candidates + thoughts
        )


class FakeEvent:
    def __init__(self, usage=None, partial=False):
        self.usage_metadata = usage
        self.partial = partial


def _src(*parts: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", *parts)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_arithmetic() -> None:
    c = RunCost("gemini-2.5-flash")
    c.observe(FakeEvent(FakeUsage(prompt=2000, candidates=200)))
    c.observe(FakeEvent(FakeUsage(prompt=2500, candidates=300)))
    s = c.snapshot()
    check("two calls accumulate", s["llm_calls"] == 2, str(s["llm_calls"]))
    check("input tokens sum", s["input_tokens"] == 4500, str(s["input_tokens"]))
    check("output tokens sum", s["output_tokens"] == 500, str(s["output_tokens"]))

    # 4500 in at $0.30/M + 500 out at $2.50/M = 0.00135 + 0.00125
    check("usd derived from list price", abs(s["usd"] - 0.0026) < 1e-9, str(s["usd"]))
    check("priced flag set", s["priced"] is True)
    check("pricing as-of surfaced", s["pricing_asof"] == PRICING_ASOF)
    check("usd labelled as derived", "not a billed amount" in s["usd_basis"])

    # Thinking tokens bill at the output rate, so they must land in output.
    t = RunCost("gemini-2.5-flash")
    t.observe(FakeEvent(FakeUsage(prompt=1000, candidates=100, thoughts=400)))
    ts = t.snapshot()
    check("thoughts fold into output", ts["output_tokens"] == 500, str(ts["output_tokens"]))
    check("thoughts also reported separately", ts["thought_tokens"] == 400)


def test_events_without_usage_are_free() -> None:
    c = RunCost("gemini-2.5-flash")
    c.observe(FakeEvent(None))
    c.observe(FakeEvent(FakeUsage()))  # present but all-zero: not an LLM call
    s = c.snapshot()
    check("no usage -> no call counted", s["llm_calls"] == 0, str(s["llm_calls"]))
    check("no usage -> zero usd", s["usd"] == 0.0, str(s["usd"]))


def test_partial_events_do_not_double_count() -> None:
    """Streaming partials repeat cumulative usage; counting them inflates."""
    c = RunCost("gemini-2.5-flash")
    c.observe(FakeEvent(FakeUsage(prompt=1000, candidates=50), partial=True))
    c.observe(FakeEvent(FakeUsage(prompt=1000, candidates=90), partial=True))
    c.observe(FakeEvent(FakeUsage(prompt=1000, candidates=120)))
    s = c.snapshot()
    check("partials skipped", s["llm_calls"] == 1, str(s["llm_calls"]))
    check("partials do not inflate input", s["input_tokens"] == 1000, str(s["input_tokens"]))


def test_never_raises() -> None:
    c = RunCost("gemini-2.5-flash")
    for junk in (None, object(), "nonsense", 42):
        c.observe(junk)

    class Hostile:
        @property
        def usage_metadata(self):
            raise RuntimeError("exploding SDK object")

    c.observe(Hostile())
    check("garbage input is survivable", c.snapshot()["llm_calls"] == 0)

    # Junk counters must not poison the totals either.
    j = RunCost("gemini-2.5-flash")
    j.observe(FakeEvent(FakeUsage(prompt="banana", candidates=-5, thoughts=None, total=7)))
    js = j.snapshot()
    check("junk counters coerce to 0", js["input_tokens"] == 0 and js["output_tokens"] == 0, str(js))


def test_unknown_model_reports_tokens_but_no_dollars() -> None:
    c = RunCost("some-future-model-9")
    c.observe(FakeEvent(FakeUsage(prompt=1000, candidates=100)))
    s = c.snapshot()
    check("unknown model -> priced False", s["priced"] is False)
    check("unknown model -> usd None", s["usd"] is None)
    check("unknown model still reports tokens", s["input_tokens"] == 1000)


def test_price_lookup_is_longest_prefix() -> None:
    flash = price_for("gemini-2.5-flash")
    lite = price_for("gemini-2.5-flash-lite")
    pinned = price_for("gemini-2.5-flash-002")
    check("lite is not priced as flash", lite is not None and lite != flash, f"{lite} vs {flash}")
    check("pinned flash id prices as flash", pinned == flash, f"{pinned} vs {flash}")
    check("empty model is unpriced", price_for("") is None)


def test_accounted_flag_catches_double_counting() -> None:
    ok = RunCost("gemini-2.5-flash")
    ok.observe(FakeEvent(FakeUsage(prompt=100, candidates=10)))
    check("consistent usage -> accounted True", ok.snapshot()["accounted"] is True)

    # Model says 100 total, but the parts add to 110: something is off.
    bad = RunCost("gemini-2.5-flash")
    bad.observe(FakeEvent(FakeUsage(prompt=100, candidates=10, total=100)))
    check("mismatched total -> accounted False", bad.snapshot()["accounted"] is False)


def test_agent_loops_observe() -> None:
    """Both run loops must meter. A dropped observe() renders as a silent zero."""
    tree = ast.parse(_src("packages", "agent", "src", "agents", "agent.py"))
    for fname in ("run_incident", "run_incident_events"):
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == fname
            ),
            None,
        )
        if fn is None:
            check(f"{fname} exists", False, "function not found")
            continue
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "observe"
        ]
        check(f"{fname} calls cost.observe", len(calls) == 1, f"found {len(calls)}")

        snaps = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "snapshot"
        ]
        check(f"{fname} emits a snapshot", len(snaps) >= 1, f"found {len(snaps)}")


def test_server_terminal_frames_carry_cost() -> None:
    """The SSE and Pub/Sub terminal frames are the only way the number escapes."""
    src = _src("packages", "agent", "src", "agents", "server.py")
    check("server forwards cost on final frames", src.count('"cost": run_cost') == 2,
          f'found {src.count(chr(34) + "cost" + chr(34) + ": run_cost")}')
    check("server logs run cost for both sources",
          '_log_run_cost(run_cost, "console")' in src and '_log_run_cost(run_cost, "pubsub")' in src)


def test_console_renders_after_the_gate() -> None:
    """Order matters: a throw here must not swallow the approval button."""
    html = _src("packages", "agent", "src", "agents", "static", "index.html")
    approve = html.find('document.getElementById("approveWrap").classList.remove("hidden")')
    render = html.find("renderRunCost(ev.cost)")
    check("console renders the cost line", render > 0)
    check("cost renders after the approval gate", 0 < approve < render, f"{approve} vs {render}")
    check("cost render is guarded", "try{ renderRunCost(ev.cost); }catch" in html)


def main() -> int:
    test_arithmetic()
    test_events_without_usage_are_free()
    test_partial_events_do_not_double_count()
    test_never_raises()
    test_unknown_model_reports_tokens_but_no_dollars()
    test_price_lookup_is_longest_prefix()
    test_accounted_flag_catches_double_counting()
    test_agent_loops_observe()
    test_server_terminal_frames_carry_cost()
    test_console_renders_after_the_gate()

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
