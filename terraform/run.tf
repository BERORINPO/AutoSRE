# Cloud Run services.
#
# Both services are source-deployed in the live demo (`gcloud run deploy
# --source .`), which builds the image via Cloud Build into the Artifact
# Registry repository "cloud-run-source-deploy". Terraform manages the
# service configuration; the image itself is bootstrapped by that command
# (see README.md, step 3).

# ---------------------------------------------------------------------------
# sida-agent: the AutoSRE agent (ADK / Vertex AI Gemini).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "agent" {
  name     = "sida-agent"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Demo stack: allow terraform destroy to tear the service down.
  deletion_protection = false

  template {
    service_account                  = local.runtime_sa
    timeout                          = "3600s" # long-running agent loops
    max_instance_request_concurrency = 80

    scaling {
      max_instance_count = 20
    }

    containers {
      image = local.agent_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        startup_cpu_boost = true
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "RUN_REGION"
        value = var.region
      }
      env {
        name  = "GITHUB_TARGET_REPO"
        value = var.github_target_repo
      }
      env {
        name  = "AUTOSRE_RESTORE_DATABASE_URL"
        value = var.restore_database_url
      }
      env {
        name  = "TARGET_HEALTH_URL"
        value = "${local.target_url}/health"
      }
      env {
        name = "GITHUB_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.github_pat.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "AUTOSRE_CONSOLE_KEY"
        value = var.console_key
      }
      env {
        name  = "AUTOSRE_PUBSUB_AUDIENCE"
        value = "${local.agent_url}/pubsub/incident"
      }
      env {
        name  = "AUTOSRE_PUBSUB_SA_EMAIL"
        value = local.runtime_sa
      }
      env {
        # Case memory (self-improving loop). Empty disables the feature
        # (recording no-ops, recall reports enabled=false) — default-off
        # staged enablement gated by var.enable_case_memory, same contract
        # as the other AUTOSRE_* flags. Dataset/table exist either way, so
        # enabling is a config-only change.
        name  = "AUTOSRE_CASES_TABLE"
        value = (
          var.enable_case_memory
          ? "${var.project_id}.${google_bigquery_dataset.autosre_memory.dataset_id}.${google_bigquery_table.cases.table_id}"
          : ""
        )
      }
      env {
        # Prompt-injection screening (Model Armor). Empty AUTOSRE_MODEL_ARMOR_ENABLED
        # disables it (screen_text no-ops, reports enabled=false) — default-off
        # staged enablement gated by var.enable_model_armor, same contract as the
        # other AUTOSRE_* flags. No Model Armor cost until enabled + template set.
        name  = "AUTOSRE_MODEL_ARMOR_ENABLED"
        value = var.enable_model_armor ? "1" : ""
      }
      env {
        name  = "AUTOSRE_MODEL_ARMOR_TEMPLATE"
        value = var.enable_model_armor ? var.model_armor_template : ""
      }
      env {
        # Durable run guard (cooldown / daily budget / kill switch). Empty
        # disables it - and a disabled guard still answers "run allowed", so
        # keep this set; see docs/cost-guard-runbook.md. These two were first
        # added with gcloud and later reconciled here so a terraform apply
        # cannot silently disarm the guard.
        name = "AUTOSRE_STATE_URI"
        value = (
          var.enable_run_guard
          ? "gs://${google_storage_bucket.autosre_state.name}/autosre-state.json"
          : ""
        )
      }
      env {
        name  = "AUTOSRE_DAILY_RUN_LIMIT"
        value = var.enable_run_guard ? var.daily_run_limit : ""
      }
      env {
        # Video-attached user reports (default-off staged enablement, same
        # contract as the other AUTOSRE_* flags). The live demo runs with this
        # on via tfvars; reconciled here so an apply cannot strip the video
        # feature the console demo depends on.
        name  = "AUTOSRE_VIDEO_ENABLED"
        value = var.enable_report_video ? "1" : ""
      }
      env {
        name  = "AUTOSRE_REPORT_VIDEO_URI"
        value = var.enable_report_video ? var.report_video_uri : ""
      }
      env {
        name  = "AUTOSRE_VIDEO_BUCKET"
        value = var.enable_report_video ? var.report_video_bucket : ""
      }
    }
  }

  depends_on = [
    google_project_service.required,
    # Ensure the runtime SA can read github-pat before revision rollout,
    # otherwise the revision fails to start.
    google_project_iam_member.runtime_sa_roles,
  ]
}

# Public demo endpoint: the app enforces its own console key where needed.
resource "google_cloud_run_v2_service_iam_member" "agent_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# sida-target: the deliberately fragile demo workload the agent repairs.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "target" {
  name     = "sida-target"
  location = var.region
  project  = var.project_id

  deletion_protection = false

  template {
    service_account = local.runtime_sa

    containers {
      image = local.target_image

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "SECRET_KEY"
        value = var.target_secret_key
      }

      # NOTE: DATABASE_URL is intentionally ABSENT. That is the "armed" demo
      # state: /health fails without it, the uptime check trips, and the
      # AutoSRE agent restores the variable as its remediation. Do not add it
      # here - the demo-rearm Scheduler job removes it every hour anyway.
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "target_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.target.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
