"""AutoSRE recovery — the human-gated actions.

After a human approves, AutoSRE: (1) merges the fix PR, (2) applies the restored
env var to the target Cloud Run service, and (3) verifies the target is healthy
again. Merge + apply + redeploy are exactly the steps that require approval.
"""
import base64
import os
import time

import httpx

API = "https://api.github.com"
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
RUN_REGION = os.environ.get("RUN_REGION", "asia-northeast1")


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def merge_pull_request(pr_number: int) -> dict:
    """Merge the AutoSRE fix PR (squash)."""
    repo = os.environ.get("GITHUB_TARGET_REPO", "")
    try:
        with httpx.Client(base_url=API, headers=_gh_headers(), timeout=20.0) as c:
            r = c.put(
                f"/repos/{repo}/pulls/{pr_number}/merge", json={"merge_method": "squash"}
            )
            r.raise_for_status()
            data = r.json()
            return {"ok": True, "merged": data.get("merged", True), "sha": data.get("sha")}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"GitHub {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def apply_env_fix(service_name: str, env_var: str, value: str) -> dict:
    """Add/update an env var on the target Cloud Run service (deploys a new revision)."""
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{PROJECT}/locations/{RUN_REGION}/services/{service_name}"
        svc = client.get_service(name=name)
        container = svc.template.containers[0]
        for e in container.env:
            if e.name == env_var:
                e.value = value
                break
        else:
            container.env.append(run_v2.EnvVar(name=env_var, value=value))
        op = client.update_service(service=svc)
        op.result(timeout=300)
        return {"ok": True, "service": service_name, "applied_env_var": env_var}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def rehearse_env_fix(service_name: str, env_var: str, value: str) -> dict:
    """Prove the fix on real infrastructure before anyone commits to it.

    Deploys the fixed configuration as a NEW Cloud Run revision that receives
    zero production traffic (current serving revision stays pinned at 100%),
    then exposes it via a "rehearsal" tag URL so its health can be checked
    directly. The evidence upgrade this buys: an approval card can say "this
    fix already ran next door, here is the green health check" instead of
    "the agent believes this is correct".

    Two update calls by design: the first (template change + pin) creates the
    rehearsal revision; the second is traffic-only (template untouched, so no
    extra revision) and attaches the tag once the revision name is known.

    Returns a snapshot for the undo contract: the previously serving revision
    (traffic can be pinned back to it instantly) and the previous env value.
    """
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{PROJECT}/locations/{RUN_REGION}/services/{service_name}"
        svc = client.get_service(name=name)
        prev_revision = (svc.latest_ready_revision or "").rsplit("/", 1)[-1]
        if not prev_revision:
            return {"ok": False, "error": "no ready revision to pin traffic to"}
        container = svc.template.containers[0]
        prev_value = next((e.value for e in container.env if e.name == env_var), None)
        for e in container.env:
            if e.name == env_var:
                e.value = value
                break
        else:
            container.env.append(run_v2.EnvVar(name=env_var, value=value))
        pin = run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=prev_revision,
            percent=100,
        )
        svc.traffic = [pin]
        op = client.update_service(service=svc)
        op.result(timeout=300)

        svc = client.get_service(name=name)
        rehearsal_revision = (svc.latest_ready_revision or "").rsplit("/", 1)[-1]
        if rehearsal_revision == prev_revision:
            return {"ok": False, "error": "rehearsal revision was not created"}
        svc.traffic = [
            pin,
            run_v2.TrafficTarget(
                type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                revision=rehearsal_revision,
                percent=0,
                tag="rehearsal",
            ),
        ]
        op = client.update_service(service=svc)
        op.result(timeout=300)

        svc = client.get_service(name=name)
        url = next((t.uri for t in svc.traffic_statuses if t.tag == "rehearsal"), "")
        if not url:
            return {"ok": False, "error": "rehearsal tag URL not exposed"}
        return {
            "ok": True,
            "service": service_name,
            "rehearsal_revision": rehearsal_revision,
            "rehearsal_url": url,
            "snapshot": {"prev_revision": prev_revision, "prev_value": prev_value},
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def promote_rehearsal(service_name: str) -> dict:
    """Ship the rehearsed fix: route 100% of traffic to latest (the rehearsal revision).

    LATEST rather than a pin on the revision name, deliberately: the rehearsal
    revision IS latest_ready here, and leaving the service on LATEST keeps
    every future deploy (gcloud, terraform, apply_env_fix) behaving normally.
    A pinned service silently ignores new revisions - that is the drift trap.
    Traffic-only change: the template is untouched, so no new revision.
    """
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{PROJECT}/locations/{RUN_REGION}/services/{service_name}"
        svc = client.get_service(name=name)
        svc.traffic = [
            run_v2.TrafficTarget(
                type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                percent=100,
            )
        ]
        op = client.update_service(service=svc)
        op.result(timeout=300)
        return {"ok": True, "service": service_name, "serving": "latest"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def revert_traffic(service_name: str, prev_revision: str) -> dict:
    """The undo contract: pin 100% of traffic back to the pre-fix revision.

    Instant (no build, no deploy) - this is why the whole scheme qualifies as
    reversible. NOTE the pin is deliberate and must be cleared by a human
    later (promote_rehearsal or a normal deploy + traffic reset): after a
    failed autonomous action, "nothing moves until a person looks" is the
    correct resting state, not a return to LATEST.
    """
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{PROJECT}/locations/{RUN_REGION}/services/{service_name}"
        svc = client.get_service(name=name)
        svc.traffic = [
            run_v2.TrafficTarget(
                type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                revision=prev_revision,
                percent=100,
            )
        ]
        op = client.update_service(service=svc)
        op.result(timeout=300)
        return {"ok": True, "service": service_name, "pinned_to": prev_revision}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def verify_recovery(health_url: str, timeout_s: int = 150) -> dict:
    """Poll the target health URL until it returns 200 (recovered) or the timeout elapses."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(health_url, timeout=10.0)
            last = r.status_code
            if r.status_code == 200:
                return {"ok": True, "recovered": True, "status_code": 200, "body": r.text[:200]}
        except Exception as e:  # noqa: BLE001
            last = f"error: {e}"
        time.sleep(5)
    return {"ok": True, "recovered": False, "last_status": last}


def inject_failure(service_name: str, env_var: str) -> dict:
    """Remove an env var from the target Cloud Run service (re-arms the incident)."""
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{PROJECT}/locations/{RUN_REGION}/services/{service_name}"
        svc = client.get_service(name=name)
        container = svc.template.containers[0]
        keep = [
            run_v2.EnvVar(name=e.name, value=e.value)
            for e in container.env
            if e.name != env_var
        ]
        del container.env[:]
        container.env.extend(keep)
        op = client.update_service(service=svc)
        op.result(timeout=300)
        return {"ok": True, "removed": env_var}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def reset_repo_config() -> dict:
    """Reset the config repo file to the broken state (DATABASE_URL absent)."""
    repo = os.environ.get("GITHUB_TARGET_REPO", "")
    path = os.environ.get("TARGET_CONFIG_PATH", "deploy/target-service.env")
    broken = (
        "# Deploy configuration for the sida-target Cloud Run service.\n"
        "# NOTE: DATABASE_URL is required by the app at runtime but is currently absent.\n"
        "# This is the incident AutoSRE detects and fixes via a pull request.\n"
        "SERVICE_NAME=sida-target\nREGION=asia-northeast1\nLOG_LEVEL=info\n"
    )
    try:
        with httpx.Client(base_url=API, headers=_gh_headers(), timeout=20.0) as c:
            f = c.get(f"/repos/{repo}/contents/{path}").raise_for_status().json()
            c.put(
                f"/repos/{repo}/contents/{path}",
                json={
                    "message": "chore: reset demo state (remove DATABASE_URL)",
                    "content": base64.b64encode(broken.encode("utf-8")).decode("ascii"),
                    "sha": f["sha"],
                },
            ).raise_for_status()
        return {"ok": True}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"GitHub {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
