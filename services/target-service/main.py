"""AutoSRE demo target-service.

A trivial "production" app that is healthy only when DATABASE_URL is set.

  - Deployed WITHOUT DATABASE_URL  => /healthz returns 503  (the incident)
  - Redeployed WITH DATABASE_URL   => /healthz returns 200  (recovered)

The container always boots (uvicorn binds $PORT), so Cloud Run keeps a serving
revision. The failure surfaces as a 503 on /healthz, which AutoSRE detects from
the outside, diagnoses (missing env var, via logs + config), fixes (re-adds the
env var through a PR), and verifies (polls /healthz until 200).
"""
import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="target-service")

# Comma-separated list of env vars the app requires to be healthy. Default is a
# single "DATABASE_URL" so the existing demo behaves byte-identically; set
# REQUIRED_ENV_VARS (e.g. "DATABASE_URL,SECRET_KEY") to arm the multi-var scenario.
REQUIRED_ENVS = [v.strip() for v in os.environ.get("REQUIRED_ENV_VARS", "DATABASE_URL").split(",") if v.strip()]

# Second failure class: the variable is set, but its value is wrong.
#
# "present" and "correct" are different questions, and an agent that only learned
# "env var in the logs -> add the env var" gets this one wrong: the fix is to
# replace a value, not to append one. Off unless VALIDATED_ENV_VARS is set, so
# the original demo is unaffected.
#
# Format: "NAME=scheme1|scheme2", comma-separated between entries.
#   VALIDATED_ENV_VARS="DATABASE_URL=postgres|postgresql"
def _parse_validated(raw: str) -> dict[str, list[str]]:
    spec: dict[str, list[str]] = {}
    for entry in raw.split(","):
        name, _, schemes = entry.partition("=")
        name, schemes = name.strip(), schemes.strip()
        if name and schemes:
            spec[name] = [s.strip().lower() for s in schemes.split("|") if s.strip()]
    return spec


VALIDATED_ENVS = _parse_validated(os.environ.get("VALIDATED_ENV_VARS", ""))


def _url_problem(value: str, allowed_schemes: list[str]) -> str | None:
    """Why this value is unusable as a connection URL, or None if it is fine."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in allowed_schemes:
        return (
            f"expected scheme {'|'.join(allowed_schemes)}, got "
            f"{parsed.scheme or '(none)'!r}"
        )
    if not parsed.hostname:
        return "no host in connection URL"
    return None


@app.get("/")
def root() -> dict:
    return {"service": "target-service", "db_configured": all(bool(os.environ.get(name)) for name in REQUIRED_ENVS)}


# NOTE: "/healthz" is reserved by the Cloud Run front end (GFE 404s it before
# it reaches the container). Expose the health check at "/health" instead.
@app.get("/health")
def health():
    for name in REQUIRED_ENVS:
        if not os.environ.get(name):
            # Log the boot-time failure so AutoSRE can find the root cause in Cloud Logging.
            print(f'{{"severity":"ERROR","message":"startup check failed: required env var {name} is not set"}}', flush=True)
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "reason": f"required env var {name} is not set"},
            )
    # Class 2: set, but not usable. Deliberately worded so it cannot be confused
    # with the "is not set" message above — the two classes have to be
    # distinguishable from the logs alone, since that is what the agent reads.
    for name, schemes in VALIDATED_ENVS.items():
        value = os.environ.get(name, "")
        if not value:
            continue  # absence is the class above; do not report it twice
        problem = _url_problem(value, schemes)
        if problem:
            print(
                f'{{"severity":"ERROR","message":"startup check failed: env var {name} '
                f'is set but its value is invalid ({problem})"}}',
                flush=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "reason": f"env var {name} is set but its value is invalid ({problem})",
                },
            )
    return {"status": "ok"}
