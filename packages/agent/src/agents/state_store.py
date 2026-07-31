"""Durable run state: cooldown, daily run budget, and a kill switch.

Default-off. With AUTOSRE_STATE_URI unset every call is a no-op and the caller
keeps its existing in-process behaviour.

Why this exists
---------------
The auto-trigger cooldown lived in a module global (`server._last_auto_trigger`).
Cloud Run runs with min-instances=0 and maxScale>1, so a cold start reset it and
each instance kept its own copy: the intended ceiling of one run per 5 minutes
was really one run per 5 minutes *per instance*. With maxScale=20 that is 5,760
runs/day, not 288. A cost guard that resets whenever the platform scales is not
a guard.

State lives in a single small GCS object, updated with a generation
precondition (compare-and-swap) so two instances cannot both spend the same
budget slot.

Failure policy (deliberately not uniform)
----------------------------------------
- Backend unreachable -> **allow**, and tell the caller so it can fall back to
  its in-process cooldown. That is exactly the pre-existing behaviour, so a GCS
  blip degrades the guard rather than taking the agent offline. Refusing every
  incident because bookkeeping is unavailable is the wrong failure for an
  on-call agent, and the per-run ceiling (agents.limits) still applies.
- Kill switch known to be tripped -> **deny**. An explicit stop wins.
- Repeated CAS conflicts -> **deny**. A conflict means another instance is
  starting a run right now, which is the thing being rate-limited.
"""
import json
import os
import time
from datetime import datetime, timezone

_ENV_URI = "AUTOSRE_STATE_URI"  # gs://bucket/object.json
_ENV_COOLDOWN = "AUTOSRE_AUTO_COOLDOWN_S"
_ENV_DAILY_LIMIT = "AUTOSRE_DAILY_RUN_LIMIT"

DEFAULT_COOLDOWN_S = 300.0
DEFAULT_DAILY_LIMIT = 50
_CAS_ATTEMPTS = 3
_IO_TIMEOUT_S = 10.0

STATE_VERSION = 1


def uri() -> str:
    return os.environ.get(_ENV_URI, "").strip()


def enabled() -> bool:
    return uri().startswith("gs://")


def cooldown_s() -> float:
    raw = os.environ.get(_ENV_COOLDOWN, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_COOLDOWN_S
    except ValueError:
        return DEFAULT_COOLDOWN_S
    return value if value >= 0 else DEFAULT_COOLDOWN_S


def daily_limit() -> int:
    raw = os.environ.get(_ENV_DAILY_LIMIT, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_DAILY_LIMIT
    except ValueError:
        return DEFAULT_DAILY_LIMIT
    return value if value > 0 else DEFAULT_DAILY_LIMIT


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "last_run_ts": 0.0,
        "day": "",
        "runs_today": 0,
        "killswitch": {"tripped": False, "reason": "", "ts": 0.0},
    }


def day_key(now: float) -> str:
    """UTC day bucket. UTC (not JST) so the rollover cannot land mid-demo."""
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def evaluate(state: dict, now: float, cooldown: float, limit: int) -> tuple[bool, str, dict]:
    """Pure decision + next state. No I/O, so this is what the smoke test drives.

    Returns (allowed, reason, next_state). next_state is only meaningful when
    allowed, except for the auto-trip case where the kill switch is recorded.
    """
    state = {**empty_state(), **(state or {})}
    ks = {**empty_state()["killswitch"], **(state.get("killswitch") or {})}
    state["killswitch"] = ks

    if ks.get("tripped"):
        return False, "killswitch", state

    today = day_key(now)
    if state.get("day") != today:  # new UTC day: budget resets
        state = {**state, "day": today, "runs_today": 0}

    last = float(state.get("last_run_ts") or 0.0)
    if cooldown > 0 and last > 0 and (now - last) < cooldown:
        return False, "cooldown", state

    used = int(state.get("runs_today") or 0)
    if used >= limit:
        # Self-trip: the agent stops itself rather than spending past its budget,
        # and stays stopped until a human clears it.
        tripped = {
            "tripped": True,
            "reason": f"daily run limit reached ({used}/{limit})",
            "ts": now,
        }
        return False, "daily_limit", {**state, "killswitch": tripped}

    return True, "ok", {**state, "last_run_ts": now, "runs_today": used + 1}


# --------------------------------------------------------------------- GCS I/O
def _split_uri(gs_uri: str) -> tuple[str, str]:
    rest = gs_uri[len("gs://") :]
    bucket, _, obj = rest.partition("/")
    return bucket, obj


def _blob():
    from google.cloud import storage  # lazy: keeps startup fast, optional dep path

    bucket_name, obj = _split_uri(uri())
    if not bucket_name or not obj:
        raise ValueError(f"malformed {_ENV_URI}: {uri()!r}")
    client = storage.Client()
    return client.bucket(bucket_name).blob(obj)


def _read(blob) -> tuple[dict, int | None]:
    """(state, generation). generation None means "object does not exist yet".

    reload() first: download_as_bytes alone does not reliably populate
    blob.generation, and the generation is the whole point — it is the CAS
    token. Reading at that exact generation also rules out a torn read.
    """
    try:
        blob.reload(timeout=_IO_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "NotFound":
            return empty_state(), None
        raise
    generation = blob.generation
    raw = blob.download_as_bytes(if_generation_match=generation, timeout=_IO_TIMEOUT_S)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Corrupt object: treat as fresh rather than wedging every run forever.
        return empty_state(), generation
    return state, generation


def _write(blob, state: dict, generation: int | None) -> None:
    """CAS write. generation None -> only create if absent (ifGenerationMatch=0)."""
    blob.upload_from_string(
        json.dumps(state, separators=(",", ":")),
        content_type="application/json",
        if_generation_match=generation if generation is not None else 0,
        timeout=_IO_TIMEOUT_S,
    )


def reserve_run(
    now: float | None = None, cooldown_override: float | None = None
) -> tuple[bool, str]:
    """Atomically claim a run slot.

    cooldown_override=0 skips the storm cooldown while still charging the daily
    budget and honouring the kill switch. Operator-initiated runs use it: a
    human pressing the button twice inside five minutes is intended, an alert
    storm firing twice is not.

    Returns (allowed, reason). reason is one of:
      "disabled"   - store not configured; caller keeps its in-process behaviour
      "unavailable"- backend error; allowed, caller falls back in-process
      "ok" | "cooldown" | "daily_limit" | "killswitch" | "conflict"
    """
    if not enabled():
        return True, "disabled"
    now = time.time() if now is None else now
    cooldown = cooldown_s() if cooldown_override is None else max(0.0, cooldown_override)
    limit = daily_limit()
    try:
        blob = _blob()
    except Exception:  # noqa: BLE001 - misconfiguration must not take the agent down
        return True, "unavailable"

    for _ in range(_CAS_ATTEMPTS):
        try:
            state, generation = _read(blob)
        except Exception:  # noqa: BLE001
            return True, "unavailable"
        allowed, reason, next_state = evaluate(state, now, cooldown, limit)
        if not allowed and reason != "daily_limit":
            return False, reason
        try:
            _write(blob, next_state, generation)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PreconditionFailed":
                continue  # another instance won the race; re-read and re-decide
            return (True, "unavailable") if allowed else (False, reason)
        return allowed, reason
    return False, "conflict"


def read_state() -> dict:
    """Best-effort snapshot for the console. Never raises."""
    if not enabled():
        return {**empty_state(), "enabled": False}
    try:
        state, _ = _read(_blob())
    except Exception:  # noqa: BLE001
        return {**empty_state(), "enabled": True, "available": False}
    return {**empty_state(), **state, "enabled": True, "available": True}


def read_demotions() -> dict:
    """Per-class autonomy demotion records ({class_key: {ts, reason}}). Never raises.

    An unreadable store returns {} — but autonomy's callers pair this with the
    ledger read, which fails closed on its own; a missing demotion record can
    only matter when the ledger IS readable, and both live in the same account,
    so the split-brain window is the GCS blip itself. Accepted for demo scale.
    """
    state = read_state()
    demoted = ((state.get("autonomy") or {}).get("demoted")) or {}
    return demoted if isinstance(demoted, dict) else {}


def set_demotion(class_key: str, reason: str = "", now: float | None = None) -> bool:
    """Demote one autonomy class (empty reason with clear=True semantics is below).

    Demotion is the one-strike rule: it must land durably before anyone reads
    the ledger again, hence the same CAS discipline as the kill switch. Returns
    False on any failure — the caller treats an unrecorded demotion as fatal
    and refuses further autonomous action in-process.
    """
    return _mutate_demotions(lambda d: {**d, class_key: {"ts": now or time.time(), "reason": reason}})


def clear_demotion(class_key: str) -> bool:
    """Human re-arm: remove one class's demotion record."""
    return _mutate_demotions(lambda d: {k: v for k, v in d.items() if k != class_key})


def _mutate_demotions(fn) -> bool:
    if not enabled():
        return False
    try:
        blob = _blob()
    except Exception:  # noqa: BLE001
        return False
    for _ in range(_CAS_ATTEMPTS):
        try:
            state, generation = _read(blob)
        except Exception:  # noqa: BLE001
            return False
        state = {**empty_state(), **state}
        autonomy = dict(state.get("autonomy") or {})
        demoted = autonomy.get("demoted") or {}
        autonomy["demoted"] = fn(demoted if isinstance(demoted, dict) else {})
        state["autonomy"] = autonomy
        try:
            _write(blob, state, generation)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PreconditionFailed":
                continue
            return False
        return True
    return False


def set_killswitch(tripped: bool, reason: str = "", now: float | None = None) -> bool:
    """Trip or clear the kill switch. Returns True on success."""
    if not enabled():
        return False
    now = time.time() if now is None else now
    try:
        blob = _blob()
    except Exception:  # noqa: BLE001
        return False
    for _ in range(_CAS_ATTEMPTS):
        try:
            state, generation = _read(blob)
        except Exception:  # noqa: BLE001
            return False
        state = {**empty_state(), **state}
        state["killswitch"] = {"tripped": bool(tripped), "reason": reason, "ts": now}
        try:
            _write(blob, state, generation)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PreconditionFailed":
                continue
            return False
        return True
    return False
