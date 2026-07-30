# Durable run-guard state (cooldown / daily budget / kill switch).
#
# One small JSON object updated with generation-preconditioned compare-and-swap
# (see packages/agent/src/agents/state_store.py). The bucket was originally
# created out-of-band with gcloud when the guard first shipped; on the live
# project, `terraform import` it before the first apply (see README.md) or the
# apply fails with a 409.

resource "google_storage_bucket" "autosre_state" {
  name     = "${var.project_id}-autosre-state"
  location = var.region
  project  = var.project_id

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Demo stack, same stance as deletion_protection=false on the services.
  # The object inside is 150 bytes of rebuildable counters, not data.
  force_destroy = true
}

# The CAS write path overwrites the state object in place, which needs
# objects.delete as well as objects.create - hence objectAdmin, scoped to this
# one bucket rather than granted at project level. Without this the guard
# degrades to (allowed=True, reason="unavailable") and every ceiling silently
# stops accumulating; see docs/cost-guard-runbook.md.
resource "google_storage_bucket_iam_member" "agent_state_writer" {
  bucket = google_storage_bucket.autosre_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.runtime_sa}"
}
