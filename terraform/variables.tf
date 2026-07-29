variable "project_id" {
  description = "GCP project id that hosts the AutoSRE stack (live demo: bero-devops-agent)."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud Scheduler and Artifact Registry."
  type        = string
  default     = "asia-northeast1"
}

variable "github_target_repo" {
  description = "GitHub repo (owner/name) the agent opens remediation PRs against."
  type        = string
  default     = "BERORINPO/sida-target-config"
}

variable "console_key" {
  description = <<-EOT
    Shared key for the agent's operator console and /reset endpoint.
    The demo-rearm Scheduler job passes it as a query parameter because the
    endpoint validates the key itself (no OIDC on that job by design).
  EOT
  type        = string
  sensitive   = true
}

variable "restore_database_url" {
  description = <<-EOT
    Value injected as AUTOSRE_RESTORE_DATABASE_URL on the agent. In the live
    demo this is a stand-in DSN the agent "restores" onto the broken target;
    it is not a real reachable database.
  EOT
  type        = string
  default     = "postgres://demo:demo@db.internal:5432/app"
}

variable "runtime_service_account_email" {
  description = <<-EOT
    Service account both Cloud Run services run as, and which Pub/Sub uses to
    mint OIDC tokens for the push subscription. Leave empty to use the Compute
    Engine default service account (<project-number>-compute@developer.gserviceaccount.com),
    which is what the live deployment uses.
  EOT
  type        = string
  default     = ""
}

variable "agent_image" {
  description = <<-EOT
    Container image for the sida-agent service. Leave empty to use the
    Artifact Registry path produced by `gcloud run deploy sida-agent --source .`
    (repository cloud-run-source-deploy). That command is the image bootstrap
    on a fresh project - see README.md.
  EOT
  type        = string
  default     = ""
}

variable "target_image" {
  description = <<-EOT
    Container image for the sida-target service. Leave empty to use the
    Artifact Registry path produced by `gcloud run deploy sida-target --source .`
    (repository cloud-run-source-deploy). See README.md.
  EOT
  type        = string
  default     = ""
}

variable "target_secret_key" {
  description = <<-EOT
    SECRET_KEY env var on sida-target. Intentionally a throwaway demo value:
    the target app is a deliberately fragile demo workload.
  EOT
  type        = string
  default     = "demo-secret-0000-rotate-me"
}

variable "enable_case_memory" {
  description = <<-EOT
    Staged enablement for the self-improving loop (case memory). false keeps
    AUTOSRE_CASES_TABLE empty on the agent service = feature fully off
    (recording no-ops, recall reports enabled=false), matching the default-off
    contract of the other AUTOSRE_* capabilities. The BigQuery dataset/table
    are still created so flipping this on is a config-only change.
  EOT
  type        = bool
  default     = false
}

variable "enable_model_armor" {
  description = <<-EOT
    Staged enablement for the prompt-injection screening layer (Model Armor).
    false keeps AUTOSRE_MODEL_ARMOR_ENABLED empty on the agent = feature fully
    off (screen_text no-ops, reports enabled=false), matching the default-off
    contract of the other AUTOSRE_* capabilities. No Model Armor cost or IAM
    grant is incurred until this is flipped on and a template is supplied.
  EOT
  type        = bool
  default     = false
}

variable "model_armor_template" {
  description = <<-EOT
    Full Model Armor template resource name the agent screens user reports
    against: projects/<project>/locations/<location>/templates/<id>. Created
    out-of-band (gcloud model-armor templates create ...) with a prompt-injection
    & jailbreak filter. Only used when enable_model_armor = true.
  EOT
  type        = string
  default     = ""
}

variable "enable_run_guard" {
  description = <<-EOT
    Durable run guard (cooldown, daily run budget, kill switch) backed by a GCS
    state object. Deliberately default-ON, unlike the other AUTOSRE_* flags:
    those gate extra capabilities, this one is the spending ceiling, and an
    unset AUTOSRE_STATE_URI makes the guard silently inert while every run
    still answers "allowed" (see docs/cost-guard-runbook.md). The bucket is
    created by this configuration, so default-on works on a fresh project with
    no manual step.
  EOT
  type        = bool
  default     = true
}

variable "daily_run_limit" {
  description = <<-EOT
    AUTOSRE_DAILY_RUN_LIMIT: agent runs allowed per UTC day before the guard
    trips its own kill switch (which then stays tripped until a human clears
    it via POST /guard/killswitch). String because it is an env var.
  EOT
  type        = string
  default     = "50"
}

variable "enable_report_video" {
  description = <<-EOT
    Staged enablement for video-attached user reports (Gemini reads the screen
    recording directly). false keeps AUTOSRE_VIDEO_ENABLED empty = feature off,
    matching the default-off contract of the other AUTOSRE_* capabilities.
    The live demo runs with this on - set it (plus the two vars below) in
    tfvars when reconciling against the live project.
  EOT
  type        = bool
  default     = false
}

variable "report_video_uri" {
  description = <<-EOT
    AUTOSRE_REPORT_VIDEO_URI: gs:// URI of the demo screen recording the agent
    analyzes (live: gs://<project>-autosre-reports/report-recording.mp4).
    Bucket and clip are created out-of-band, like the Model Armor template.
    Only used when enable_report_video = true.
  EOT
  type        = string
  default     = ""
}

variable "report_video_bucket" {
  description = <<-EOT
    AUTOSRE_VIDEO_BUCKET: gs:// URI (including the gs:// prefix, matching the
    live value) of the bucket holding report recordings. Created out-of-band.
    Only used when enable_report_video = true.
  EOT
  type        = string
  default     = ""
}
