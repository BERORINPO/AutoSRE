"""Offline smoke test for `autosre watch` (no ADK, no GCP, no network, no tty).

A terminal UI is the one component nobody notices is broken until the moment
they need it - which, for this screen, is during an incident. So the gate drives
the parts that can be driven without a terminal:

  1. Import purity. The screen must not drag in ADK / google-cloud-* / httpx at
     import time, for the same reason `autosre doctor` must not: the operator
     reaching for `watch` on a half-installed machine should see a panel saying
     what is missing, not an ImportError.
  2. The renderer never raises and never lies. Empty, degraded and hostile
     snapshots (None values, wrong types, a source that raised) must all draw a
     frame - and "off", "unreadable" and "empty" must stay three distinct
     sentences. A disabled ledger rendering as "nothing earned yet" is the same
     class of bug as a green console over a 503.
  3. Frame discipline. Every line fits the width and is ASCII - a redraw loop
     that throws UnicodeEncodeError in cmd.exe kills the window an operator was
     watching, and a line one column too long corrupts the whole frame.
  4. The promotion arithmetic. `runs_to_promote` is what turns the ledger from
     a status into a plan, and it must agree with the Wilson bound the gate
     actually uses (agents.autonomy), not with a second implementation.
  5. One dead source degrades one panel. build_snapshot isolates collectors,
     including the SystemExit the CLI raises for a missing console key.

Usage:
    python scripts/test_tui_local.py
"""
import datetime
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

# Fixed clock, two hours after the newest PR in the fixture - so "opened 2h ago"
# is an assertion about the formatter, not about when the suite happened to run.
NOW = datetime.datetime.fromisoformat("2026-08-31T11:00:00+00:00").timestamp()

FULL_SNAPSHOT = {
    "generated_at": NOW,
    "sections": {
        "target_health": {"ok": True, "status_code": 503, "healthy": False},
        "pull_requests": {
            "ok": True,
            "count": 2,
            "pull_requests": [
                {"number": 43, "title": "fix: restore DATABASE_URL to recover sida-target",
                 "url": "https://github.com/o/r/pull/43",
                 "created_at": "2026-08-31T09:00:00Z", "branch": "autosre/fix-database_url"},
                {"number": 44, "title": "fix: correct API_KEY", "url": "https://github.com/o/r/pull/44",
                 "created_at": "2026-08-30T09:00:00Z", "branch": "autosre/fix-api_key"},
            ],
        },
        "ledger": {
            "enabled": True,
            "available": True,
            "threshold": 0.8,
            "classes": [
                {"class": "restore_env:DATABASE_URL", "env_var": "DATABASE_URL",
                 "successes": 20, "attempts": 20, "wilson_lower_bound": 0.8389,
                 "threshold": 0.8, "demoted": False, "demotion": None, "promoted": True},
                {"class": "restore_env:API_KEY", "env_var": "API_KEY",
                 "successes": 3, "attempts": 3, "wilson_lower_bound": 0.4385,
                 "threshold": 0.8, "demoted": False, "demotion": None, "promoted": False},
            ],
        },
        "cases": {
            "ok": True, "enabled": True, "count": 1,
            "cases": [{"when": "2026-08-31T08:00:00Z", "missing_env_var": "DATABASE_URL",
                       "pr_number": 43, "confidence": 0.9, "outcome": "verified_recovered",
                       "root_cause": "DATABASE_URL missing from the deployed revision"}],
        },
        "guard": {"enabled": True, "available": True, "runs_today": 3, "daily_limit": 25,
                  "day": "2026-08-31", "last_run_ts": 1_756_599_000.0,
                  "killswitch": {"tripped": False, "reason": "", "ts": 0.0}},
    },
}


def _frame(snapshot, width=100, height=44):
    from agents import tui

    return tui.render(snapshot, {"service": "sida-target", "interval": 10.0}, width, height)


def test_import_is_stdlib_only() -> None:
    for mod in list(sys.modules):
        if mod.split(".")[0] in ("google", "httpx", "fastapi"):
            del sys.modules[mod]
    import agents.tui  # noqa: F401

    heavy = sorted(
        m for m in sys.modules
        if m in ("httpx", "fastapi") or m.startswith(("google.adk", "google.cloud"))
    )
    assert not heavy, f"agents.tui imported heavy modules at import time: {heavy}"


def test_frame_lines_fit_and_are_ascii() -> None:
    """A line wider than the terminal corrupts every line below it."""
    for width in (60, 80, 100, 200):
        for line in _frame(FULL_SNAPSHOT, width=width):
            assert len(line) <= width, f"line exceeds width {width}: {line!r}"
            line.encode("ascii")  # raises on anything cmd.exe may refuse


def test_frame_shows_the_queue_and_the_ledger() -> None:
    text = "\n".join(_frame(FULL_SNAPSHOT))
    assert "https://github.com/o/r/pull/43" in text, "the PR link must be openable"
    assert "opened 2h ago" in text, "the queue has to show how long it has been waiting"
    assert "autosre approve 43" in text, "the screen must name the command that acts"
    assert "restore_env:DATABASE_URL" in text and "PROMOTED" in text
    assert "restore_env:API_KEY" in text and "more to promote" in text, (
        "a gated class must say how far it is from the bar, not just that it is gated")
    assert "threshold 0.80" in text
    assert "recovered (verified)" in text, "past outcomes belong on the screen"


def test_columns_stay_aligned() -> None:
    """Padding is computed per column, and the line is width-clipped LAST.

    Regression: an `_ascii` that collapsed whitespace ran after the padding and
    flattened every column in the frame into a single-spaced sentence.
    """
    from agents import tui

    rows = [line for line in tui.ledger_lines(FULL_SNAPSHOT, 110) if " verified  [" in line]
    assert len(rows) == 2
    assert len({row.index("[") for row in rows}) == 1, f"bars are not aligned: {rows}"
    assert len({row.index(" verified") for row in rows}) == 1, (
        f"the evidence column is not aligned: {rows}")
    header = _frame(FULL_SNAPSHOT, width=100)[0]
    assert len(header) == 100 and header.endswith(":00"), (
        "the header's right-hand annotation must reach the margin")


def test_empty_and_degraded_snapshots_still_render() -> None:
    """Every failure mode of every source, on one screen, without an exception."""
    from agents import tui

    hostile = {
        "generated_at": None,
        "sections": {
            "target_health": {"ok": False, "error": "ConnectError: no route"},
            "pull_requests": {"ok": False, "error": "GitHub 401: Bad credentials"},
            "ledger": {"enabled": False, "available": False, "reason": "case memory disabled",
                       "threshold": None, "classes": None},
            "cases": {"error": "RuntimeError: bigquery said no"},
            "guard": {"enabled": False},
        },
    }
    for snapshot in ({}, {"sections": {}}, {"sections": None}, hostile):
        lines = tui.render(snapshot, {}, 80, 40)
        assert lines and all(isinstance(line, str) for line in lines)
    text = "\n".join(tui.render(hostile, {}, 100, 40))
    assert "Bad credentials" in text, "an unreadable queue must say why"
    assert "bigquery said no" in text
    assert "unreadable" in text or "off (" in text


def test_off_is_not_empty() -> None:
    """A disabled source must not render as 'nothing has happened yet'."""
    from agents import tui

    off = {"sections": {"ledger": {"enabled": False, "classes": []}}}
    empty = {"sections": {"ledger": {"enabled": True, "available": True,
                                     "threshold": 0.8, "classes": []}}}
    off_text = "\n".join(tui.ledger_lines(off, 100))
    empty_text = "\n".join(tui.ledger_lines(empty, 100))
    assert "AUTOSRE_CASES_TABLE" in off_text, "off must name the switch that is off"
    assert "no verified recoveries recorded yet" in empty_text
    assert off_text != empty_text


def test_demoted_beats_a_high_score() -> None:
    """One strike locks the class no matter what the arithmetic says."""
    from agents import tui

    snapshot = {"sections": {"ledger": {
        "enabled": True, "available": True, "threshold": 0.8,
        "classes": [{"class": "restore_env:DATABASE_URL", "successes": 99, "attempts": 100,
                     "wilson_lower_bound": 0.9463, "demoted": True, "promoted": False,
                     "demotion": {"ts": 1_756_500_000.0, "reason": "verify failed"}}]}}}
    text = "\n".join(tui.ledger_lines(snapshot, 110))
    assert "DEMOTED" in text and "verify failed" in text
    assert "PROMOTED" not in text


def test_bar_marks_the_threshold_and_clamps() -> None:
    from agents import tui

    assert "|" in tui.bar(0.0, 0.8) and "|" in tui.bar(1.0, 0.8), (
        "a fill that hides the bar defeats the bar")
    assert tui.bar(-5, 0.8).count("#") == 0
    assert len(tui.bar(0.5, 0.8, 14)) == 16  # 14 cells + the brackets
    assert tui.bar(1.0, 0.8, 10).count("#") == 9  # every cell but the marker


def test_runs_to_promote_agrees_with_the_real_gate() -> None:
    from agents import autonomy, tui

    assert tui.runs_to_promote(20, 20, 0.8) == 0, "an already promoted class needs 0 more"
    needed = tui.runs_to_promote(3, 3, 0.8)
    assert needed and needed > 0
    assert autonomy.wilson_lower_bound(3 + needed, 3 + needed) >= 0.8
    assert autonomy.wilson_lower_bound(3 + needed - 1, 3 + needed - 1) < 0.8, (
        "it must be the SMALLEST number of runs that clears the bar")
    assert tui.runs_to_promote(0, 10, 0.8, cap=5) is None, "unreachable must say so"


def test_build_snapshot_isolates_one_dead_source() -> None:
    from agents import tui

    def boom():
        raise RuntimeError("bigquery is down")

    def unauthorized():
        raise SystemExit("the deployed agent requires a console key")

    snapshot = tui.build_snapshot({
        "guard": lambda: {"enabled": True, "runs_today": 1},
        "cases": boom,
        "ledger": unauthorized,
        "pull_requests": lambda: "not a dict",
    })
    assert snapshot["sections"]["guard"]["runs_today"] == 1, "a healthy source is untouched"
    assert "bigquery is down" in snapshot["sections"]["cases"]["error"]
    assert "console key" in snapshot["sections"]["ledger"]["error"], (
        "SystemExit from the CLI's remote path must degrade one panel, not the screen")
    assert snapshot["sections"]["pull_requests"] == {"value": "not a dict"}


def test_short_terminal_keeps_the_queue() -> None:
    """When the screen cannot hold everything, evidence goes - not the queue."""
    lines = _frame(FULL_SNAPSHOT, width=100, height=22)
    text = "\n".join(lines)
    assert len(lines) <= 22 + 2, f"frame overflowed the terminal: {len(lines)} lines"
    assert "awaiting your approval" in text and "pull/43" in text
    assert "trust ledger" in text


def test_keymap_covers_the_documented_keys() -> None:
    """The footer promises r / 1-9 / q; a keymap only a human can test rots."""
    from agents import tui

    assert tui.key_action("q") == ("quit", None)
    assert tui.key_action("\x1b") == ("quit", None), "Esc must leave the alternate screen"
    assert tui.key_action("\x03") == ("quit", None), "Ctrl-C must not be an unknown key"
    for key in ("r", "R", "\r", " ", None):
        assert tui.key_action(key) == ("reload", None), key
    assert tui.key_action("3") == ("open", 3)
    assert tui.key_action("0") == ("unknown", None), "there is no PR 0 in the queue"
    assert tui.key_action("\xe0") == ("unknown", None), "a Windows arrow prefix is not a command"


def test_open_pr_never_raises_on_a_bad_index() -> None:
    from agents import tui

    assert "no PR" in tui.open_pr(FULL_SNAPSHOT, 7)
    assert "no PR" in tui.open_pr({"sections": {}}, 1)


def test_watch_verb_is_wired() -> None:
    from agents.cli import DEFAULT_WATCH_INTERVAL_S, build_parser

    args = build_parser().parse_args(["watch"])
    assert callable(getattr(args, "func", None))
    assert args.interval == DEFAULT_WATCH_INTERVAL_S and args.once is False
    assert build_parser().parse_args(["watch", "--once", "--interval", "3"]).interval == 3.0


def test_watch_collectors_cover_every_panel() -> None:
    """The screen may not grow a source the scriptable path cannot reach."""
    from agents.cli import _watch_collectors, build_parser

    collectors = _watch_collectors(build_parser().parse_args(["watch"]))
    assert set(collectors) == {"target_health", "pull_requests", "ledger", "cases", "guard"}
    assert all(callable(fn) for fn in collectors.values())


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
