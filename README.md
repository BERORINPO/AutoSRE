# AutoSRE - an autonomous on-call SRE agent

**When a deploy breaks at 2am, AutoSRE reads the user complaints, tails the real logs,
finds the cause and opens a fix PR - then, after one human approval, verifies `/health`
is 200 again.** It is a Google ADK agent that runs the on-call investigate-and-repair
loop on Cloud Run, and stops at the approval gate before anything irreversible.

**[Quickstart](#quickstart--from-an-empty-gcp-project-to-your-first-autonomous-fix) - 15 minutes from an empty GCP project to your first diagnosis.**

| Measured | |
|---|---|
| Correct diagnosis | **90.2%** (n=51, offline eval) |
| Investigation -> fix PR opened | **27-30 s** |
| Approval -> `/health` back to 200 | **12 s** (measured on stage, 2026-08-19) |
| One full run | **~$0.013** (8 LLM calls, 32k tokens) |

> DevOps x AI Agent Hackathon (Findy x Google Cloud, 2026) - **優秀賞 (Excellence Award)**

![AutoSRE in action: a real Pub/Sub event starts the run with zero clicks, the agent investigates on real logs and opens a real PR, one human approval, and 503 flips to 200 with a drafted reply to the affected users](docs/media/hero.gif)

Solo founders and small teams have no on-call SRE for 2am, and the loop still has to be
run by someone: read the logs, find the cause, write the fix, ship it, verify it
recovered.

AutoSRE starts where the pain does. It reads the **real user reports** (complaints
in the project's issue tracker), correlates them with the live system state, fixes the
root cause, and drafts a reply to the affected users — closing the loop from user pain
to remediation.

**How it differs:** most AI SRE agents trigger from a monitoring *alert* and end
at a *dashboard*. AutoSRE triggers from the **user's voice** and ends by **writing the user
back** — user-pain-in, user-answer-out. (Drafting a fix PR itself is table stakes —
incident.io, Rootly, and Azure SRE Agent do it too; the differentiation is the entry and
exit points of the loop.) Investigate / diagnose / propose are autonomous;
merge + deploy stay behind a human approval gate — the 2026 industry consensus for
irreversible actions (incident.io, Rootly, Azure SRE Agent).

**Live demo:** https://sida-agent-860561433627.asia-northeast1.run.app

---

## What it does — five steps, and each one is the product

| Step | What the agent does | Why a single LLM call can't |
|------|---------------------|-----------------------------|
| **Sense** | Reads **real user reports**, probes the target's health, tails **real Cloud Logging**, reads the **deployed Cloud Run config** | Multi-step tool use where each result decides the next action (a ReAct loop) |
| **Diagnose** | Gemini reasons over the real evidence and pinpoints the root cause | Grounded in the actual stack trace + config, not a plausible guess |
| **Propose** | Generates a concrete config fix | — |
| **Gate** | **Pauses for human approval** — the trust boundary | Suspends multi-source state across an unbounded human decision |
| **Verify** | After the fix is applied, polls health until it's green again | observe → act → observe closure — agency, not generation |

The line AutoSRE draws: **read / diagnose / propose = autonomous. Merge + deploy =
always human-approved.**

Every run produces a **real, permanent artifact**: the pull request the agent opened.
See a live example: https://github.com/BERORINPO/sida-target-config/pull/7

### Autonomous trigger — no human needed to start

AutoSRE runs itself — and the whole detection chain is **live, not simulated**. A
**Cloud Monitoring uptime check** watches the target's `/health`; when it fails, the
alert policy publishes to a **Pub/Sub** topic (`autosre-incidents`), whose push
subscription invokes the agent's `POST /pubsub/incident` endpoint (**OIDC-verified**:
Google-signed token, audience + service-account checked). The full
**investigate → diagnose → open-PR** loop runs with **no human click**. Only merge +
deploy wait for a human. (The "Run AutoSRE" button remains for on-demand runs, and
publishing to the topic manually exercises the exact same event path.)

### Measured in production

Numbers from real production runs of this demo scenario (2026-07-07, Cloud Run
request logs + GitHub timestamps) — measured, not projected:

| What | Measured |
|------|----------|
| Full autonomous chain — uptime check 503 → alert → Pub/Sub → OIDC-verified push → agent run → real fix PR, **zero human action** | Fired end-to-end **twice**: [PR #20](https://github.com/BERORINPO/sida-target-config/pull/20), [PR #21](https://github.com/BERORINPO/sida-target-config/pull/21) |
| Agent investigation run — reads real user reports + live Cloud Logging + deployed Cloud Run config, diagnoses, opens a real PR | **27–30 s** (Cloud Run request latencies: 27.3 s / 30.4 s) |
| Pub/Sub event published → fix PR opened | **~90 s** end-to-end |
| After the one human click (Approve) | PR merged, target redeployed, and the agent itself polls `/health` until 503 flips to 200 — recovery verified automatically (150 s poll budget) |

For context: a human on-call engineer paged at 2am typically needs tens of
minutes to read the logs, find the cause, patch, deploy, and verify. These
figures are for this demo scenario — evidence that the loop closes fast, not a
universal MTTR claim.

---

## Quickstart — from an empty GCP project to your first autonomous fix

Two tracks, and the first one is short on purpose.

- **Track A (~15 min)** — deploy the broken demo service, then drive the agent **from
  your terminal**. The agent itself is not deployed: `autosre run` executes the same
  ReAct loop locally against your real Cloud Run service and your real Cloud Logging.
  You get a diagnosis and a real fix PR before committing to any infrastructure.
- **Track B (~20 min more)** — one `terraform apply` for the full stack, including the
  autonomous chain: an uptime check notices the 503, an alert publishes to Pub/Sub, and
  the agent runs **with nobody watching**.

**Track A is a prerequisite for Track B**, not just a recommendation: Track B adopts the
target service you deploy in step 4.

### What you need

| | |
|---|---|
| **GCP project** | with **billing enabled** — Cloud Run and Vertex AI both refuse to start without it |
| **`gcloud`** | authenticated as a project **Owner** (or Editor + Project IAM Admin) |
| **Python** | >= 3.10, plus `git` and `curl` |
| **`gh`** | GitHub CLI, **already authenticated** (`gh auth status`). Optional — step 2 has a click-through fallback |
| **Terraform** | >= 1.5 — Track B only |
| **GitHub token** | a **fine-grained PAT** (`github_pat_...`) scoped to the one repo you create in step 2, with **Contents: Read and write** and **Pull requests: Read and write** |
| **Time** | ~15 min (A) + ~20 min (B). Most of it is two Cloud Builds and one 5-minute uptime-check period |

**What it costs.** At rest, effectively nothing: both Cloud Run services scale to zero
and Gemini is only called during a run. One full run measured **~$0.013** (8 LLM calls,
32k tokens; 2026-08-07, computed from public list prices — not a bill). The uptime
check, the Scheduler jobs and the Secret Manager secret fit inside the always-free
tiers. Two things keep costing after you stop looking, and [Teardown](#teardown) turns
both off: **Artifact Registry storage** for the two container images (~0.5 GB), and —
Track B only — the hourly `autosre-demo-rearm` job, which re-breaks the target every
hour so the demo stays armed.

### Set your placeholders once

Every command below reads these. Nothing else in this Quickstart needs editing.

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-northeast1"
export CONFIG_REPO="your-github-user/autosre-target-config"   # created in step 2
export GITHUB_TOKEN="github_pat_..."                          # fine-grained, see above
export AUTOSRE_CONSOLE_KEY="$(openssl rand -hex 24)"          # generate ONCE - write it down

# what the CLI itself reads
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_GENAI_USE_VERTEXAI=TRUE     # route Gemini through Vertex, not an API key
export GOOGLE_CLOUD_LOCATION=global       # Vertex location; NOT the same as REGION
export RUN_REGION="$REGION"
export GITHUB_TARGET_REPO="$CONFIG_REPO"
```

**These live only in this shell.** Track A and Track B are separated by a decision point
and two Cloud Builds, so if you open a new tab or come back tomorrow, **re-run this
block first** — with the same `AUTOSRE_CONSOLE_KEY` you wrote down, because Track B
stores it in `terraform.tfvars` and a regenerated key will not match. An empty
`$GITHUB_TOKEN` in step 8 fails **silently**: it writes a zero-byte secret version, and
you find out later as a 401 from the deployed agent.

**On PowerShell**, `gcloud` / `terraform` / `git` / `gh` / `autosre` commands are
identical; `export`, `printf`, `curl` and `$(...)` are not. Their PowerShell equivalents
are in [Troubleshooting](#troubleshooting).

---

### Track A — diagnose a real incident from your terminal

#### 1. Get the code and the CLI

```bash
git clone https://github.com/BERORINPO/AutoSRE.git
cd AutoSRE
python -m venv .venv && . .venv/bin/activate    # PowerShell: .venv\Scripts\Activate.ps1
pip install -e packages/agent                   # puts `autosre` on PATH
autosre version
```

The virtualenv matters on current Linux and macOS: a system `pip install` there stops
with `error: externally-managed-environment`. Every later `autosre` command assumes this
environment is active — re-activate it if you open a new shell.

#### 2. Create the config repo the fix PR will edit

The agent does not patch Cloud Run behind your back: it opens a pull request against a
git repo holding the target's deploy config, and only a merged PR becomes a deployment.
That repo is yours, and it needs exactly one file.

Run this **outside** the AutoSRE clone, so you do not nest one repo inside the other:

```bash
cd ..
gh repo create "$CONFIG_REPO" --public --clone
cd "$(basename "$CONFIG_REPO")"
mkdir -p deploy
printf '%s\n' \
  '# Deploy configuration for the demo target service.' \
  '# DATABASE_URL is deliberately absent. That missing line is the incident.' \
  'SECRET_KEY=demo-secret-0000-rotate-me' > deploy/target-service.env
git add deploy && git commit -m "chore: initial target config" && git push -u origin HEAD
cd ../AutoSRE
```

`git push -u origin HEAD` rather than `git push`: a freshly created repo has no upstream
yet, and it also copes with `master` as your local default branch.

No `gh`? Create the repo in the GitHub UI, add `deploy/target-service.env` with those
three lines, and commit it on the default branch.

#### 3. Point gcloud at the project and turn on the APIs

```bash
gcloud auth login
gcloud auth application-default login     # in Track A the agent runs as *you*
gcloud config set project "$PROJECT_ID"
gcloud auth application-default set-quota-project "$PROJECT_ID"

gcloud services enable \
  run.googleapis.com logging.googleapis.com aiplatform.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT_ID"
```

Track A needs those five. Track B enables the rest (Pub/Sub, Monitoring, Scheduler,
Secret Manager, BigQuery) through Terraform.

#### 4. Deploy the demo target — and break it

The target is an ordinary FastAPI app that is healthy only when `DATABASE_URL` is set.
You deploy it without one, and that is the entire incident.

```bash
gcloud run deploy sida-target \
  --source services/target-service \
  --project "$PROJECT_ID" --region "$REGION" --allow-unauthenticated

export TARGET_HEALTH_URL="$(gcloud run services describe sida-target \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')/health"

curl -s -o /dev/null -w '%{http_code}\n' "$TARGET_HEALTH_URL"    # expect: 503
```

The first build takes ~4 minutes. A `503` here means the demo is armed correctly — that
is the failure the agent is about to investigate. `TARGET_HEALTH_URL` is not just for
that `curl`: it is the endpoint the CLI probes, so keep it exported (or pass
`--health-url`). The service **name** `sida-target` is the CLI's built-in default;
`--service` overrides it, but Terraform in Track B expects these names, so leave them.

#### 5. Ask the CLI what is still missing

```bash
autosre doctor
```

`doctor` is a checklist, not a smoke test: every failing line carries its own fix,
secrets are reported as `set (hidden)` and never echoed, and it exits non-zero while
anything required is missing. A ready machine ends like this:

```
python packages:
  [ok] google.adk            google-adk
  [ok] google.cloud.run_v2   google-cloud-run
  [ok] google.cloud.logging  google-cloud-logging
  [ok] httpx                 httpx

credentials:
  [ok] google credentials  application default credentials

ready: `autosre run` can start an incident from this machine.
```

Do not go on until you see that last line. Missing lines are marked `[--]` with the fix
next to them.

#### 6. Run the loop

Rehearse first — this investigates and diagnoses but opens nothing:

```bash
autosre run --dry-run
```

Then do it for real. The run prints each tool call as it happens (health probe, the real
Cloud Logging tail, the deployed Cloud Run config, then the diagnosis) and ends with the
number of the pull request it opened:

```bash
autosre run
```

Read that PR on GitHub — it is the artifact, and reading it before approving is the
point of the gate. Then, with its number:

```bash
autosre approve 7      # merge, apply to Cloud Run, poll /health until it answers 200
```

`--dry-run` is enforced inside `open_pull_request` itself, so a rehearsal cannot leak a
PR even if something above it misbehaves. `approve` is the only step that changes
anything: it merges the PR you just read, patches `DATABASE_URL` back onto the service,
and then polls `/health` until it answers 200 — recovery verified, not assumed. Exit
code `3` means the agent refused to act because the fix fell outside the remediation
allowlist; the full table is in
[the CLI section](#use-it-from-a-terminal--the-autosre-cli).

That is the whole product loop. Track B changes **who starts it**, and nothing else.

---

### Track B — the autonomous chain, with Terraform

From the AutoSRE clone, with the placeholder block from the top of this Quickstart still
exported in this shell.

#### 7. Write your tfvars and create the prerequisites

```bash
cd terraform
printf '%s\n' \
  "project_id         = \"$PROJECT_ID\"" \
  "region             = \"$REGION\"" \
  "github_target_repo = \"$CONFIG_REPO\"" \
  "console_key        = \"$AUTOSRE_CONSOLE_KEY\"" > terraform.tfvars

terraform init
terraform apply \
  -target=google_project_service.required \
  -target=google_secret_manager_secret.github_pat \
  -target=google_project_iam_member.runtime_sa_roles
```

State is **local** — `terraform.tfstate` lands in this directory, so keep it (a lost
state file means `terraform destroy` can no longer clean up). `terraform.tfvars` holds
your console key in plain text and is git-ignored; do not commit it.

This first, targeted apply exists because the Cloud Run resources cannot come up before
the images exist and the secret has a value — the next step.

#### 8. Build the agent image and store the token

```bash
gcloud run deploy sida-agent --source ../packages/agent \
  --project "$PROJECT_ID" --region "$REGION"

printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add github-pat \
  --project "$PROJECT_ID" --data-file=-
```

The GitHub token value never enters Terraform state, by design. (Your console key does —
it is in `terraform.tfvars` and in the state file.)

Check the secret took a real value rather than an empty `$GITHUB_TOKEN`:

```bash
gcloud secrets versions describe latest --secret=github-pat \
  --project "$PROJECT_ID" --format='value(state)'    # expect: ENABLED
```

#### 9. Hand the two services over to Terraform

Both services now exist because `gcloud run deploy` created them — that command builds
the image *and* creates the service. Terraform manages the same two resources, so import
them or the next apply fails with "already exists":

```bash
terraform import google_cloud_run_v2_service.target \
  "projects/$PROJECT_ID/locations/$REGION/services/sida-target"
terraform import google_cloud_run_v2_service.agent \
  "projects/$PROJECT_ID/locations/$REGION/services/sida-agent"
```

#### 10. Apply the rest

```bash
terraform apply
terraform output agent_url
```

That creates the Pub/Sub topic and its OIDC push subscription, the uptime check, the
alert policy, the Scheduler jobs, the IAM that ties them together — and the public
invoker binding that makes the agent reachable from a browser. Access control on the
agent is the console key, not IAM. Open `agent_url` for the operator console, or keep
using the CLI against it:

```bash
autosre status --remote "$(terraform output -raw agent_url)"
```

This apply also arms the durable cost guard (cooldown, daily run limit, kill switch),
which is off in a local Track A run. `autosre status` reports the budget it is holding.

#### 11. Watch it run without you

If you ran `autosre approve` in step 6, the target is healthy again — so break it once
more, or nothing will fire until the hourly re-arm job gets to it:

```bash
gcloud run services update sida-target --remove-env-vars DATABASE_URL \
  --project "$PROJECT_ID" --region "$REGION"
```

Now wait. Within one uptime-check period (**~5 minutes**) the check fails, the alert
policy publishes to `autosre-incidents`, the push subscription calls the agent with a
Google-signed OIDC token, and a run starts with no human click. A new PR appears in
`$CONFIG_REPO`.

Merge + deploy still wait for you. That line does not move.

---

### Teardown

From the AutoSRE clone:

```bash
cd terraform && terraform destroy && cd ..
gcloud artifacts repositories delete cloud-run-source-deploy \
  --project "$PROJECT_ID" --location "$REGION"     # the images: the lingering cost
```

`terraform destroy` removes both Cloud Run services (once step 9 imported them), the
Pub/Sub and Monitoring resources, the Scheduler jobs and the state bucket. Did Track A
only? Then there is no Terraform state, and one line does it:

```bash
gcloud run services delete sida-target --project "$PROJECT_ID" --region "$REGION"
```

Two things are outside all of this and are yours to clean up: the **config repo** on
GitHub, and the **fine-grained PAT** — revoke it, it can write to that repo.

Just pausing the demo instead: `gcloud scheduler jobs pause autosre-demo-rearm --project
"$PROJECT_ID" --location "$REGION"` stops the hourly re-break, which is the part that
accrues cost while you are not looking.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `doctor` reports `GOOGLE_GENAI_USE_VERTEXAI` missing | Without it the Gemini SDK falls back to API-key mode and never reaches Vertex. It is in the placeholder block — re-run that block |
| `autosre: command not found` in a new shell | The virtualenv from step 1 is not active. `. .venv/bin/activate` (PowerShell: `.venv\Scripts\Activate.ps1`) |
| `pip install` fails with `externally-managed-environment` | You skipped the `python -m venv` line in step 1 |
| `PermissionDenied` reading logs or the service config | Your ADC identity, not the project, is short a role. Grant yourself `roles/logging.viewer`, `roles/run.viewer` and `roles/run.developer` — or run Track A as project Owner |
| `403 ... API has not been used in project ... or it is disabled` | An API from step 3 was skipped. Re-run that `gcloud services enable` line; enablement can take a minute to propagate |
| Vertex returns a quota-project error | `gcloud auth application-default set-quota-project "$PROJECT_ID"` (step 3) |
| `404` / `NOT_FOUND` from the Gemini model | Wrong Vertex location. This stack uses `GOOGLE_CLOUD_LOCATION=global`, which is **not** the same value as `REGION` |
| `--allow-unauthenticated` is rejected | An org policy (Domain Restricted Sharing) blocks public Cloud Run services. Use a personal project, or ask an admin for an exception on this project |
| `gcloud run deploy` fails with a billing error | Billing is not enabled on the project. Cloud Build and Cloud Run both require it, free tier included |
| The health URL returns `404` instead of `503` | The route is `/health`; `$TARGET_HEALTH_URL` must include it. Step 4 appends it for you |
| `autosre run` exits `4` | The cost guard blocked the run: cooldown, daily limit, or a tripped kill switch. `autosre status` says which; see [docs/cost-guard-runbook.md](docs/cost-guard-runbook.md) |
| `autosre run` exits `1` with `undetermined` | The run finished but its answer could not be parsed. That is deliberately not a success — read the printed tool trace to see where it stopped |
| The PR opens against the wrong repo | `GITHUB_TARGET_REPO` is unset in this shell, so it fell back to the upstream default. Re-run the placeholder block |
| The deployed agent 401s on GitHub | `$GITHUB_TOKEN` was empty when you ran step 8 and a zero-byte secret version was stored. Re-run that `printf ... \| gcloud secrets versions add` line with the token exported |
| `terraform apply` says a service already exists | Step 9's two `terraform import` commands were skipped |
| `terraform apply` 409s on the state bucket | It survives from an earlier run: `terraform import google_storage_bucket.autosre_state "${PROJECT_ID}-autosre-state"` |
| Step 11: five minutes pass and nothing happens | The target is healthy — `autosre approve` fixed it in step 6. Run the `--remove-env-vars DATABASE_URL` command at the top of step 11 |
| **PowerShell**: `export` does nothing | `$env:PROJECT_ID = "your-gcp-project-id"`, one line per variable |
| **PowerShell**: `openssl` / `basename` / `printf` not recognized | Key: `$env:AUTOSRE_CONSOLE_KEY = -join ((1..48) \| % { '{0:x}' -f (Get-Random -Max 16) })`. For `basename`, `cd` into the repo name literally. Write `deploy/target-service.env` and `terraform.tfvars` with an editor instead of the `printf` blocks |
| **PowerShell**: the `curl` line in step 4 misbehaves | `curl` is an alias for `Invoke-WebRequest`. Use `(iwr $env:TARGET_HEALTH_URL -SkipHttpErrorCheck).StatusCode` |
| **PowerShell**: step 8 stores a corrupt token | The pipe encodes as UTF-16LE. Write the token to a file with `Set-Content -Encoding ascii -NoNewline`, then `gcloud secrets versions add github-pat --data-file=that-file` and delete it |





---

## Architecture

```
   Browser (Incident console, served by agent-service)
        │  POST /incident          POST /approve (human-gated)
        ▼
┌──────────────────────────────────────────────┐
│  agent-service  (Cloud Run, FastAPI + ADK)    │
│  ADK LlmAgent (Gemini 2.5 Flash) ReAct loop   │
│  tools: probe_health, get_recent_logs,        │
│         get_service_config, get_service_status│
│         open_pull_request                     │
└───┬─────────────┬──────────────┬──────────────┘
    │ read logs   │ read/patch   │ open + merge PR
    ▼             ▼              ▼
Cloud Logging   Cloud Run    GitHub (config repo)
                  API              │
                    │              │ on approval: merge
                    ▼              ▼
        ┌───────────────────────────────┐
        │  target-service (Cloud Run)   │  ← the "production" app under incident
        │  /health 503 without config   │
        └───────────────────────────────┘
```

## Tech stack (satisfies both required categories)

- **Google Cloud AI**: **Vertex AI Gemini 2.5 Flash**, driven by the **Google Agent
  Development Kit (ADK, `google-adk>=1.34,<2`)** as a single tool-using ReAct agent.
- **Google Cloud products**: **Cloud Run** (agent-service + target-service),
  **Cloud Logging** (grounded evidence), **Secret Manager** (GitHub token).
- **Backend**: Python 3.12 + FastAPI. **Frontend**: a single self-contained console
  page served by the agent-service (same-origin, no separate deploy).

## Why Gemini (and not just any LLM)

The architecture deliberately keeps the probabilistic core swappable — merge/deploy
runs on a deterministic, LLM-free pipeline — yet Vertex AI Gemini 2.5 Flash is a
considered choice, not a default:

- **Latency is the product.** On-call value decays by the second. The measured
  27–30 s investigation is a direct consequence of Flash's low latency.
- **Production logs never leave the project boundary.** Logs and configs carry
  secrets and PII. Diagnosis runs inside the same GCP project as the workloads:
  no egress to an external LLM API, no API-key lifecycle to manage (auth is ADC
  from the Cloud Run service account), and every model call is captured by Cloud
  Audit Logs. For an SRE tool that reads raw production evidence, that
  auditability bar is hard to meet with any external API.
- **Zero-idle economics.** Pay-per-call Gemini + scale-to-zero Cloud Run keeps a
  24/7 on-call agent at ~$0/month at rest.
- **ADK-native.** The agent is an ADK `LlmAgent` running a multi-step ReAct tool
  loop on Vertex — auth, retries and tool dispatch come from the platform, not
  hand-rolled glue.

## Repository layout

```
packages/agent/
  src/agents/
    server.py        FastAPI app: / (console), /incident, /approve, /target-health, /health, /smoke
    cli.py           the `autosre` command: doctor / run / approve / status
    incident.py      the incident prompt, shared by every entry point
    agent.py         ADK ReAct agent (build_agent, run_incident)
    tools.py         read-only investigation tools (Cloud Run + Logging + health probe)
    github_tools.py  open_pull_request (autonomous)
    recovery.py      merge + apply env fix + verify recovery (human-gated)
    static/index.html  the Incident console UI
  Dockerfile         Cloud Run container (uvicorn)
services/target-service/   the demo "production" app that breaks without DATABASE_URL
scripts/
  target-incident.ps1      inject / restore the demo incident
  test_agent_local.py      local end-to-end validation (no Cloud Build)
  test_recovery_local.py   local recovery validation
terraform/                 the whole stack as code - see the Quickstart's Track B
docs/sprint-4day-autosre.md  the plan + engineering log
```

## Use it from a terminal — the `autosre` CLI

The agent has three entry points onto **one** core: a Pub/Sub push (the 2am path), the
web console, and this command. Nothing is reimplemented for the CLI — `autosre run` is
the same ReAct loop, the same cost guard, and the same approval gate the console drives.

```bash
pip install -e packages/agent   # puts `autosre` on PATH

autosre doctor                  # what is this machine still missing?
autosre run --dry-run           # investigate and diagnose, open no PR
autosre run                     # ...and open the fix PR for real
autosre approve 42              # the gate: merge, apply, verify /health is 200 again
autosre status                  # cost guard budget, trust ledger, target health
```

`--dry-run` is enforced inside `open_pull_request` — the one function that writes to
GitHub — not by the caller remembering to behave. A rehearsal is also kept out of case
memory: the trust ledger counts verified recoveries, and a run that never proposed a fix
must not move the bar that decides what may act unattended.

`doctor` runs first for a reason: it reports the configuration, the packages and the
Google credentials as a checklist with the fix on each failing line, instead of failing
later inside a run. Secrets are reported as `set (hidden)` and never echoed.

Already have it deployed? Every verb takes `--remote` and talks to the running service
over the same routes the console uses, so nothing needs to be configured locally:

```bash
autosre run --remote https://your-agent.run.app --key "$AUTOSRE_CONSOLE_KEY"
```

Exit codes, because this is meant to be scriptable:

| code | meaning |
|------|---------|
| `0` | a fix PR was opened, or the service really is healthy |
| `1` | error — including `undetermined`, i.e. the final answer could not be read |
| `2` | usage |
| `3` | escalated: the fix is outside the auto-remediation allowlist, a human must act |
| `4` | blocked by the cost guard (cooldown, daily limit, kill switch) |

`undetermined` deliberately exits non-zero: a run whose answer could not be parsed is
not a healthy service. That distinction came from a real production failure — see
`agents/diagnosis.py`.

## Cost guard

The agent holds a durable run budget (cooldown, daily limit, kill switch) in a single
GCS object, so the ceiling survives cold starts and `maxScale > 1`. It is default-off,
and a disabled guard still answers "run allowed" — so it has to be *read*, not inferred
from runs succeeding. `GET /guard` reports whether it is armed; the daily-limit self-trip
stays tripped until a human clears it.

See [docs/cost-guard-runbook.md](docs/cost-guard-runbook.md) for the pre-demo check and
how to clear a tripped kill switch.

## Roadmap (designed for, deliberately out of hackathon scope)

These were scoped out to ship one deep, reliable vertical slice in the hackathon window,
but the architecture is built for them:

- **Self-Improving autonomy policy** — learn per-scenario autonomy thresholds from past-incident approve-vs-override rates, with shadow mode, a minimum-sample guard, and never auto-escalating destructive actions. The `confidence` the agent already emits is the seed signal.
- **Multi-Agent Debate** — considered and **deliberately skipped**: 2025 research shows a single grounded agent outperforms debate on well-scoped RCA while adding cost, latency, and JSON-fragility.
- ~~**Full Cloud Monitoring wiring**~~ — **shipped**: a Monitoring uptime check + alert policy + Pub/Sub notification channel are live; the real detection chain (uptime 503 → alert → Pub/Sub → OIDC-verified push → autonomous run → PR) has fired end-to-end in production. It is Terraform-managed in `terraform/monitoring.tf`.
- **Incident history** — Firestore-backed audit trail and dedupe.
- ~~**One-click deploy**~~ — **shipped**: `terraform/` reproduces the entire stack on a
  fresh project. Two bootstrap steps stay manual by design — the container images, and the
  GitHub token value, which never belongs in Terraform state.
- **More scenarios** — 5xx spikes, memory leaks, dependency CVEs (the tool interface already exposes revisions and status via `get_service_status`).

## License

Apache 2.0 — see [LICENSE](LICENSE).
