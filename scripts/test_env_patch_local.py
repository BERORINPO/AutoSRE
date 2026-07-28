"""Offline smoke test for the config patch that AutoSRE writes into a real repo.

apply_env_value decides the actual diff of every remediation PR, so it gets a
test of its own. Failure class 1 (the variable is absent) appends; class 2 (it
is present but unusable) replaces; an already-canonical value writes nothing.

Usage:
    python scripts/test_env_patch_local.py
"""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

from agents.github_tools import apply_env_value  # noqa: E402

CANON = "postgres://demo:demo@db.internal:5432/app"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def case(name: str, text: str, expect_change: str, expect_text: str) -> None:
    got_text, got_change = apply_env_value(text, "DATABASE_URL", CANON)
    check(f"{name}: change={expect_change}", got_change == expect_change, f"got {got_change!r}")
    check(f"{name}: content", got_text == expect_text, f"got {got_text!r}")


def main() -> int:
    # class 1: absent -> append, leaving the rest of the file alone
    case(
        "absent",
        "PORT=8080\nLOG_LEVEL=info\n",
        "added",
        f"PORT=8080\nLOG_LEVEL=info\nDATABASE_URL={CANON}\n",
    )
    case("empty file", "", "added", f"\nDATABASE_URL={CANON}\n")

    # class 2: present but unusable -> replace in place, order preserved
    case(
        "invalid value",
        f"PORT=8080\nDATABASE_URL=mysql://demo:demo@db.internal:3306/app\nLOG_LEVEL=info\n",
        "corrected",
        f"PORT=8080\nDATABASE_URL={CANON}\nLOG_LEVEL=info\n",
    )
    case(
        "present but empty",
        "PORT=8080\nDATABASE_URL=\n",
        "corrected",
        f"PORT=8080\nDATABASE_URL={CANON}\n",
    )

    # nothing to do
    case(
        "already canonical",
        f"PORT=8080\nDATABASE_URL={CANON}\n",
        "already_correct",
        f"PORT=8080\nDATABASE_URL={CANON}\n",
    )

    # A '#' line is documentation, not config. Reviving one would be a change
    # nobody reviewed, so the value is appended instead and the comment survives.
    text = f"# DATABASE_URL={CANON}\nPORT=8080\n"
    got_text, got_change = apply_env_value(text, "DATABASE_URL", CANON)
    check("commented-out line is not revived", got_change == "added", f"got {got_change!r}")
    check(
        "commented-out line survives",
        got_text.startswith(f"# DATABASE_URL={CANON}\n") and got_text.count("DATABASE_URL=") == 2,
        f"got {got_text!r}",
    )

    # A different variable whose name ends with ours must not be hit.
    text = "EXTRA_DATABASE_URL=keep-me\n"
    got_text, got_change = apply_env_value(text, "DATABASE_URL", CANON)
    check(
        "similar variable name is not clobbered",
        got_change == "added" and "EXTRA_DATABASE_URL=keep-me" in got_text,
        f"got {got_change!r} {got_text!r}",
    )

    # Idempotence: applying twice must not produce a second line.
    once, _ = apply_env_value("PORT=8080\n", "DATABASE_URL", CANON)
    twice, change2 = apply_env_value(once, "DATABASE_URL", CANON)
    check(
        "applying twice is a no-op",
        change2 == "already_correct" and twice == once,
        f"got {change2!r}",
    )

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
