<#
.SYNOPSIS
  Inject or restore the AutoSRE demo incident on the target Cloud Run service.

.DESCRIPTION
  Two failure classes:
    inject   : remove DATABASE_URL          -> /health 503 "required env var ... is not set"
    corrupt  : set DATABASE_URL to a bad URL -> /health 503 "... is set but its value is invalid"
    restore  : re-add the canonical value    -> /health 200 (recovered)

  'corrupt' needs the target deployed with VALIDATED_ENV_VARS set, e.g.
    --update-env-vars="VALIDATED_ENV_VARS=DATABASE_URL=postgres|postgresql"
  Without it the target only checks presence and a corrupted value still returns 200.

  This deterministic on-demand trigger replaces Cloud Monitoring + Pub/Sub for the demo.

.EXAMPLE
  pwsh scripts/target-incident.ps1 -Action inject
  pwsh scripts/target-incident.ps1 -Action corrupt
  pwsh scripts/target-incident.ps1 -Action restore
#>
param(
  [ValidateSet("inject", "corrupt", "restore")]
  [string]$Action = "inject",
  [string]$EnvVar = "DATABASE_URL"
)

# Demo restore values keyed by env var name. DATABASE_URL keeps the original
# demo value so the default path is unchanged; SECRET_KEY supports the
# escalation (out-of-policy) scenario.
$restoreValues = @{
  DATABASE_URL = "postgres://demo:demo@db.internal:5432/app"
  SECRET_KEY   = "demo-secret-0000-rotate-me"
}

# Class 2 values: present, non-empty, and unusable. Wrong scheme rather than
# garbage, so the log line names a concrete problem the agent has to read.
$corruptValues = @{
  DATABASE_URL = "mysql://demo:demo@db.internal:3306/app"
}

$common = @(
  "run", "services", "update", "sida-target",
  "--project", "bero-devops-agent",
  "--region", "asia-northeast1",
  "--quiet"
)

if ($Action -eq "inject") {
  gcloud @common --remove-env-vars $EnvVar
  Write-Output "[inject] $EnvVar removed from sida-target. /health -> 503 (class 1: not set)."
}
elseif ($Action -eq "corrupt") {
  $bad = $corruptValues[$EnvVar]
  if (-not $bad) {
    Write-Error "no corrupt value defined for $EnvVar (add one to `$corruptValues)"
    exit 1
  }
  # Quote the whole flag so PowerShell does not array-split on any comma.
  gcloud @common "--update-env-vars=$EnvVar=$bad"
  Write-Output "[corrupt] $EnvVar set to an invalid value. /health -> 503 (class 2: invalid value)."
  Write-Output "          (requires VALIDATED_ENV_VARS on the target; otherwise this still returns 200)"
}
else {
  gcloud @common "--update-env-vars=$EnvVar=$($restoreValues[$EnvVar])"
  Write-Output "[restore] $EnvVar set to the canonical value. /health -> 200 (recovered)."
}
