locals {
  vm_roles = ["ekm", "vpn", "workload", "qkdsim", "client"]
}

resource "google_service_account" "vm" {
  for_each = toset(local.vm_roles)

  account_id   = "${each.key}-vm"
  display_name = "qkd-ekm ${each.key} VM"
  project      = var.project_id

  depends_on = [google_project_service.services]
}

# ---------------------------------------------------------------------------
# Every VM: pull the bootstrap tarball and write logs.
# ---------------------------------------------------------------------------

resource "google_storage_bucket_iam_member" "dist_reader" {
  for_each = google_service_account.vm

  bucket = google_storage_bucket.dist.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "log_writer" {
  for_each = google_service_account.vm

  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${each.value.email}"
}

# ---------------------------------------------------------------------------
# Per-role permissions.
# ---------------------------------------------------------------------------

# The rotation timer creates new CryptoKeyVersions and re-points the primary.
resource "google_kms_key_ring_iam_member" "ekm_admin" {
  key_ring_id = google_kms_key_ring.main.id
  role        = "roles/cloudkms.admin"
  member      = "serviceAccount:${google_service_account.vm["ekm"].email}"
}

resource "google_storage_bucket_iam_member" "workload_data" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.vm["workload"].email}"
}

# The client VM drives the experiment: it opens an IAP tunnel to vpn-vm.
# `gcloud compute start-iap-tunnel` also needs the tunnel role at project level -- the
# per-instance grant below covers the tunnel itself, but the IAP handshake is checked
# against the project resource as well.
resource "google_project_iam_member" "client_iap" {
  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "serviceAccount:${google_service_account.vm["client"].email}"
}

# `start-iap-tunnel` resolves the target instance first (compute.instances.get), so the
# client SA needs compute.viewer -- but only on vpn-vm, not across the project.
resource "google_compute_instance_iam_member" "client_compute_viewer" {
  project       = var.project_id
  zone          = var.zone
  instance_name = google_compute_instance.vm["vpn"].name
  role          = "roles/compute.viewer"
  member        = "serviceAccount:${google_service_account.vm["client"].email}"
}

# ---------------------------------------------------------------------------
# Google service agents.
# ---------------------------------------------------------------------------

# Cloud EKM resolves the ekm VM through Service Directory / PSC.
resource "google_project_iam_member" "ekms_service_directory" {
  for_each = toset([
    "roles/servicedirectory.viewer",
    "roles/servicedirectory.pscAuthorizedService",
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.ekms_sa_email}"

  depends_on = [
    google_project_service_identity.kms,
    null_resource.service_identities,
  ]
}

# GCS encrypts/decrypts objects in the data bucket with the CMEK key.
resource "google_kms_crypto_key_iam_member" "gcs_encrypter_decrypter" {
  crypto_key_id = google_kms_crypto_key.external.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${local.gcs_sa_email}"
}

# ---------------------------------------------------------------------------
# Operators.
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "operator_os_login" {
  for_each = toset(var.operator_emails)

  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = "user:${each.key}"
}

# Operators run scripts/run_experiment.sh, which needs a shell on every VM: the preflight
# health checks and the EKM / upload-server logs that the S2 transcript is built from live on
# ekm-vm, workload-vm and qkdsim-vm. Project level, because IAP tunnel access is per instance
# and enumerating five of them buys nothing here.
resource "google_project_iam_member" "operator_iap_tunnel" {
  for_each = toset(var.operator_emails)

  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "user:${each.key}"
}

# client-vm opens an IAP tunnel to the VPN control API from inside the experiment, so its
# service account needs tunnel access to vpn-vm -- and to nothing else. Operators are covered
# by the project-level grant above; a per-instance binding for them would be redundant.
resource "google_iap_tunnel_instance_iam_member" "client_to_vpn" {
  project  = var.project_id
  zone     = var.zone
  instance = google_compute_instance.vm["vpn"].name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.vm["client"].email}"
}
