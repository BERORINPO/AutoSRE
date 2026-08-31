"""The incident prompt: one wording, three adapters.

The text that starts a run used to be built inline at every entry point
(/incident, /incident/stream, the Pub/Sub push receiver). Three copies of a
prompt the eval baseline is measured against is a drift hazard: a wording
"cleanup" at one call site silently makes the console and the 2am path
different agents. A CLI would have been the fourth copy.

Stdlib-only and free of any FastAPI/ADK import, like agents.limits,
agents.cost and agents.diagnosis, so the offline smoke gate can pin the exact
strings without the SDK or a running app.
"""
import os
import re

# Two source wordings, kept verbatim from the call sites they replaced: a
# human/CLI-initiated run says "is reported unhealthy", the Cloud Monitoring
# push says an alert detected it. Changing either changes the prompt the eval
# baseline (90.2%, n=51) was measured on.
_SOURCE_HEADS = {
    "manual": "Incident: the Cloud Run service '{service}' is reported unhealthy. ",
    "alert": (
        "Incident auto-detected by Cloud Monitoring: the Cloud Run service "
        "'{service}' is unhealthy. "
    ),
}


def build_incident_text(service_name: str, health_url: str, source: str = "manual") -> str:
    """The base incident prompt. Callers append their own clauses, in their order.

    The Pub/Sub path appends the (armor-screened) alert payload before the video
    clause, so this deliberately returns only the head - ordering stays with the
    caller that owns it.
    """
    head = _SOURCE_HEADS.get(source, _SOURCE_HEADS["manual"]).format(service=service_name)
    return head + (
        f"Its health endpoint is {health_url}. Investigate and diagnose the single root cause."
    )


def video_clause(video_ref: str | None) -> str:
    """Append a screen-recording hint to the incident prompt when one is available.

    Precedence: an explicit per-request video_ref, else AUTOSRE_REPORT_VIDEO_URI
    (the demo default). Empty on both -> "" -> the agent never calls the video
    tool = current behavior. Keeps the video feature staged/default-off.
    """
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
