"""AutoSRE agent-service (Cloud Run, FastAPI + ADK).

Day 1 smoke gate: prove that Vertex Gemini + Cloud Logging both work from the
Cloud Run runtime service account. Day 2 adds /incident (the ReAct investigate ->
diagnose -> propose -> open-PR loop) and /incident/stream (per-connection SSE).

/events is a persistent, read-only SSE broadcast channel: any open console
subscribes on page load, and a Pub/Sub-triggered agent run on POST /pubsub/incident
is fanned out to every subscriber so a real Cloud Monitoring alert makes every open
console timeline come alive with no human click.

Env contract (set via `gcloud run deploy --set-env-vars`):
  GOOGLE_GENAI_USE_VERTEXAI=TRUE
  GOOGLE_CLOUD_PROJECT=<project id>
  GOOGLE_CLOUD_LOCATION=<vertex location, e.g. global or us-central1>
"""
import asyncio
import base64
import collections
import hmac
import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# Eager (unlike the other agents.* imports, which are lazy to keep startup fast):
# state_store pulls only stdlib at import time — the storage client is lazy
# inside it — and it gates every run-starting route, so it is on the hot path.
from agents import state_store

_UI_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

app = FastAPI(title="AutoSRE agent-service")

GEMINI_MODEL = "gemini-2.5-flash"


# NOTE: "/healthz" is a reserved path intercepted by the Cloud Run front end
# (GFE returns its own 404 before the request reaches the container), so we
# expose the health check at "/health" instead.
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    return _UI_HTML


@app.get("/target-health")
def target_health() -> dict:
    from agents.tools import probe_health

    return probe_health(os.environ.get("TARGET_HEALTH_URL", ""))


@app.get("/api/info")
def info() -> dict:
    return {
        "service": "autosre-agent",
        "project": os.environ.get("GOOGLE_CLOUD_PROJECT"),
        "location": os.environ.get("GOOGLE_CLOUD_LOCATION"),
        "use_vertex": os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"),
    }


def _gemini_smoke() -> dict:
    """One real Gemini call via Vertex AI (uses the runtime SA / ADC)."""
    try:
        from google import genai

        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        client = genai.Client(vertexai=True, project=project, location=location)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Reply with exactly this token and nothing else: SMOKE_OK",
        )
        return {"ok": True, "model": GEMINI_MODEL, "text": (resp.text or "").strip()}
    except Exception as e:  # noqa: BLE001 - surface the exact error to the smoke caller
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _logging_smoke() -> dict:
    """One real Cloud Logging read (proves logging.viewer + API from Cloud Run)."""
    try:
        from google.cloud import logging as gcloud_logging

        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        client = gcloud_logging.Client(project=project)
        # /smoke is unauthenticated: prove the Logging read happened (count +
        # severities) but never return raw payload text, which can carry secrets.
        recent: list[str] = []
        for entry in client.list_entries(order_by=gcloud_logging.DESCENDING, page_size=5):
            recent.append(str(getattr(entry, "severity", None) or "DEFAULT"))
            if len(recent) >= 3:
                break
        return {"ok": True, "count": len(recent), "recent_severities": recent}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/smoke")
def smoke() -> JSONResponse:
    """Day-1 gate: one real Gemini call + one real Cloud Logging read from Cloud Run."""
    gemini = _gemini_smoke()
    logging_result = _logging_smoke()
    overall_ok = gemini["ok"] and logging_result["ok"]
    return JSONResponse(
        status_code=200 if overall_ok else 500,
        content={"overall_ok": overall_ok, "gemini": gemini, "logging": logging_result},
    )


def _parse_diagnosis(text: str) -> dict:
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
        return {"error": f"parse_failed: {e}", "raw": text[:500]}


def _classify_outcome(d: dict) -> str:
    """Classify the agent's diagnosis into a single outcome label (frozen cross-worker contract)."""
    if d.get("pr_url"):
        return "pr_opened"
    if d.get("action") == "escalate" or d.get("escalation"):
        return "escalated"
    if d.get("missing_env_var") is None:
        return "healthy"
    return "none"


def _record_case(diagnosis: dict, source: str, service: str, started_ts: float) -> None:
    """Persist the diagnosed case to memory (case_store handles default-off + never raises).

    The synchronous BigQuery insert (~0.3s) runs after the final diagnosis is
    already produced, so it does not affect the measured investigation timings.
    """
    from agents.case_store import record_diagnosis  # lazy import (matches codebase style)

    record_diagnosis(diagnosis, source=source, service=service, duration_s=time.time() - started_ts)


def _log_run_cost(cost: dict | None, source: str) -> None:
    """Emit this run's own LLM usage as one structured log line.

    The console shows the number to whoever is watching; this line is what makes
    "median cost over the last N real runs" answerable afterwards without
    re-running anything. Deliberately not written to the case table - that
    schema is about incidents, not about our own meter.
    """
    if not cost:
        return
    try:
        print(json.dumps({"severity": "INFO", "event": "run_cost", "source": source, **cost}))
    except Exception:  # noqa: BLE001 - never break a run in order to log it
        pass
    # A new diagnosis row changes the learned-cases counter -> next /console-meta
    # must recompute (keeps the console's learn-tick fresh despite the TTL cache).
    _invalidate_console_meta()


def _video_clause(video_ref: str | None) -> str:
    """Append a screen-recording hint to the incident prompt when one is available.

    Precedence: an explicit per-request video_ref, else AUTOSRE_REPORT_VIDEO_URI
    (the demo default). Empty on both -> "" -> the agent never calls the video
    tool = current behavior. Keeps the video feature staged/default-off."""
    from agents.video_tools import enabled as _video_enabled

    if not _video_enabled():
        return ""  # feature off -> never mention a video (also avoids a wasted tool call)
    ref = (video_ref or os.environ.get("AUTOSRE_REPORT_VIDEO_URI", "")).strip()
    # Only splice a STRICT gs:// URI into the instruction. Rejecting whitespace/prose
    # means a caller-controlled video_ref cannot carry a prompt-injection payload. (CISO M-1)
    if not ref or not re.fullmatch(r"gs://[\w.\-/]+", ref):
        return ""
    return (
        f" A user attached a screen recording at {ref}. "
        "Call analyze_report_video on it to extract the reproduction steps and timeline "
        "before you diagnose."
    )


class IncidentRequest(BaseModel):
    service_name: str = "sida-target"
    target_health_url: str | None = None
    video_ref: str | None = None


@app.post("/incident")
async def incident(req: IncidentRequest, request: Request) -> dict:
    """Run the AutoSRE agent on an incident: investigate -> diagnose -> open a real fix PR."""
    # Gate on the console key (no-op when AUTOSRE_CONSOLE_KEY is unset). This endpoint
    # accepts a caller-controlled video_ref, so it must not be an open trigger. (CISO M-2)
    _check_console_key(request)
    # lazy imports (heavy ADK deps, keep startup fast)
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError

    from agents.agent import run_incident

    health_url = req.target_health_url or os.environ.get("TARGET_HEALTH_URL", "")
    incident_text = (
        f"Incident: the Cloud Run service '{req.service_name}' is reported unhealthy. "
        f"Its health endpoint is {health_url}. Investigate and diagnose the single root cause."
    )
    incident_text += _video_clause(req.video_ref)
    started = time.time()
    # Operator-initiated: no storm cooldown, but it still spends daily budget and
    # obeys the kill switch.
    allowed, reason = _claim_run_slot(started, apply_cooldown=False)
    if not allowed:
        return {
            "steps": [],
            "outcome": f"blocked_{reason}",
            "diagnosis": None,
            "raw_final": "",
            "error": f"run budget gate: {reason}",
        }
    try:
        result = await run_incident(incident_text)
    except LlmCallsLimitExceededError as e:
        # The per-incident LLM call ceiling (agent.run_config) fired. This is a
        # cost guard doing its job, not a server fault -> report it as an
        # outcome instead of a 500 so the console can render it.
        return {
            "steps": [],
            "outcome": "aborted_llm_limit",
            "diagnosis": None,
            "raw_final": "",
            "error": str(e),
        }
    diagnosis = _parse_diagnosis(result["final"])
    # to_thread: the sync BigQuery insert must not block the event loop
    # (a slow insert would freeze /events heartbeats on this single instance).
    await asyncio.to_thread(_record_case, diagnosis, "manual", req.service_name, started)
    # Earned autonomy (default-off): None leaves this response byte-identical.
    autonomy_audit = await asyncio.to_thread(
        _maybe_execute_autonomously, diagnosis, req.service_name, "manual"
    )
    response = {
        "steps": result["steps"],
        "outcome": _classify_outcome(diagnosis),
        "diagnosis": diagnosis,
        "raw_final": result["final"],
    }
    if autonomy_audit is not None:
        response["autonomy"] = autonomy_audit
    return response


def _check_console_key(request: Request) -> None:
    """Gate on AUTOSRE_CONSOLE_KEY (default-off). Accept Bearer header OR ?key= query param."""
    expected = os.environ.get("AUTOSRE_CONSOLE_KEY", "")
    if not expected:
        return  # unset -> open = current behavior
    # Check BOTH credentials independently: a client may carry an unrelated
    # Authorization header (e.g. Cloud Scheduler attaches a Google OIDC JWT)
    # while supplying the console key via ?key= - the header must not shadow it.
    supplied_header = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    supplied_query = request.query_params.get("key", "")
    if not (
        hmac.compare_digest(supplied_header, expected)
        or hmac.compare_digest(supplied_query, expected)
    ):
        raise HTTPException(status_code=401, detail="console key required")


class ApproveRequest(BaseModel):
    pr_number: int
    service_name: str = "sida-target"
    env_var: str = "DATABASE_URL"
    target_health_url: str | None = None


@app.post("/approve")
def approve(req: ApproveRequest, request: Request) -> dict:
    """Human-gated recovery: merge the fix PR, apply the env fix, verify the target is healthy."""
    _check_console_key(request)
    from agents.github_tools import allowed_env_vars
    from agents.recovery import apply_env_fix, merge_pull_request, verify_recovery

    if req.env_var not in allowed_env_vars():
        return {
            "ok": False,
            "error": f"'{req.env_var}' is not in the allowed remediation set; escalated vars require manual operation",
        }
    value = os.environ.get(f"AUTOSRE_RESTORE_{req.env_var}", "")
    started = time.time()
    merge = merge_pull_request(req.pr_number)
    applied = apply_env_fix(req.service_name, req.env_var, value)
    health_url = req.target_health_url or os.environ.get("TARGET_HEALTH_URL", "")
    verify = verify_recovery(health_url)
    # Close the learning loop: remember whether the human-approved fix actually
    # recovered the service (joined to the diagnosis case by pr_number).
    from agents.case_store import record_resolution

    record_resolution(req.pr_number, bool(verify.get("recovered")), time.time() - started)
    # The verified-recoveries counter just changed -> the console refetches
    # /console-meta right after this response; bypass the TTL cache for it.
    _invalidate_console_meta()
    return {"merge": merge, "apply": applied, "verify": verify}


@app.get("/incident/stream")
async def incident_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of the agent's steps as they happen (live demo).

    Gated on the console key like every other agent-starting route. EventSource
    cannot set headers, so the console passes it as ?key= - _check_console_key
    accepts either credential independently.
    """
    _check_console_key(request)
    from agents.agent import run_incident_events

    health_url = os.environ.get("TARGET_HEALTH_URL", "")
    incident_text = (
        "Incident: the Cloud Run service 'sida-target' is reported unhealthy. "
        f"Its health endpoint is {health_url}. Investigate and diagnose the single root cause."
    )
    incident_text += _video_clause(None)

    async def gen():
        yield "retry: 60000\n\n"
        started = time.time()
        # Same gate as /incident, reported inside the stream so the console can
        # render it (a non-SSE error response would surface as a generic
        # connection failure).
        allowed, reason = _claim_run_slot(started, apply_cooldown=False)
        if not allowed:
            yield f"data: {json.dumps({'type': 'blocked', 'reason': reason})}\n\n"
            yield 'data: {"type": "done"}\n\n'
            return
        diagnosis: dict = {}
        try:
            async for ev in run_incident_events(incident_text):
                if ev.get("type") == "final":
                    diagnosis = _parse_diagnosis(ev["final"])
                    run_cost = ev.get("cost")
                    await asyncio.to_thread(
                        _record_case, diagnosis, "console", "sida-target", started
                    )
                    _log_run_cost(run_cost, "console")
                    ev = {
                        "type": "final",
                        "outcome": _classify_outcome(diagnosis),
                        "diagnosis": diagnosis,
                        "raw_final": ev["final"],
                        "cost": run_cost,
                    }
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            yield 'data: {"type": "done"}\n\n'
            return
        # Earned autonomy on the demo path. This is the same console the human
        # would approve in, so the stream narrates each stage live and keeps
        # the connection warm with comment frames while the pipeline works
        # (rehearsal alone can take a quiet minute). None = feature off/not
        # applicable -> the stream ends exactly as it did before this existed.
        if diagnosis:
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def _emit_to_stream(step: dict) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, step)

            pipeline = asyncio.ensure_future(
                asyncio.to_thread(
                    _maybe_execute_autonomously,
                    diagnosis,
                    "sida-target",
                    "console",
                    _emit_to_stream,
                )
            )
            while not (pipeline.done() and queue.empty()):
                try:
                    step = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"data: {json.dumps({'type': 'autonomy_step', **step})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
            try:
                audit = pipeline.result()
            except Exception as e:  # noqa: BLE001 - narrate, never hang the stream
                audit = {"outcome": "autonomy_error", "error": str(e)[:300]}
            if audit is not None:
                yield f"data: {json.dumps({'type': 'autonomy', 'audit': audit})}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/reset")
def reset(request: Request) -> dict:
    """Re-arm the demo: break the target again and reset the config repo (for repeated judging)."""
    _check_console_key(request)
    from agents.recovery import inject_failure, reset_repo_config

    return {
        "inject": inject_failure("sida-target", "DATABASE_URL"),
        "repo_reset": reset_repo_config(),
    }


def _maybe_execute_autonomously(
    diagnosis: dict, service_name: str, source: str, emit=None
) -> dict | None:
    """Earned autonomy: close the loop with NO human click, iff this class earned it.

    Runs after a diagnosis produced a fix PR. Every step fails closed to the
    existing human gate: feature off, class not promoted, allowlist miss,
    rehearsal not green -> return with serving traffic untouched and the PR
    waiting for a human exactly as today. The one path that acts is:
    promoted class -> fix proven green on a zero-traffic rehearsal revision ->
    merge + traffic promote -> verify on the real URL. A failed verify reverts
    traffic to the pre-fix revision and DEMOTES the class on the spot
    (one strike); the failure is also recorded into the same ledger the
    promotion was earned from.

    Returns None when autonomy is off/irrelevant (response stays byte-identical
    to today), else an audit dict the console renders as the post-hoc record.
    """
    from agents import autonomy

    if not autonomy.enabled():
        return None
    env_var = diagnosis.get("missing_env_var")
    pr_number = diagnosis.get("pr_number")
    if diagnosis.get("action") != "fix_pr" or not env_var or not pr_number:
        return None
    from agents.github_tools import allowed_env_vars

    if env_var not in allowed_env_vars():
        return None

    def _emit(stage: str, **fields) -> None:
        """Progress callback for live surfaces (SSE stream / ambient broadcast).

        A broken emitter must never break the recovery it is narrating."""
        if emit is None:
            return
        try:
            emit({"stage": stage, **fields})
        except Exception:  # noqa: BLE001
            pass

    demotions = state_store.read_demotions()
    promoted, evidence = autonomy.promoted_for(env_var, demotions)
    result: dict = {
        "class": autonomy.class_key(env_var),
        "promoted": promoted,
        "evidence": evidence,
        "attempted": False,
    }
    if not promoted:
        result["outcome"] = "gate_kept"
        return result
    _emit(
        "engaged",
        **{
            "class": result["class"],
            "successes": evidence.get("successes"),
            "attempts": evidence.get("attempts"),
            "wilson_lower_bound": evidence.get("wilson_lower_bound"),
        },
    )

    from urllib.parse import urlsplit

    from agents.case_store import record_resolution
    from agents.recovery import (
        merge_pull_request,
        promote_rehearsal,
        rehearse_env_fix,
        revert_traffic,
        verify_recovery,
    )

    value = os.environ.get(f"AUTOSRE_RESTORE_{env_var}", "")
    result["attempted"] = True
    _emit("rehearsal_started")
    rehearsal = rehearse_env_fix(service_name, env_var, value)
    result["rehearsal"] = rehearsal
    if not rehearsal.get("ok"):
        result["outcome"] = "rehearsal_error_gate_kept"
        _emit("gate_kept", reason="rehearsal_error")
        return result
    _emit("rehearsal_deployed", url=rehearsal.get("rehearsal_url", ""))

    health_url = os.environ.get("TARGET_HEALTH_URL", "")
    health_path = urlsplit(health_url).path or "/health"
    rehearsal_health = verify_recovery(
        rehearsal["rehearsal_url"].rstrip("/") + health_path, timeout_s=90
    )
    result["rehearsal_health"] = rehearsal_health
    if not rehearsal_health.get("recovered"):
        # The fix did not prove itself next door -> nothing was risked, the
        # serving revision never changed, and the PR waits for a human.
        result["outcome"] = "rehearsal_red_gate_kept"
        _emit("gate_kept", reason="rehearsal_red")
        return result
    _emit("rehearsal_green")

    started = time.time()
    result["merge"] = merge_pull_request(int(pr_number))
    _emit("merged", pr_number=pr_number)
    promote = promote_rehearsal(service_name)
    result["promote"] = promote
    _emit("promoted")
    verify = verify_recovery(health_url)
    result["verify"] = verify
    recovered = bool(verify.get("recovered")) and bool(promote.get("ok"))
    # Feed the SAME ledger the promotion was earned from - autonomous outcomes
    # must count exactly like human-approved ones, or the statistics go blind
    # right when the gate opens.
    record_resolution(int(pr_number), recovered, time.time() - started)
    if recovered:
        result["outcome"] = "autonomous_recovery_verified"
        _emit("verified", status_code=verify.get("status_code"))
    else:
        result["revert"] = revert_traffic(
            service_name, rehearsal["snapshot"]["prev_revision"]
        )
        demoted = state_store.set_demotion(
            result["class"], reason=f"autonomous verify failed (pr #{pr_number})"
        )
        result["demotion_recorded"] = demoted
        _emit("reverted_demoted", revert_ok=bool(result["revert"].get("ok")))
        result["outcome"] = "reverted_and_demoted"
        print(
            json.dumps(
                {
                    "severity": "ERROR" if demoted else "CRITICAL",
                    "event": "autonomy_demoted",
                    "class": result["class"],
                    "pr_number": pr_number,
                    "demotion_recorded": demoted,
                    "source": source,
                }
            ),
            flush=True,
        )
    _invalidate_console_meta()
    return result


@app.get("/trust")
def trust_ledger(request: Request) -> dict:
    """The trust ledger: per-class verified success record vs the promotion bar.

    This endpoint IS the feature's honesty: it shows the gate's retirement
    schedule before, during and after promotion, from the same BigQuery rows
    the learning loop records - nothing here is asserted at pitch time. Key-
    gated like /guard: which classes act without approval is operator data.
    """
    _check_console_key(request)
    from agents import autonomy

    return autonomy.ledger(state_store.read_demotions())


class RearmRequest(BaseModel):
    class_key: str


@app.post("/trust/rearm")
def trust_rearm(req: RearmRequest, request: Request) -> dict:
    """Human re-arm of a demoted class. Deliberately the ONLY road back:
    statistics can open the gate the first time, but after a strike the ledger
    alone can never reopen it - a person has to look at what went wrong."""
    _check_console_key(request)
    from agents import autonomy

    ok = state_store.clear_demotion(req.class_key)
    return {"ok": ok, "ledger": autonomy.ledger(state_store.read_demotions())}


class KillswitchRequest(BaseModel):
    tripped: bool
    reason: str = ""


@app.get("/guard")
def guard_state(request: Request) -> dict:
    """Cost-guard state: armed or not, today's budget spend, kill switch.

    Worth exposing at all because reserve_run() returns allowed=True with
    reason="disabled" when AUTOSRE_STATE_URI is unset: an inert guard is
    indistinguishable from a working one by watching runs succeed. read_state()
    reports enabled/available separately and never raises, so "unconfigured" and
    "unreachable" cannot masquerade as a healthy zero.

    Key-gated rather than folded into the unauthenticated /console-meta: "the
    guard is off" and "the budget is nearly spent" are precisely what someone
    trying to burn the budget would want to read.
    """
    _check_console_key(request)
    return state_store.read_state()


@app.post("/guard/killswitch")
def guard_killswitch(req: KillswitchRequest, request: Request) -> dict:
    """Trip or clear the kill switch. Same key gate as /reset.

    The daily-limit self-trip deliberately stays tripped until a human clears
    it; without this route the only way to clear it was hand-editing the GCS
    object, which is not something to be doing on stage. `ok` is False when the
    store is not configured, so a no-op cannot read as a successful clear.
    """
    _check_console_key(request)
    ok = state_store.set_killswitch(req.tripped, req.reason)
    return {"ok": ok, "requested": req.tripped, "state": state_store.read_state()}


@app.get("/user-reports")
def user_reports() -> dict:
    """Return recent user-reported problems (for the console's 'user voice' panel)."""
    from agents.github_tools import get_user_reviews

    return get_user_reviews()


# Unauthenticated endpoint -> a request loop must not amplify into BigQuery jobs
# (CISO WARN): serve from a short in-process cache, invalidated on the two events
# that actually change the counters (diagnosis recorded / resolution recorded) so
# the console's learn-tick after /approve is never served stale.
_console_meta_cache: dict = {"ts": 0.0, "data": None}
_CONSOLE_META_TTL_S = 30.0


def _invalidate_console_meta() -> None:
    _console_meta_cache["ts"] = 0.0


@app.get("/console-meta")
def console_meta() -> dict:
    """Read-only console metadata: case-memory growth counters + report-video availability.

    Both capabilities follow the default-off contract, so this endpoint degrades to
    {"enabled": false} sections on an unconfigured deploy. Counts only — no case
    content — so it stays as open as /target-health and /user-reports.
    """
    now = time.time()
    if _console_meta_cache["data"] is not None and now - _console_meta_cache["ts"] < _CONSOLE_META_TTL_S:
        return _console_meta_cache["data"]
    from agents.case_store import memory_stats
    from agents.video_tools import enabled as video_enabled

    video_ref = os.environ.get("AUTOSRE_REPORT_VIDEO_URI", "").strip()
    data = {
        "memory": memory_stats(),
        # same predicate as /report-video's own gate, so "available" can never
        # advertise a player that would then 404 (misconfigured non-gs:// URI)
        "video": {"enabled": video_enabled(),
                  "available": bool(video_enabled() and video_ref.startswith("gs://"))},
    }
    _console_meta_cache.update(ts=now, data=data)
    return data


# The demo clip is seconds long; anything bigger points at a misconfigured URI.
_REPORT_VIDEO_MAX_BYTES = 50 * 1024 * 1024


@app.get("/report-video")
def report_video(request: Request):
    """Serve the demo report recording — the exact gs:// object Gemini watches.

    Buffered fetch (the demo clip is seconds long, size-capped) with single-range
    support: Safari refuses to play media whose server ignores Range requests, so
    a `Range:` header gets a proper 206 slice. Key-gated like /approve (the
    <video> tag cannot send headers, so the console appends ?key=). The object
    path comes ONLY from AUTOSRE_REPORT_VIDEO_URI; no caller input reaches GCS,
    so this can never become an arbitrary-read oracle.
    """
    _check_console_key(request)
    from fastapi.responses import Response

    from agents.video_tools import enabled as video_enabled
    from agents.video_tools import mime_for

    ref = os.environ.get("AUTOSRE_REPORT_VIDEO_URI", "").strip()
    if not (video_enabled() and ref.startswith("gs://")):
        raise HTTPException(status_code=404, detail="report video not configured")
    try:
        from google.cloud import storage  # lazy import (matches codebase style)

        bucket_name, _, blob_name = ref.removeprefix("gs://").partition("/")
        client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        blob = client.bucket(bucket_name).get_blob(blob_name, timeout=15.0)
        if blob is None:
            raise HTTPException(status_code=404, detail="report video object not found")
        if (blob.size or 0) > _REPORT_VIDEO_MAX_BYTES:
            raise HTTPException(status_code=502, detail="report video exceeds size cap")
        data = blob.download_as_bytes(timeout=30.0)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - degrade to an error status, never a 500 traceback
        print(json.dumps({"severity": "WARNING", "event": "report_video_fetch_failed",
                          "error": f"{type(e).__name__}: {e}"}), flush=True)
        raise HTTPException(status_code=502, detail="report video fetch failed")
    media = mime_for(ref)
    common = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600",
              "X-Content-Type-Options": "nosniff"}
    m = re.match(r"bytes=(\d+)-(\d*)$", request.headers.get("range", ""))
    if m and int(m.group(1)) < len(data):
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
        return Response(content=data[start:end + 1], status_code=206, media_type=media,
                        headers={**common, "Content-Range": f"bytes {start}-{end}/{len(data)}"})
    return Response(content=data, media_type=media, headers=common)


_last_auto_trigger = {"ts": 0.0}
_AUTO_COOLDOWN_S = 300


def _claim_run_slot(now: float, apply_cooldown: bool = True) -> tuple[bool, str]:
    """Rate-limit + budget gate for anything that starts a billable agent run.

    Prefers the durable store (cross-instance cooldown, daily budget, kill
    switch). The in-process cooldown below is only a fallback: it resets on
    every cold start, so with min-instances=0 and maxScale>1 it was really a
    per-instance limit, not a service-wide one.

    apply_cooldown=False for operator-initiated runs: the storm cooldown is
    there to stop an alert storm becoming a billing storm, not to stop a human
    pressing the button twice. The daily budget and kill switch still apply.
    """
    allowed, reason = state_store.reserve_run(
        now, cooldown_override=None if apply_cooldown else 0.0
    )
    if reason not in ("disabled", "unavailable"):
        return allowed, reason
    # Store off or unreachable -> degrade to the historical in-process cooldown
    # rather than taking the agent offline.
    if not apply_cooldown:
        return True, reason
    if now - _last_auto_trigger["ts"] < _AUTO_COOLDOWN_S:
        return False, "cooldown"
    _last_auto_trigger["ts"] = now
    return True, reason

# In-process broadcast fabric for the persistent /events SSE channel.
# NOTE: this reaches only consoles connected to the SAME Cloud Run instance. The
# demo runs a single instance (min-instances=0 + a ~5-min warm-ping), which is
# sufficient; a multi-instance deploy would need a shared bus (Pub/Sub/Redis).
_console_subscribers: list[asyncio.Queue] = []
_SUBSCRIBER_QUEUE_MAXSIZE = 100

# A /events connection is closed by the server after this long, so that no single
# connection can hold an instance for the full Cloud Run request timeout (3600s).
# Before this existed, one open console tab cost ~85,000 billable instance-seconds
# per day: the generator never returned, so every connection ran the full 3600s
# and EventSource immediately reconnected.
_SSE_MAX_LIFETIME_S = 900.0
# Closed earlier if nothing at all was broadcast for this long. Heartbeats do not
# count as activity. Pairs with the console's own idle rule: an unattended tab
# stops reconnecting, so an abandoned console costs nothing.
_SSE_IDLE_CLOSE_S = 600.0

# Replay buffer so the deliberate closes above are invisible to the console:
# on reconnect EventSource sends Last-Event-ID and we resend what it missed.
_EVENT_RING_MAX = 200
_EVENT_RING_TTL_S = 900.0
_event_ring: collections.deque = collections.deque(maxlen=_EVENT_RING_MAX)
_event_seq = 0


def _broadcast(ev: dict) -> None:
    """Fan an event out to every /events subscriber. Never raises into the caller.

    Drops the event for any full/broken queue rather than blocking a slow console
    (bounded queue + drop-on-full), so one dead client cannot stall a broadcast."""
    global _event_seq
    _event_seq += 1
    seq = _event_seq
    _event_ring.append((seq, time.time(), ev))
    for q in list(_console_subscribers):
        try:
            q.put_nowait((seq, ev))
        except Exception:  # noqa: BLE001 - QueueFull or a torn-down queue: skip it
            pass


def _parse_last_event_id(raw: str | None) -> int:
    """Last-Event-ID -> int. Anything unparseable means "replay nothing"."""
    if not raw:
        return 0
    try:
        return max(0, int(raw.strip()))
    except (TypeError, ValueError):
        return 0


def _replay_since(last_id: int, now: float) -> list[tuple[int, dict]]:
    """Buffered events newer than last_id that have not aged out.

    last_id == 0 (fresh connection) replays nothing: a new console renders its
    state from /console-meta, and replaying a stale run would be confusing."""
    if last_id <= 0:
        return []
    return [
        (seq, ev)
        for seq, ts, ev in list(_event_ring)
        if seq > last_id and now - ts <= _EVENT_RING_TTL_S
    ]


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Persistent, read-only SSE channel: broadcasts agent-run events (from any source,
    e.g. a Pub/Sub-triggered autonomous run) to every open console on this instance.

    Read-only observation -> intentionally unauthenticated (EventSource cannot send
    headers). Each connection gets a bounded queue; a heartbeat comment keeps the
    connection alive, and the queue is always removed on disconnect (no leak).

    The connection is bounded (_SSE_MAX_LIFETIME_S / _SSE_IDLE_CLOSE_S) so it can
    never run to the 3600s Cloud Run request timeout. Reconnects resume from
    Last-Event-ID, so the console does not miss events across a close."""
    last_id = _parse_last_event_id(request.headers.get("last-event-id"))
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
    _console_subscribers.append(queue)

    async def gen():
        try:
            # 3s (was 60s): the server now closes connections on purpose, so a
            # long retry would leave the console blind for a minute each time.
            yield "retry: 3000\n\n"
            for seq, ev in _replay_since(last_id, time.time()):
                yield f"id: {seq}\ndata: {json.dumps(ev)}\n\n"
            started = time.monotonic()
            last_activity = started
            while True:
                now = time.monotonic()
                remaining = _SSE_MAX_LIFETIME_S - (now - started)
                if remaining <= 0:
                    break  # clean close; EventSource reconnects with Last-Event-ID
                try:
                    seq, ev = await asyncio.wait_for(
                        queue.get(), timeout=min(15.0, remaining)
                    )
                except asyncio.TimeoutError:
                    if time.monotonic() - last_activity >= _SSE_IDLE_CLOSE_S:
                        break  # nothing happened for 10 min: stop billing for it
                    yield ": ping\n\n"  # heartbeat: keep the connection warm, avoid busy-loop
                    continue
                last_activity = time.monotonic()
                yield f"id: {seq}\ndata: {json.dumps(ev)}\n\n"
        finally:
            try:
                _console_subscribers.remove(queue)
            except ValueError:  # already removed
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _verify_pubsub_oidc(request: Request) -> None:
    """Verify the Google OIDC bearer token on Pub/Sub push (default-off via AUTOSRE_PUBSUB_AUDIENCE).

    When AUTOSRE_PUBSUB_AUDIENCE is unset, returns silently -> exact current behavior (deploys never
    break). When set, requires a valid Google-signed OIDC token whose aud matches. Optionally pins the
    signing service account via AUTOSRE_PUBSUB_SA_EMAIL. Raises HTTPException(403) on any failure."""
    audience = os.environ.get("AUTOSRE_PUBSUB_AUDIENCE", "")
    if not audience:
        return  # unset -> skip = exact current behavior
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="missing bearer token")
    try:
        from google.auth.transport import requests as _gar  # lazy import (matches codebase style)
        from google.oauth2 import id_token as _idt

        # clock_skew tolerates just-minted Pub/Sub tokens on a fresh instance
        # ("Token used too early") so the first push delivery is not 403'd into
        # a multi-minute retry backoff.
        claims = _idt.verify_oauth2_token(
            auth.removeprefix("Bearer "),
            _gar.Request(),
            audience=audience,
            clock_skew_in_seconds=10,
        )
    except Exception as e:  # noqa: BLE001 - any verification failure -> reject
        print(
            json.dumps(
                {
                    "severity": "WARNING",
                    "message": f"pubsub oidc verification failed: {str(e)[:200]}",
                }
            ),
            flush=True,
        )
        raise HTTPException(status_code=403, detail="invalid token")
    expected_sa = os.environ.get("AUTOSRE_PUBSUB_SA_EMAIL", "")
    if expected_sa and not (claims.get("email") == expected_sa and claims.get("email_verified")):
        raise HTTPException(status_code=403, detail="wrong service account")


@app.post("/pubsub/incident")
async def pubsub_incident(request: Request) -> dict:
    """Pub/Sub push receiver: a Cloud Monitoring alert (target unhealthy) auto-triggers
    AutoSRE with NO human click — the agent investigates and opens a PR autonomously
    (merge + deploy still require human approval). A cooldown prevents alert storms.

    Order: OIDC verify -> tolerant envelope parse -> cooldown -> run. Verifying auth BEFORE
    stamping the cooldown prevents an unauthenticated attacker from suppressing real alerts."""
    _verify_pubsub_oidc(request)

    detail = ""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty body (curl smoke) must still succeed
        body = {}
    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, dict):  # real Pub/Sub push envelope
        data = msg.get("data") or ""
        try:
            detail = base64.b64decode(data).decode("utf-8", "replace")[:1000]
        except Exception:  # noqa: BLE001 - malformed data payload -> ignore
            detail = ""
    elif isinstance(body, dict) and body:  # legacy raw-JSON body (curl tests)
        detail = json.dumps(body)[:1000]

    now = time.time()
    allowed, reason = _claim_run_slot(now)
    if not allowed:
        # 200 on purpose: Pub/Sub retries non-2xx, and retrying a deliberate
        # rate-limit decision would be a retry storm on top of a cost guard.
        return {"status": "skipped", "reason": reason}
    from agents.agent import run_incident_events  # lazy import (matches /incident/stream)

    health_url = os.environ.get("TARGET_HEALTH_URL", "")
    incident_text = (
        "Incident auto-detected by Cloud Monitoring: the Cloud Run service 'sida-target' is unhealthy. "
        f"Its health endpoint is {health_url}. Investigate and diagnose the single root cause."
    )
    if detail:
        # Screen the (semi-trusted) alert payload for injection before splicing it
        # into the prompt (Model Armor; default-off no-op). Fail-open + annotate:
        # a flagged payload is still passed through, with a hardened warning clause.
        from agents.armor_tools import clause as _armor_clause
        from agents.armor_tools import screen_text as _armor_screen

        _armor = _armor_screen(detail, source="alert_payload")
        incident_text += f" Monitoring alert payload (verbatim): {detail}"
        incident_text += _armor_clause(_armor)
    incident_text += _video_clause(None)

    # Stream the autonomous run to every open console (in-process broadcast), then
    # still return the same ack dict shape so the Pub/Sub push ack is unaffected.
    diagnosis: dict = {}
    started = time.time()
    _broadcast({"type": "run_started", "source": "pubsub"})
    try:
        async for ev in run_incident_events(incident_text):
            if ev.get("type") == "final":
                diagnosis = _parse_diagnosis(ev["final"])
                run_cost = ev.get("cost")
                await asyncio.to_thread(
                    _record_case, diagnosis, "pubsub", "sida-target", started
                )
                _log_run_cost(run_cost, "pubsub")
                _broadcast(
                    {
                        "type": "final",
                        "outcome": _classify_outcome(diagnosis),
                        "diagnosis": diagnosis,
                        "raw_final": ev["final"],
                        "cost": run_cost,
                    }
                )
            else:
                _broadcast(ev)  # tool_call / tool_result pass through unchanged
    except Exception as e:  # noqa: BLE001 - never 500 a Pub/Sub push; always give
        # subscribers a terminal event (else the console spinner hangs forever) and
        # return a JSON ack so Pub/Sub does not retry-storm a run that will re-fail.
        _broadcast({"type": "error", "error": str(e)})
        _broadcast({"type": "done"})
        return {"status": "error", "error": str(e)[:300]}
    # The 2am path: this handler had no human in it before the approval gate,
    # and with a promoted class it now has none after it either. Broadcast the
    # audit so any open console renders the post-hoc record live.
    autonomy_audit = await asyncio.to_thread(
        _maybe_execute_autonomously,
        diagnosis,
        "sida-target",
        "pubsub",
        lambda step: _broadcast({"type": "autonomy_step", **step}),
    )
    if autonomy_audit is not None:
        _broadcast({"type": "autonomy", "audit": autonomy_audit})
    _broadcast({"type": "done"})

    response = {
        "status": "handled",
        "outcome": _classify_outcome(diagnosis),
        "diagnosis": diagnosis,
    }
    if autonomy_audit is not None:
        response["autonomy"] = autonomy_audit
    return response
