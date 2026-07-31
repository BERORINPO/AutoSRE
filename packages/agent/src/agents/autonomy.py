"""Earned autonomy — the approval gate that retires itself, per class, with receipts.

The human approval gate has a contradiction at its center: the product's premise
is "2am, nobody is on call", yet recovery waits for a human click. This module
resolves it without discarding the gate. Autonomy is not claimed, it is EARNED:

- Every human-approved recovery already records whether the fix actually worked
  (case_store.record_resolution). That verified track record, grouped by
  remediation class (which env var was restored), is the trust ledger.
- A class is PROMOTED to autonomous execution only when the Wilson lower bound
  of its verified success rate clears a threshold. Not the raw rate - the lower
  bound, so a lucky 3/3 cannot open the gate that 30/30 has to earn.
- One verified failure DEMOTES the class immediately and it stays demoted until
  a human re-arms it. Promotion is statistical; demotion is instant.

The gate does not disappear: it moves. Before promotion the human approves each
action; after promotion the human audits outcomes. Unpromoted classes, actions
outside the allowlist, and anything non-reversible stay behind the gate forever.

Default-off contract (matches the codebase's staged-enablement pattern):
  AUTOSRE_AUTONOMY_ENABLED    unset/empty = feature off: the ledger stays
                              readable (it is just reporting), but nothing is
                              ever executed autonomously.
  AUTOSRE_AUTONOMY_THRESHOLD  Wilson lower-bound needed to promote (default 0.80).

Deliberately stdlib-only in its decision core (wilson_lower_bound, evaluate_class)
so the offline smoke gate can test the arithmetic without any SDK installed,
same as agents.limits and agents.cost.
"""
import json
import math
import os

_ENV_ENABLED = "AUTOSRE_AUTONOMY_ENABLED"
_ENV_THRESHOLD = "AUTOSRE_AUTONOMY_THRESHOLD"

DEFAULT_THRESHOLD = 0.80
# z for a 95% one-sided-ish interval; matches the eval harness's Wilson usage.
_Z = 1.96


def enabled() -> bool:
    return bool(os.environ.get(_ENV_ENABLED, "").strip())


def threshold() -> float:
    raw = os.environ.get(_ENV_THRESHOLD, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if 0.0 < value < 1.0 else DEFAULT_THRESHOLD


def class_key(env_var: str) -> str:
    """One trust class per remediation, today: restoring one allowlisted env var."""
    return f"restore_env:{env_var}"


def wilson_lower_bound(successes: int, attempts: int, z: float = _Z) -> float:
    """Lower bound of the Wilson score interval. 0.0 when there is no evidence.

    Chosen over the raw rate for the same reason the eval harness uses it: with
    small n the raw rate flatters. 3/3 has a lower bound of ~0.44, 16/16 ~0.81,
    35/35 ~0.90 - the gate opens on evidence volume, not on a hot streak.
    """
    if attempts <= 0:
        return 0.0
    successes = max(0, min(successes, attempts))
    p = successes / attempts
    z2 = z * z
    denom = 1.0 + z2 / attempts
    center = p + z2 / (2.0 * attempts)
    margin = z * math.sqrt(p * (1.0 - p) / attempts + z2 / (4.0 * attempts * attempts))
    return max(0.0, (center - margin) / denom)


def evaluate_class(
    successes: int, attempts: int, demoted: dict | None, thr: float
) -> dict:
    """Pure promotion decision for one class. No I/O - this is what smoke tests drive.

    demoted is the per-class demotion record from the durable state object
    (None/{} = not demoted). A demotion always wins over any statistics: the
    ledger can say 99% while the class stays locked, because the failure that
    demoted it is exactly the evidence the ledger has not absorbed yet.
    """
    lb = wilson_lower_bound(successes, attempts)
    is_demoted = bool((demoted or {}).get("ts"))
    return {
        "successes": successes,
        "attempts": attempts,
        "wilson_lower_bound": round(lb, 4),
        "threshold": thr,
        "demoted": is_demoted,
        "demotion": (demoted or None) if is_demoted else None,
        "promoted": (lb >= thr) and not is_demoted,
    }


def _log(level: str, event: str, **fields) -> None:
    print(json.dumps({"severity": level, "event": event, **fields}), flush=True)


def ledger(demotions: dict | None = None) -> dict:
    """The trust ledger: per-class verified track record + promotion status.

    Read-only and honest by construction - rows come from the same BigQuery
    case rows the learning loop records; nothing here is self-reported by the
    agent at pitch time. Never raises: a ledger that cannot be read reports
    available=false, and every caller treats that as "nothing is promoted"
    (fail-closed to the human gate).
    """
    from agents import case_store

    thr = threshold()
    base = {"enabled": enabled(), "threshold": thr, "classes": []}
    if not case_store.enabled():
        return {**base, "available": False, "reason": "case memory disabled"}
    try:
        rows = case_store.resolution_stats_by_env_var()
    except Exception as e:  # noqa: BLE001 - reporting must never take the agent down
        _log("WARNING", "trust_ledger_unavailable", error=f"{type(e).__name__}: {e}")
        return {**base, "available": False, "reason": f"{type(e).__name__}: {e}"}
    demotions = demotions or {}
    classes = []
    for row in rows:
        key = class_key(row["env_var"])
        verdict = evaluate_class(
            int(row["successes"]), int(row["attempts"]), demotions.get(key), thr
        )
        classes.append({"class": key, "env_var": row["env_var"], **verdict})
    classes.sort(key=lambda c: (-c["attempts"], c["class"]))
    return {**base, "available": True, "classes": classes}


def promoted_for(env_var: str, demotions: dict | None = None) -> tuple[bool, dict]:
    """(may_act_autonomously, evidence) for one env var. Fail-closed on any doubt."""
    if not enabled():
        return False, {"reason": "autonomy disabled"}
    book = ledger(demotions)
    if not book.get("available"):
        return False, {"reason": book.get("reason", "ledger unavailable")}
    key = class_key(env_var)
    for cls in book["classes"]:
        if cls["class"] == key:
            return bool(cls["promoted"]), cls
    return False, {"reason": "no verified track record for this class"}
