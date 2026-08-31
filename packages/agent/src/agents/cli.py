"""AutoSRE command line: the third adapter over the same core.

The investigate -> diagnose -> propose -> gate -> verify loop lives in
agents.agent / agents.diagnosis / agents.recovery. Until now the only ways to
start it were a Pub/Sub push and the web console, which meant you had to deploy
the whole system before you could watch it do anything once. This module runs
the same core from a terminal:

    autosre doctor            # is this machine able to run it at all?
    autosre run               # investigate -> diagnose -> open the fix PR
    autosre approve <pr>      # the human gate: merge, apply, verify recovery
    autosre status            # cost guard budget, trust ledger, target health

Design constraints:

  * Import time is stdlib only. `import agents.cli` must not pull in ADK,
    google-cloud-* or httpx, so `autosre doctor` can REPORT a missing
    dependency instead of dying on the import that proves it. Every heavy
    import is inside the function that needs it (matches server.py's style).
  * Every verb has two backends: the default runs the core in-process (no
    deploy needed), --remote talks HTTP to an already deployed agent-service -
    the same routes the console uses.
  * Exit codes are part of the contract. This is meant to be scriptable, so
    "the agent escalated" and "the guard blocked the run" must be
    distinguishable from "it crashed" without parsing stdout.
"""
import argparse
import json
import os
import sys

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2  # argparse's own code, kept here so the table is in one place
EXIT_ESCALATED = 3  # ran fine, but the fix is outside the auto-remediation policy
EXIT_BLOCKED = 4  # the cost guard / kill switch refused to start the run

DEFAULT_SERVICE = "sida-target"

# Values that must never be echoed back to a terminal or a log.
_SECRET_KEYS = frozenset({"GITHUB_TOKEN", "AUTOSRE_CONSOLE_KEY"})

# (key, required-for-local, what it is / how to get it)
_ENV_SPEC = (
    ("GOOGLE_CLOUD_PROJECT", True, "GCP project holding the target service and its logs"),
    ("GOOGLE_GENAI_USE_VERTEXAI", True, "TRUE sends Gemini through Vertex AI (unset = the SDK wants an API key)"),
    ("TARGET_HEALTH_URL", True, "health endpoint AutoSRE probes (or pass --health-url)"),
    ("GITHUB_TOKEN", True, "token that opens the fix PR (contents+pull_requests write)"),
    ("GITHUB_TARGET_REPO", True, "owner/repo whose config file the fix PR edits"),
    ("RUN_REGION", False, "Cloud Run region of the target (default: asia-northeast1)"),
    ("GOOGLE_CLOUD_LOCATION", False, "Vertex AI location for Gemini"),
    ("AUTOSRE_MODEL", False, "model id (default: gemini-2.5-flash)"),
    ("AUTOSRE_ALLOWED_ENV_VARS", False, "remediation allowlist (default: DATABASE_URL)"),
    ("TARGET_CONFIG_PATH", False, "config file in the target repo the PR edits"),
    ("AUTOSRE_STATE_URI", False, "gs:// object holding the cost guard state (unset = guard off)"),
    ("AUTOSRE_CASES_TABLE", False, "BigQuery table for case memory (unset = memory off)"),
    ("AUTOSRE_CONSOLE_KEY", False, "console key, only needed with --remote"),
)

_DEPENDENCIES = (
    ("google.adk", "google-adk", "the ReAct runner"),
    ("google.cloud.run_v2", "google-cloud-run", "reading the deployed config"),
    ("google.cloud.logging", "google-cloud-logging", "tailing the real error logs"),
    ("httpx", "httpx", "probing the target's health endpoint"),
)


# --------------------------------------------------------------------------
# pure helpers (no I/O, no heavy imports - the offline gate drives these)
# --------------------------------------------------------------------------
def outcome_exit_code(outcome: str) -> int:
    """Map a diagnosis outcome onto the process exit code.

    "healthy" is a success: being asked to investigate a service that turns out
    to be fine is a correct answer, not a failure. "undetermined" is not - it is
    the label for a run whose final answer could not be read, and a script that
    treats that as success is the console-shows-green-over-a-503 bug again.
    """
    if outcome in ("pr_opened", "healthy", "none", "dry_run"):
        return EXIT_OK
    if outcome == "escalated":
        return EXIT_ESCALATED
    if outcome.startswith("blocked_") or outcome.startswith("aborted_"):
        return EXIT_BLOCKED
    return EXIT_ERROR


def _shorten(text: str, limit: int = 96) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_step(ev: dict) -> str | None:
    """One line for one streamed event, or None if it is not worth a line.

    ASCII only on purpose: this prints into cmd.exe and PowerShell as often as
    into a UTF-8 terminal, and a UnicodeEncodeError in the progress renderer
    would abort a run that is otherwise fine.
    """
    if not isinstance(ev, dict):
        return None
    kind = ev.get("type")
    name = ev.get("name") or "?"
    # The indent is applied AFTER shortening: _shorten collapses whitespace, so
    # folding it in would flatten the step/result nesting into one column.
    if kind == "tool_call":
        args = ev.get("args") or {}
        rendered = ", ".join(f"{k}={_shorten(v, 48)}" for k, v in args.items())
        return "  -> " + _shorten(f"{name}({rendered})", 112)
    if kind == "tool_result":
        summary = ev.get("summary") or {}
        if isinstance(summary, dict) and summary:
            detail = " ".join(f"{k}={v}" for k, v in summary.items())
        else:
            detail = "done"
        return "     " + _shorten(f"{name}: {detail}", 112)
    if kind == "blocked":
        return f"  !! run blocked by the cost guard: {ev.get('reason')}"
    return None


def _row(key: str, value: str, ok: bool, required: bool, note: str) -> dict:
    return {"key": key, "value": value, "ok": ok, "required": required, "note": note}


def environment_report(env, remote: bool = False) -> list[dict]:
    """Which configuration is present. Secret values are never echoed back.

    With --remote the target's project/credentials belong to the deployed
    service, so only the console key is this machine's business.
    """
    rows = []
    for key, required_local, note in _ENV_SPEC:
        raw = (env.get(key) or "").strip()
        # --remote: project, credentials and the GitHub token belong to the
        # deployed service, so nothing here is required of this machine. A
        # deployment may also be running with no console key at all, so the key
        # is reported, never demanded.
        required = (not remote) and required_local
        if not raw:
            shown = "-"
        elif key in _SECRET_KEYS:
            shown = "set (hidden)"
        else:
            shown = _shorten(raw, 48)
        rows.append(_row(key, shown, bool(raw), required, note))
    return rows


def dependency_report(resolver=None) -> list[dict]:
    """Which python packages are importable. resolver is injectable for tests."""
    if resolver is None:
        from importlib.util import find_spec

        def resolver(module: str) -> bool:
            try:
                return find_spec(module) is not None
            except (ImportError, ValueError):
                return False

    rows = []
    for module, package, note in _DEPENDENCIES:
        ok = bool(resolver(module))
        rows.append(_row(module, package if ok else f"pip install {package}", ok, True, note))
    return rows


def credential_report(env, exists=None) -> list[dict]:
    """Whether Google credentials are reachable at all (ADC or a key file)."""
    if exists is None:
        exists = os.path.exists
    explicit = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    adc = os.path.join(
        env.get("APPDATA") or os.path.join(env.get("HOME", ""), ".config"),
        "gcloud",
        "application_default_credentials.json",
    )
    if explicit:
        ok, value = bool(exists(explicit)), _shorten(explicit, 48)
    else:
        ok, value = bool(exists(adc)), "application default credentials"
    return [
        _row(
            "google credentials",
            value if ok else "-",
            ok,
            True,
            "run: gcloud auth application-default login",
        )
    ]


def report_failures(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["required"] and not r["ok"]]


def render_report(title: str, rows: list[dict]) -> str:
    """Fixed-width report block. 'ok'/'--' markers, not colours or emoji."""
    width = max((len(r["key"]) for r in rows), default=0)
    lines = [f"{title}:"]
    for r in rows:
        key = f"{r['key']:<{width}}"
        if r["ok"]:
            lines.append(f"  [ok] {key}  {r['value']}")
        elif r["required"]:
            # The only lines a reader has to act on get the second line to
            # themselves; everything else stays one row so the block is skimmable.
            lines.append(f"  [--] {key}  {r['value']}")
            lines.append(f"       {'':<{width}}  {r['note']}")
        else:
            lines.append(f"  [  ] {key}  {r['value']}  ({r['note']})")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# remote backend (HTTP against a deployed agent-service)
# --------------------------------------------------------------------------
def _console_key(args) -> str:
    return (getattr(args, "key", None) or os.environ.get("AUTOSRE_CONSOLE_KEY") or "").strip()


def _request(args, method: str, path: str, body: dict | None = None, timeout: float = 600.0):
    import httpx

    key = _console_key(args)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = args.remote.rstrip("/") + path
    # A bodyless POST is rejected by the Cloud Run front end with 411 before it
    # ever reaches the container, so always send an object.
    payload = body if body is not None else {}
    with httpx.Client(timeout=timeout) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        else:
            resp = client.post(url, headers=headers, json=payload)
    if resp.status_code == 401:
        raise SystemExit(
            "the deployed agent requires a console key: pass --key or set AUTOSRE_CONSOLE_KEY"
        )
    resp.raise_for_status()
    return resp.json()


def _probe_health(url: str) -> dict:
    """Best-effort status code of a health endpoint. Never raises."""
    if not url:
        return {"ok": False, "error": "no health url configured"}
    try:
        import httpx

        resp = httpx.get(url, timeout=15.0)
        return {"ok": True, "status_code": resp.status_code, "healthy": resp.status_code == 200}
    except Exception as e:  # noqa: BLE001 - a probe is never the reason a verb fails
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    remote = bool(args.remote)
    sections = [("environment", environment_report(os.environ, remote=remote))]
    if not remote:
        sections.append(("python packages", dependency_report()))
        sections.append(("credentials", credential_report(os.environ)))
    if remote:
        health = _probe_health(args.remote.rstrip("/") + "/health")
        value = f"HTTP {health.get('status_code')}" if health.get("ok") else "unreachable"
        sections.append((
            "deployed agent",
            [_row(args.remote, value, bool(health.get("healthy")), True,
                  health.get("error") or "expected HTTP 200 from /health")],
        ))

    failures = [r for _, rows in sections for r in report_failures(rows)]
    if args.json:
        print(json.dumps({"sections": {t: rows for t, rows in sections},
                          "ok": not failures}, indent=2))
        return EXIT_OK if not failures else EXIT_ERROR

    for title, rows in sections:
        print(render_report(title, rows))
        print()
    if failures:
        print(f"{len(failures)} required item(s) missing - fix the lines marked [--] above.")
        return EXIT_ERROR
    print("ready: `autosre run` can start an incident from this machine.")
    return EXIT_OK


def _run_remote(args) -> dict:
    body = {"service_name": args.service}
    if args.health_url:
        body["target_health_url"] = args.health_url
    if args.video_ref:
        body["video_ref"] = args.video_ref
    return _request(args, "POST", "/incident", body)


def _run_local(args) -> dict:
    import asyncio
    import time

    from agents import state_store
    from agents.diagnosis import classify_outcome, parse_diagnosis, salvage_diagnosis
    from agents.incident import build_incident_text, video_clause

    health_url = args.health_url or os.environ.get("TARGET_HEALTH_URL", "")
    if getattr(args, "dry_run", False):
        # Armed for the whole process, and read by open_pull_request itself.
        os.environ["AUTOSRE_DRY_RUN"] = "1"
    # Operator-initiated, like the console button: skip the alert-storm cooldown,
    # but still charge the daily budget and obey the kill switch. A CLI that
    # sidesteps the guard would make the guard a property of one adapter.
    allowed, reason = state_store.reserve_run(cooldown_override=0)
    if not allowed:
        return {"outcome": f"blocked_{reason}", "diagnosis": None, "raw_final": "",
                "error": f"run budget gate: {reason}"}

    incident_text = build_incident_text(args.service, health_url) + video_clause(args.video_ref)

    async def drive() -> dict:
        from google.adk.agents.invocation_context import LlmCallsLimitExceededError

        from agents.agent import run_incident_events

        trace: list[dict] = []
        diagnosis: dict = {}
        final_text = ""
        cost = None
        try:
            async for ev in run_incident_events(incident_text):
                if not args.json:
                    line = format_step(ev)
                    if line:
                        print(line, flush=True)
                if ev.get("type") == "tool_result":
                    trace.append({"name": ev.get("name"), "summary": ev.get("summary")})
                if ev.get("type") == "final":
                    final_text = ev.get("final") or ""
                    cost = ev.get("cost")
                    diagnosis = salvage_diagnosis(parse_diagnosis(final_text), trace)
        except LlmCallsLimitExceededError as e:
            # The per-incident LLM call ceiling fired. That is a cost guard doing
            # its job, not a crash - report it as an outcome.
            return {"outcome": "aborted_llm_limit", "diagnosis": None, "raw_final": "",
                    "error": str(e)}
        return {
            "outcome": classify_outcome(diagnosis),
            "diagnosis": diagnosis,
            "raw_final": final_text,
            "cost": cost,
        }

    started = time.time()
    result = asyncio.run(drive())
    if getattr(args, "dry_run", False):
        # A suppressed run is not a case. Recording it would put an incident
        # with no remediation into the memory the trust ledger counts, i.e. a
        # rehearsal would move the bar that decides what may act unattended.
        if result.get("outcome") not in ("undetermined",) and not result.get("error"):
            result["outcome"] = "dry_run"
        result["dry_run"] = True
        return result
    if result.get("diagnosis"):
        from agents.case_store import record_diagnosis  # default-off, never raises

        record_diagnosis(
            result["diagnosis"], source="cli", service=args.service,
            duration_s=time.time() - started,
        )
    return result


def _print_run_result(result: dict) -> None:
    diagnosis = result.get("diagnosis") or {}
    outcome = result.get("outcome", "unknown")
    print()
    print(f"outcome:    {outcome}")
    if result.get("error"):
        print(f"error:      {result['error']}")
    if diagnosis.get("root_cause"):
        print(f"root cause: {_shorten(diagnosis['root_cause'], 200)}")
    if diagnosis.get("missing_env_var"):
        print(f"variable:   {diagnosis['missing_env_var']}")
    if diagnosis.get("confidence") is not None:
        print(f"confidence: {diagnosis['confidence']}")
    if result.get("dry_run"):
        print("dry run:    no pull request was opened. Re-run without --dry-run to "
              "let the agent propose the fix for real.")
    if diagnosis.get("pr_url"):
        print(f"fix PR:     {diagnosis['pr_url']}")
        print()
        print("The gate is yours. Review the PR, then apply it:")
        print(f"  autosre approve {diagnosis.get('pr_number')}")
    escalation = diagnosis.get("escalation") or {}
    if escalation:
        print()
        print(f"escalated:  {_shorten(escalation.get('reason', ''), 200)}")
        for i, step in enumerate(escalation.get("runbook") or [], 1):
            print(f"  {i}. {_shorten(step, 160)}")
    cost = result.get("cost") or {}
    if cost:
        print()
        print(f"cost:       {json.dumps(cost, ensure_ascii=False)}")


def cmd_run(args) -> int:
    if args.dry_run and args.remote:
        # The deployed service has no dry-run mode, so honouring the flag over
        # HTTP would mean opening a real PR while claiming not to.
        print("--dry-run is a local mode; a deployed agent cannot suppress its own "
              "writes. Drop --remote to rehearse.", file=sys.stderr)
        return EXIT_USAGE
    result = _run_remote(args) if args.remote else _run_local(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_run_result(result)
    return outcome_exit_code(result.get("outcome") or "")


def _approve_local(args) -> dict:
    import time

    from agents.github_tools import allowed_env_vars
    from agents.recovery import apply_env_fix, merge_pull_request, verify_recovery

    if args.env_var not in allowed_env_vars():
        return {
            "ok": False,
            "error": f"'{args.env_var}' is not in the allowed remediation set; "
            "escalated variables require manual operation",
        }
    value = os.environ.get(f"AUTOSRE_RESTORE_{args.env_var}", "")
    health_url = args.health_url or os.environ.get("TARGET_HEALTH_URL", "")
    started = time.time()
    merge = merge_pull_request(args.pr_number)
    applied = apply_env_fix(args.service, args.env_var, value)
    verify = verify_recovery(health_url)
    from agents.case_store import record_resolution  # closes the learning loop

    record_resolution(args.pr_number, bool(verify.get("recovered")), time.time() - started)
    return {"merge": merge, "apply": applied, "verify": verify}


def cmd_approve(args) -> int:
    if args.remote:
        body = {"pr_number": args.pr_number, "service_name": args.service,
                "env_var": args.env_var}
        if args.health_url:
            body["target_health_url"] = args.health_url
        result = _request(args, "POST", "/approve", body)
    else:
        result = _approve_local(args)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("error"):
            print(f"refused: {result['error']}")
        else:
            merge = result.get("merge") or {}
            verify = result.get("verify") or {}
            print(f"merged:    PR #{args.pr_number} -> {merge.get('ok')}")
            print(f"applied:   {args.env_var} on {args.service} -> "
                  f"{(result.get('apply') or {}).get('ok')}")
            print(f"verified:  recovered={verify.get('recovered')} "
                  f"status={verify.get('status_code')} after {verify.get('elapsed_s')}s")
    verified = (result.get("verify") or {}).get("recovered")
    return EXIT_OK if verified else EXIT_ERROR


def cmd_status(args) -> int:
    if args.remote:
        guard = _request(args, "GET", "/guard", timeout=60.0)
        ledger = _request(args, "GET", "/trust-ledger", timeout=60.0)
    else:
        from agents import autonomy, state_store

        guard = state_store.read_state()
        ledger = autonomy.ledger(state_store.read_demotions())
    health = _probe_health(args.health_url or os.environ.get("TARGET_HEALTH_URL", ""))
    payload = {"guard": guard, "ledger": ledger, "target_health": health}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("cost guard:")
    print(f"  enabled      {guard.get('enabled')}   available {guard.get('available')}")
    print(f"  runs today   {guard.get('runs_today')} (day {guard.get('day')})")
    print(f"  killswitch   {(guard.get('killswitch') or {}).get('tripped')}")
    print()
    print("trust ledger (which classes may act without a click):")
    classes = ledger.get("classes") or []
    if not classes:
        print("  (no verified recoveries recorded yet - every class stays behind the gate)")
    for c in classes:
        print(f"  {c.get('class_key'):<24} {c.get('successes')}/{c.get('attempts')} "
              f"lower_bound={c.get('lower_bound')} promoted={c.get('promoted')}")
    print()
    if health.get("ok"):
        print(f"target health: HTTP {health['status_code']}")
    else:
        print(f"target health: unknown ({health.get('error')})")
    return EXIT_OK


def cmd_version(args) -> int:
    print(f"autosre {VERSION}")
    return EXIT_OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosre",
        description="AutoSRE - an autonomous on-call SRE agent, from your terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  autosre doctor                        what is this machine still missing?\n"
            "  autosre run --dry-run                 investigate and diagnose, open no PR\n"
            "  autosre run                           ...and open the fix PR for real\n"
            "  autosre approve 42                    merge it, apply it, verify /health\n"
            "  autosre status --json                 guard + ledger, machine-readable\n"
            "  autosre run --remote https://agent.example --key $AUTOSRE_CONSOLE_KEY\n"
            "                                        drive an already-deployed agent\n"
            "\n"
            "exit codes:\n"
            "  0  a fix PR was opened, the service is genuinely healthy, or a dry run\n"
            "  1  error - including 'undetermined', an answer that could not be read\n"
            "  2  usage\n"
            "  3  escalated: outside the auto-remediation allowlist, a human must act\n"
            "  4  blocked by the cost guard (cooldown, daily limit, kill switch)\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"autosre {VERSION}")
    sub = parser.add_subparsers(dest="verb", metavar="<verb>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--remote", metavar="URL",
                       help="talk to a deployed agent-service instead of running in-process")
        p.add_argument("--key", metavar="KEY",
                       help="console key for --remote (default: $AUTOSRE_CONSOLE_KEY)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    doctor = sub.add_parser("doctor", help="check this machine can run AutoSRE")
    common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="investigate an incident and open the fix PR")
    common(run)
    run.add_argument("--service", default=DEFAULT_SERVICE,
                     help=f"target Cloud Run service (default: {DEFAULT_SERVICE})")
    run.add_argument("--health-url", help="target health endpoint (default: $TARGET_HEALTH_URL)")
    run.add_argument("--video-ref", help="gs:// URI of a screen recording attached to the report")
    run.add_argument("--dry-run", action="store_true",
                     help="investigate and diagnose, but open no PR (local runs only)")
    run.set_defaults(func=cmd_run)

    approve = sub.add_parser("approve", help="merge the fix PR, apply it, verify recovery")
    common(approve)
    approve.add_argument("pr_number", type=int, help="the PR number `autosre run` opened")
    approve.add_argument("--service", default=DEFAULT_SERVICE)
    approve.add_argument("--env-var", default="DATABASE_URL",
                         help="the variable being restored (default: DATABASE_URL)")
    approve.add_argument("--health-url")
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status", help="cost guard, trust ledger and target health")
    common(status)
    status.add_argument("--health-url")
    status.set_defaults(func=cmd_status)

    version = sub.add_parser("version", help="print the version")
    common(version)
    version.set_defaults(func=cmd_version)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "verb", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except SystemExit as e:  # raised by the verbs for expected, explained failures
        code = e.code
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR
    except ImportError as e:
        print(f"missing dependency: {e}", file=sys.stderr)
        print("run `autosre doctor` to see what this machine is missing.", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001 - a CLI reports failures, it does not traceback
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
