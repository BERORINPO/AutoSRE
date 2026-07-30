"""AutoSRE case memory — the self-improving loop (BigQuery-backed).

Every diagnosed incident is recorded as a structured "case" in BigQuery, and the
agent consults this memory at the start of the next investigation via the
recall_similar_cases tool. Past cases are surfaced to the model as HYPOTHESES to
verify against live evidence — never as proof — so the grounding discipline of
the ReAct loop is preserved while repeat incidents get diagnosed faster.

Why BigQuery (not an app-local store): AutoSRE already emits structured logs to
Cloud Logging; keeping case memory in BigQuery keeps the "move the model to the
data" property — the memory lives where operational data already lives, is
queryable with SQL, and needs zero extra serving infrastructure.

Default-off contract (matches the codebase's staged-enablement pattern):
  AUTOSRE_CASES_TABLE  "<project>.<dataset>.<table>" — unset means the feature
                       is disabled: recording becomes a no-op and the recall
                       tool reports enabled=false. Deploys never break.
"""
import datetime
import json
import os
import uuid

_RECALL_LIMIT = 5
_RECALL_WINDOW_DAYS = 90
# Streaming inserts + queries must never stall an incident run.
_BQ_TIMEOUT_S = 15.0


def _cases_table() -> str:
    return os.environ.get("AUTOSRE_CASES_TABLE", "")


def enabled() -> bool:
    return bool(_cases_table())


def _log(level: str, event: str, **fields) -> None:
    print(json.dumps({"severity": level, "event": event, **fields}), flush=True)


def _insert_row(row: dict) -> None:
    """Streaming-insert one case row. Raises on failure (callers decide policy)."""
    from google.cloud import bigquery  # lazy import (matches codebase style)

    client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    errors = client.insert_rows_json(_cases_table(), [row], timeout=_BQ_TIMEOUT_S)
    if errors:
        raise RuntimeError(f"bigquery insert_rows_json: {errors}")


def record_diagnosis(diagnosis: dict, source: str, service: str, duration_s: float) -> None:
    """Persist one diagnosed incident as a case row. Fire-and-forget: never raises.

    Called after _parse_diagnosis on every run path (manual, stream, pubsub).
    A parse-failed diagnosis (no root_cause) is not worth remembering — skip it.
    """
    if not enabled():
        return
    if not diagnosis or not diagnosis.get("root_cause"):
        return
    try:
        confidence = diagnosis.get("confidence")
        # The model occasionally emits numbers as strings; coerce or drop so a
        # type mismatch can never fail the INTEGER column insert.
        try:
            pr_number = int(diagnosis.get("pr_number"))
        except (TypeError, ValueError):
            pr_number = None
        row = {
            "kind": "diagnosis",
            "case_id": uuid.uuid4().hex,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "service": service,
            "source": source,
            "root_cause": str(diagnosis.get("root_cause") or "")[:1000],
            "missing_env_var": (str(diagnosis.get("missing_env_var"))[:200]
                                if diagnosis.get("missing_env_var") else None),
            "action": (str(diagnosis.get("action"))[:50] if diagnosis.get("action") else None),
            "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
            "pr_url": (str(diagnosis.get("pr_url"))[:500] if diagnosis.get("pr_url") else None),
            "pr_number": pr_number,
            "user_reports_summary": str(diagnosis.get("user_reports_summary") or "")[:1000],
            "duration_s": round(duration_s, 1),
        }
        _insert_row(row)
        _log("INFO", "case_recorded", kind="diagnosis", service=service, source=source,
             action=row["action"], pr_number=row["pr_number"])
    except Exception as e:  # noqa: BLE001 - memory must never break an incident run
        _log("WARNING", "case_record_failed", kind="diagnosis", error=f"{type(e).__name__}: {e}")


def record_resolution(pr_number: int, recovered: bool, duration_s: float) -> None:
    """Persist the human-approved recovery outcome for a case (joined by pr_number).

    Fire-and-forget: never raises. Called from /approve after verify_recovery.
    """
    if not enabled():
        return
    try:
        row = {
            "kind": "resolution",
            "case_id": uuid.uuid4().hex,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pr_number": pr_number,
            "recovered": bool(recovered),
            "duration_s": round(duration_s, 1),
        }
        _insert_row(row)
        _log("INFO", "case_recorded", kind="resolution", pr_number=pr_number, recovered=recovered)
    except Exception as e:  # noqa: BLE001
        _log("WARNING", "case_record_failed", kind="resolution", error=f"{type(e).__name__}: {e}")


def memory_stats() -> dict:
    """Aggregate growth counters for the console's experience panel. Never raises.

    Read-only and cheap: the cases table is day-partitioned and demo-scale, and
    the console calls this on page load + once after each /approve — not per event.
    """
    if not enabled():
        return {"enabled": False}
    try:
        from google.cloud import bigquery  # lazy import

        client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        query = f"""
            SELECT
              COUNTIF(kind = 'diagnosis') AS learned_cases,
              COUNTIF(kind = 'resolution' AND recovered) AS verified_recoveries,
              MIN(IF(kind = 'diagnosis', ts, NULL)) AS first_learned_at,
              MAX(IF(kind = 'diagnosis', ts, NULL)) AS last_learned_at
            FROM `{_cases_table()}`
        """
        row = next(iter(client.query(query).result(timeout=_BQ_TIMEOUT_S)))
        return {
            "enabled": True,
            "ok": True,
            "learned_cases": int(row["learned_cases"] or 0),
            "verified_recoveries": int(row["verified_recoveries"] or 0),
            "first_learned_at": row["first_learned_at"].isoformat() if row["first_learned_at"] else None,
            "last_learned_at": row["last_learned_at"].isoformat() if row["last_learned_at"] else None,
        }
    except Exception as e:  # noqa: BLE001 - a stats outage must not break the console
        _log("WARNING", "memory_stats_failed", error=f"{type(e).__name__}: {e}")
        return {"enabled": True, "ok": False, "error": f"{type(e).__name__}: {e}"}


def resolution_stats_by_env_var() -> list[dict]:
    """Verified track record per remediation class, for the trust ledger.

    Joins each diagnosis (which env var was restored) to its resolution row
    (did the human-approved fix actually recover the service) by pr_number.
    Only diagnoses that reached a recorded resolution count as attempts:
    an unapproved PR is not evidence in either direction.

    Raises on failure — the caller (agents.autonomy.ledger) owns the policy of
    treating an unreadable ledger as "nothing is promoted".
    """
    if not enabled():
        return []
    from google.cloud import bigquery  # lazy import

    client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    # One row per PR on each side before joining: replayed demos may record a
    # diagnosis twice, and a duplicated join row would double-count evidence.
    query = f"""
        WITH diag AS (
          SELECT pr_number, ANY_VALUE(missing_env_var) AS env_var
          FROM `{_cases_table()}`
          WHERE kind = 'diagnosis' AND pr_number IS NOT NULL
            AND missing_env_var IS NOT NULL
          GROUP BY pr_number
        ),
        res AS (
          -- LOGICAL_AND: if any resolution attempt for a PR failed, the PR
          -- counts as a failure. Trust arithmetic must be pessimistic.
          SELECT pr_number, LOGICAL_AND(recovered) AS recovered
          FROM `{_cases_table()}`
          WHERE kind = 'resolution' AND pr_number IS NOT NULL
          GROUP BY pr_number
        )
        SELECT diag.env_var, COUNTIF(res.recovered) AS successes, COUNT(*) AS attempts
        FROM diag JOIN res ON res.pr_number = diag.pr_number
        GROUP BY diag.env_var
    """
    rows = client.query(query).result(timeout=_BQ_TIMEOUT_S)
    return [
        {"env_var": r["env_var"], "successes": int(r["successes"] or 0),
         "attempts": int(r["attempts"] or 0)}
        for r in rows
    ]


def recall_similar_cases(service_name: str) -> dict:
    """Recall AutoSRE's own past incident cases for a service, newest first.

    Returns previous diagnoses (root cause, missing env var, action, confidence,
    PR) joined with their human-approved recovery outcome where one exists.
    Treat past cases ONLY as hypotheses to verify against live evidence (logs and
    config) — never as proof.

    Args:
        service_name: the Cloud Run service name, e.g. "sida-target".
    """
    if not enabled():
        return {
            "ok": True,
            "enabled": False,
            "cases": [],
            "note": "case memory is not configured (AUTOSRE_CASES_TABLE unset); proceed normally",
        }
    try:
        from google.cloud import bigquery  # lazy import

        client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        # Table id is validated server-side by BigQuery; parameters cover user input.
        query = f"""
            SELECT
              d.ts, d.source, d.root_cause, d.missing_env_var, d.action,
              d.confidence, d.pr_url, d.pr_number, d.user_reports_summary,
              d.duration_s,
              r.recovered AS resolution_recovered,
              r.duration_s AS resolution_duration_s
            FROM `{_cases_table()}` AS d
            LEFT JOIN `{_cases_table()}` AS r
              ON r.kind = 'resolution' AND r.pr_number = d.pr_number
              -- prune the join side to the same partition window (else the
              -- self-join scans the whole table as it grows)
              AND r.ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
            WHERE d.kind = 'diagnosis'
              AND d.service = @service
              AND d.ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
            ORDER BY d.ts DESC
            LIMIT @limit
        """
        job = client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("service", "STRING", service_name),
                    bigquery.ScalarQueryParameter("days", "INT64", _RECALL_WINDOW_DAYS),
                    bigquery.ScalarQueryParameter("limit", "INT64", _RECALL_LIMIT),
                ]
            ),
        )
        cases = []
        for r in job.result(timeout=_BQ_TIMEOUT_S):
            resolved = r["resolution_recovered"]
            cases.append(
                {
                    "when": r["ts"].isoformat() if r["ts"] else None,
                    "source": r["source"],
                    "root_cause": r["root_cause"],
                    "missing_env_var": r["missing_env_var"],
                    "action": r["action"],
                    "confidence": r["confidence"],
                    "pr_url": r["pr_url"],
                    "pr_number": r["pr_number"],
                    "user_reports_summary": r["user_reports_summary"],
                    "diagnosis_duration_s": r["duration_s"],
                    # "verified_recovered" is the strongest signal: a human approved
                    # the fix and the 503->200 verification succeeded afterwards.
                    "outcome": (
                        "verified_recovered"
                        if resolved
                        else ("recovery_failed" if resolved is False else "unresolved_or_pending")
                    ),
                }
            )
        _log("INFO", "case_recalled", service=service_name, count=len(cases))
        return {
            "ok": True,
            "enabled": True,
            "count": len(cases),
            "cases": cases,
            "reminder": "past cases are hypotheses; verify against live logs/config before concluding",
        }
    except Exception as e:  # noqa: BLE001 - a memory outage must not block diagnosis
        _log("WARNING", "case_recall_failed", error=f"{type(e).__name__}: {e}")
        return {"ok": False, "enabled": True, "cases": [], "error": f"{type(e).__name__}: {e}"}
