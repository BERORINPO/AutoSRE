# AutoSRE - Terraform

Infrastructure-as-code for the **entire live AutoSRE stack** on Google Cloud:
two Cloud Run services, the Pub/Sub incident pipeline, Cloud Scheduler jobs,
Secret Manager, the Cloud Monitoring uptime check + alert policy, and the IAM
grants that tie it together.

**Honest framing:** the live demo (project `bero-devops-agent`,
`asia-northeast1`) was deployed by hand first with `gcloud`. This Terraform
was written afterwards to mirror that deployment exactly (config values
verified against the live project with `gcloud describe` commands) so the
whole stack is reproducible on a fresh project with one `terraform apply`
plus the two documented bootstrap steps below (container images and the
GitHub token value, which by design never lives in Terraform).

## What gets created

| File | Resources |
|---|---|
| `run.tf` | `sida-agent` + `sida-target` Cloud Run v2 services, public invoker bindings |
| `pubsub.tf` | `autosre-incidents` topic, OIDC push subscription to the agent, Monitoring publisher grant |
| `scheduler.tf` | `autosre-demo-rearm` (hourly re-break) + `autosre-warm-ping` (5-min cold-start mitigation) |
| `secret.tf` | `github-pat` secret container (value added out-of-band) |
| `monitoring.tf` | uptime check on `/health`, Pub/Sub notification channel, alert policy |
| `iam.tf` | runtime SA project roles, Pub/Sub OIDC token-creator grant |
| `apis.tf` | API enablement for a fresh project |

Incident loop wired by these resources:

```
uptime check fails -> alert policy OPENED -> Pub/Sub channel
  -> autosre-incidents topic -> OIDC push -> sida-agent /pubsub/incident
  -> agent diagnoses + patches sida-target on Cloud Run -> verifies /health
  -> opens PR on GITHUB_TARGET_REPO with the incident report
```

## Apply order on a fresh project

Prerequisites: Terraform >= 1.5, `gcloud` authenticated with a project owner
(or equivalent), billing enabled on the project.

1. **Init and set variables.**

   ```
   terraform init
   ```

   Create `terraform.tfvars` (do not commit the key):

   ```hcl
   project_id  = "your-project-id"
   console_key = "a-long-random-string"
   ```

2. **First apply - APIs, secret container, topic, IAM.**

   ```
   terraform apply
   ```

   On a brand-new project this first apply will create everything except the
   Cloud Run services, which fail until the images exist (step 3) and the
   secret has a version (step 4). If you prefer a clean sequence, target the
   prerequisites first:

   ```
   terraform apply -target=google_project_service.required \
                   -target=google_secret_manager_secret.github_pat \
                   -target=google_project_iam_member.runtime_sa_roles
   ```

3. **Bootstrap the container images (one-time, source deploy).**

   The live services were source-deployed; `gcloud run deploy --source`
   builds the image with Cloud Build and pushes it to the Artifact Registry
   repository `cloud-run-source-deploy`, which is exactly where the
   `agent_image` / `target_image` variable defaults point:

   ```
   gcloud run deploy sida-target --source services/target-service \
     --project <project_id> --region asia-northeast1
   gcloud run deploy sida-agent  --source packages/agent \
     --project <project_id> --region asia-northeast1
   ```

   Terraform then owns the service configuration from the next apply
   (import the two services, or let the next apply reconcile env/scaling;
   `terraform import` is the cleaner path if you ran the deploys after
   creating the services with Terraform). Alternative: build the images any
   other way and pass `agent_image` / `target_image` explicitly.

4. **Add the GitHub token value (never stored in Terraform state).**

   ```
   printf '%s' "$GITHUB_PAT" | gcloud secrets versions add github-pat \
     --project <project_id> --data-file=-
   ```

   The PAT needs `repo` scope on the repo in `github_target_repo` (default
   `BERORINPO/sida-target-config`).

5. **Final apply.**

   ```
   terraform apply
   ```

   Outputs `agent_url` and `target_url`. The demo is now "armed":
   `sida-target` deliberately has no `DATABASE_URL`, so within one uptime
   check period (300s) the alert fires and the agent runs autonomously.

## Manual steps summary (by design, not omissions)

- **Secret value** (`github-pat` version): added with `gcloud`, step 4.
- **Container images**: built by `gcloud run deploy --source`, step 3.
- Everything else - services, env vars, secret reference, IAM, Pub/Sub,
  Scheduler, Monitoring - is fully declarative here.

## Notes / deliberate details that mirror the live stack

- `autosre-demo-rearm` uses **no OIDC**: the agent's `/reset` endpoint
  authenticates via the `?key=` console-key query parameter. That matches
  the live job exactly.
- `sida-target` has `DATABASE_URL` **intentionally absent** (armed demo
  state). Do not "fix" it in `run.tf`; breaking it is the demo.
- Both services run as the Compute Engine default service account
  (`<project-number>-compute@developer.gserviceaccount.com`), matching the
  live stack. Override with `runtime_service_account_email` if you want a
  dedicated SA.
- Cloud Run URLs are computed deterministically
  (`https://SERVICE-PROJECT_NUMBER.REGION.run.app`) instead of referencing
  resource attributes, because the agent's own env vars embed its own URL
  (`AUTOSRE_PUBSUB_AUDIENCE`), which would otherwise be a dependency cycle.
- `google_project_iam_member.pubsub_token_creator` (Pub/Sub service agent as
  token creator) is required for OIDC push on a fresh project; on the
  long-lived live project this grant already existed implicitly.
- `deletion_protection = false` on both Cloud Run services so
  `terraform destroy` cleans up the demo; flip to `true` for anything
  longer-lived.
