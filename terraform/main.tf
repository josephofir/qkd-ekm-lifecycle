data "google_project" "project" {
  project_id = var.project_id
}

# Service agent for Cloud KMS; the Cloud EKM agent (gcp-sa-ekms) is created alongside it.
resource "google_project_service_identity" "kms" {
  provider = google-beta
  project  = var.project_id
  service  = "cloudkms.googleapis.com"

  depends_on = [google_project_service.services]
}

# google_project_service_identity is not always enough: the Cloud EKM agent
# (service-<num>@gcp-sa-ekms.iam.gserviceaccount.com) is only materialised when the
# ekms service identity is created, and the beta provider has no resource for it.
# Ask gcloud directly. The cloudkms call is authoritative; the ekms one is best effort
# (it fails on projects where the service is not directly enableable — see README).
resource "null_resource" "service_identities" {
  triggers = {
    project = var.project_id
  }

  provisioner "local-exec" {
    command = <<-EOT
      gcloud beta services identity create --service=cloudkms.googleapis.com --project=${var.project_id} --quiet
      gcloud beta services identity create --service=ekms.googleapis.com --project=${var.project_id} --quiet || true
    EOT
  }

  depends_on = [
    google_project_service.services,
    google_project_service_identity.kms,
  ]
}

# Creating this data source materialises the GCS service agent, which needs
# cryptoKeyEncrypterDecrypter on the CMEK key before the data bucket can be created.
data "google_storage_project_service_account" "gcs" {
  project = var.project_id

  depends_on = [google_project_service.services]
}

resource "google_project_service" "services" {
  for_each = toset([
    "compute.googleapis.com",
    "cloudkms.googleapis.com",
    "servicedirectory.googleapis.com",
    "storage.googleapis.com",
    "iap.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "oslogin.googleapis.com",
    "logging.googleapis.com",
  ])

  project                    = var.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false
}

resource "random_id" "suffix" {
  byte_length = 3
}

# Shared bearer token between the VPN control service, the upload service and the EKM.
resource "random_password" "vpn_token" {
  length  = 40
  special = false
}

# Simulator credentials (unused when qkd_backend = "heqa").
resource "random_password" "sim_token" {
  length  = 40
  special = false
}

resource "random_password" "sim_password" {
  length  = 24
  special = false
}

# ---------------------------------------------------------------------------
# TLS material
# ---------------------------------------------------------------------------

resource "tls_private_key" "ekm" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "ekm" {
  private_key_pem = tls_private_key.ekm.private_key_pem
  dns_names       = ["ekm.qkd.internal"]

  subject {
    common_name  = "ekm.qkd.internal"
    organization = "qkd-ekm-lifecycle"
  }

  validity_period_hours = 87600 # 10 years
  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

resource "tls_private_key" "qkdsim" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "qkdsim" {
  private_key_pem = tls_private_key.qkdsim.private_key_pem
  dns_names       = ["qkdsim.qkd.internal"]
  ip_addresses = [
    google_compute_address.qkdsim_internal.address,
    google_compute_address.qkdsim_external.address,
  ]

  subject {
    common_name  = "qkdsim.qkd.internal"
    organization = "qkd-ekm-lifecycle"
  }

  validity_period_hours = 87600 # 10 years
  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "dist" {
  name                        = "qkd-ekm-dist-${random_id.suffix.hex}"
  project                     = var.project_id
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_object" "dist" {
  name         = "dist.tar.gz"
  bucket       = google_storage_bucket.dist.name
  source       = var.dist_tarball
  content_type = "application/gzip"
}

# The bucket *name* is computed in a local (see below) so that the workload VM's env file
# and the outputs never reference this resource. That keeps the graph acyclic while the
# bucket itself is created last: GCS refuses a CMEK key that has no enabled primary version,
# and the primary version can only exist once the EKM VM is serving.
resource "google_storage_bucket" "data" {
  name                        = local.data_bucket_name
  project                     = var.project_id
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true

  encryption {
    default_kms_key_name = google_kms_crypto_key.external.id
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gcs_encrypter_decrypter,
    null_resource.primary_version,
  ]
}

# ---------------------------------------------------------------------------
# Derived values shared by the VM startup scripts
# ---------------------------------------------------------------------------

locals {
  project_number = data.google_project.project.number
  ekms_sa_email  = "service-${local.project_number}@gcp-sa-ekms.iam.gserviceaccount.com"
  gcs_sa_email   = data.google_storage_project_service_account.gcs.email_address

  dist_url = "gs://${google_storage_bucket.dist.name}/${google_storage_bucket_object.dist.name}"

  # Computed, not read back from the resource: see the comment on google_storage_bucket.data.
  data_bucket_name = "qkd-ekm-data-${random_id.suffix.hex}"

  # VM instance names, used by the instances themselves and by the client's env file.
  vm_name = { for role in local.vm_roles : role => "${role}-vm" }

  sim_user = "admin"

  # A PEM certificate body *is* the base64 encoding of the DER bytes, so stripping the
  # armour and the line breaks yields exactly what raw_der wants (base64-encoded DER).
  # No openssl / external data source required.
  ekm_cert_der_b64 = replace(
    replace(
      replace(tls_self_signed_cert.ekm.cert_pem, "-----BEGIN CERTIFICATE-----", ""),
      "-----END CERTIFICATE-----", ""
    ),
    "\n", ""
  )

  # QKD endpoints: the simulator VM when qkd_backend = "sim", the real appliances otherwise.
  qkd1_url   = var.qkd_backend == "sim" ? "https://${google_compute_address.qkdsim_internal.address}:8200" : var.qkd1_url
  qkd2_url   = var.qkd_backend == "sim" ? "https://${google_compute_address.qkdsim_external.address}:8200" : var.qkd2_url
  qkd_ca_pem = var.qkd_backend == "sim" ? tls_self_signed_cert.qkdsim.cert_pem : var.qkd_ca_pem
  qkd_token  = var.qkd_backend == "sim" ? random_password.sim_token.result : var.qkd_token

  ekm_url = "https://ekm.qkd.internal:8443"

  vpn_allowed_emails = join(",", concat(
    var.operator_emails,
    [google_service_account.vm["client"].email],
  ))
}
