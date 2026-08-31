"""GitHub PR tool for AutoSRE.

open_pull_request() opens a REAL pull request that fixes the incident by restoring
a missing environment variable in the target deploy-config repo. This is the
load-bearing DevOps artifact and a permanent, clickable proof for the demo.

Opening the PR is autonomous; merging + redeploying is human-gated (not here).

Env:
  GITHUB_TOKEN        classic PAT with 'repo' scope (mounted from Secret Manager)
  GITHUB_TARGET_REPO  "owner/name", e.g. "BERORINPO/sida-target-config"
  TARGET_CONFIG_PATH  path to the env file (default "deploy/target-service.env")
"""
import base64
import os

import httpx

API = "https://api.github.com"
CONFIG_PATH = os.environ.get("TARGET_CONFIG_PATH", "deploy/target-service.env")
# Safety guard: the agent may only open PRs that restore an env var on this allowlist.
# This stops a hallucinated or prompt-injected "fix" (e.g. SECRET_KEY) from ever
# becoming a real PR, no matter what the model outputs.
def allowed_env_vars() -> set:
    """The remediation allowlist, read from env at CALL time (not import time).

    Production sets AUTOSRE_ALLOWED_ENV_VARS once at deploy, so behavior there is
    identical to a module constant. Call-time reading matters for eval REAL mode,
    where scenario-scoped env (e.g. S02's extended allowlist) must reach both this
    guard and the agent instruction built from it.

    NOTE: this guard is the injection backstop. If a code path is ever added that
    sets AUTOSRE_ALLOWED_ENV_VARS from user-controllable input mid-process,
    re-evaluate call-time reading here first (an attacker could widen the set).
    """
    return {
        v.strip()
        for v in os.environ.get("AUTOSRE_ALLOWED_ENV_VARS", "DATABASE_URL").split(",")
        if v.strip()
    }


def dry_run() -> bool:
    """Whether writes to GitHub are suppressed for this process.

    Read at call time like allowed_env_vars(), and only ever able to STOP a
    write - so a stale value can cost a run, never a repository.
    """
    return os.environ.get("AUTOSRE_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def _repo() -> str:
    return os.environ.get("GITHUB_TARGET_REPO", "")


def _headers() -> dict:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_user_reviews(limit: int = 10) -> dict:
    """Read recent user-reported problems (open issues labeled 'user-report') from the project tracker.

    These are REAL user complaints. Read them first to understand the user-facing symptom
    before investigating the system.

    Args:
        limit: max number of user reports to return (default 10).
    """
    repo = _repo()
    try:
        with httpx.Client(base_url=API, headers=_headers(), timeout=20.0) as c:
            r = c.get(
                f"/repos/{repo}/issues",
                params={"state": "open", "labels": "user-report", "per_page": limit},
            )
            r.raise_for_status()
            reviews = [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "body": (i.get("body") or "")[:300],
                    "user": i["user"]["login"],
                    "created_at": i["created_at"],
                }
                for i in r.json()
                if "pull_request" not in i
            ]
            # Second-layer injection screen (Model Armor; default-off no-op).
            # Flags a prompt-injection / jailbreak embedded in a user report so the
            # agent and console can see it — the open_pull_request allowlist guard
            # is still the backstop that makes an injected "fix" impossible.
            from agents.armor_tools import screen_text  # lazy import (default-off)

            armor = screen_text(
                "\n".join(f"{r['title']}\n{r['body']}" for r in reviews),
                source="user_report",
            )
            return {"ok": True, "count": len(reviews), "reviews": reviews, "armor": armor}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"GitHub {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def apply_env_value(text: str, name: str, value: str) -> tuple[str, str]:
    """Set name=value in a .env-style file. Returns (new_text, change).

    change is "added" (the variable was absent), "corrected" (it was present
    with a different value) or "already_correct" (nothing to do). Pure, so the
    offline smoke gate can cover both failure classes without GitHub.

    Commented-out lines are left alone: a '#' line is documentation, not config,
    and silently reviving one would be a change nobody reviewed.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith(f"{name}="):
            continue
        if stripped == f"{name}={value}":
            return text, "already_correct"
        lines[i] = f"{name}={value}"
        return "\n".join(lines) + "\n", "corrected"
    return text.rstrip("\n") + f"\n{name}={value}\n", "added"


def open_pull_request(missing_env_var: str, root_cause: str) -> dict:
    """Open a real GitHub pull request that fixes the incident by setting an environment variable to its correct value in the target deploy config.

    Handles both failure classes: a variable that is missing entirely, and a
    variable that is present but set to an unusable value.

    Args:
        missing_env_var: the environment variable to fix, e.g. "DATABASE_URL".
        root_cause: a one-line root-cause summary for the PR description.
    """
    try:
        if dry_run():
            # `autosre run --dry-run`: the investigation is real, the write is
            # not. Suppressed HERE, at the only function that writes to GitHub,
            # rather than in the CLI - a dry run that depended on the caller
            # remembering to disarm the tool would eventually open a PR.
            return {
                "ok": False,
                "dry_run": True,
                "error": f"dry run: no pull request was opened for '{missing_env_var}' "
                "(AUTOSRE_DRY_RUN is set); the diagnosis itself is unaffected",
            }
        repo = _repo()
        allowed = allowed_env_vars()
        if missing_env_var not in allowed:
            return {
                "ok": False,
                "error": f"'{missing_env_var}' is not in the allowed remediation set "
                f"{sorted(allowed)}; refusing to open a PR (safety guard against "
                f"hallucinated or injected fixes)",
            }
        # The canonical restore value is configured out-of-band (not guessed by
        # the model), so the fix is deterministic and matches what recovery applies.
        env_value = os.environ.get(f"AUTOSRE_RESTORE_{missing_env_var}", "<restore-value-unavailable>")
        branch = f"autosre/fix-{missing_env_var.lower()}"
        with httpx.Client(base_url=API, headers=_headers(), timeout=20.0) as c:
            base_sha = (
                c.get(f"/repos/{repo}/git/ref/heads/main").raise_for_status().json()
            )["object"]["sha"]

            f = (
                c.get(f"/repos/{repo}/contents/{CONFIG_PATH}", params={"ref": "main"})
                .raise_for_status()
                .json()
            )
            file_sha = f["sha"]
            current = base64.b64decode(f["content"]).decode("utf-8")
            new_content, change = apply_env_value(current, missing_env_var, env_value)
            if change == "already_correct":
                # The config is already what recovery would write, so a PR would
                # be a no-op. The incident is real but its cause is elsewhere ->
                # the agent should escalate rather than open an empty PR.
                return {
                    "ok": False,
                    "error": f"{missing_env_var} is already set to the canonical value in "
                    f"{CONFIG_PATH}; the config is not the cause - refusing to open an "
                    f"empty PR",
                }

            # Idempotent for repeated demo runs: drop any stale branch first.
            c.delete(f"/repos/{repo}/git/refs/heads/{branch}")
            c.post(
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            ).raise_for_status()

            verb = "restore" if change == "added" else "correct"
            title = f"fix: {verb} {missing_env_var} to recover sida-target"
            c.put(
                f"/repos/{repo}/contents/{CONFIG_PATH}",
                json={
                    "message": title,
                    "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                    "sha": file_sha,
                    "branch": branch,
                },
            ).raise_for_status()

            fix_line = (
                f"**Fix:** restore `{missing_env_var}` in `{CONFIG_PATH}` (it was missing)."
                if change == "added"
                else f"**Fix:** correct the value of `{missing_env_var}` in `{CONFIG_PATH}` "
                f"(it was set, but not to a usable value)."
            )
            pr_body = (
                "## AutoSRE automated fix\n\n"
                f"**Root cause:** {root_cause}\n\n"
                f"{fix_line}\n\n"
                "Opened autonomously by AutoSRE after investigating the live Cloud Run "
                "status, logs, and deployed config. Merge + redeploy require human approval."
            )
            pr = (
                c.post(
                    f"/repos/{repo}/pulls",
                    json={
                        "title": title,
                        "head": branch,
                        "base": "main",
                        "body": pr_body,
                    },
                )
                .raise_for_status()
                .json()
            )
            return {
                "ok": True,
                "pr_number": pr["number"],
                "pr_url": pr["html_url"],
                "branch": branch,
            }
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"GitHub {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
