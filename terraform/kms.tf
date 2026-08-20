# ---------------------------------------------------------------------------
# Service Directory: how Cloud EKM reaches the ekm VM inside qkd-vpc.
# ---------------------------------------------------------------------------

resource "google_service_directory_namespace" "ekm" {
  provider = google-beta

  namespace_id = "qkd-ekm-${random_id.suffix.hex}" # suffixed for the same reason as the VPC name
  location     = var.region
  project      = var.project_id

  depends_on = [google_project_service.services]
}

resource "google_service_directory_service" "ekm" {
  provider = google-beta

  service_id = "ekm"
  namespace  = google_service_directory_namespace.ekm.id
}

resource "google_service_directory_endpoint" "ekm" {
  provider = google-beta

  endpoint_id = "ekm"
  service     = google_service_directory_service.ekm.id

  address = google_compute_address.ekm_internal.address
  port    = 8443
  network = "projects/${local.project_number}/locations/global/networks/${google_compute_network.qkd.name}"
}

# ---------------------------------------------------------------------------
# Cloud EKM connection (EKM via VPC, manual key management).
# ---------------------------------------------------------------------------

# Cloud KMS EKM connections (like key rings and keys) cannot be deleted: `terraform
# destroy` only forgets them, so the name carries the deployment suffix to let a fresh
# apply in the same project succeed. Forgotten connections linger at no cost.
resource "google_kms_ekm_connection" "ekm" {
  name                = "qkd-ekm-${random_id.suffix.hex}"
  location            = var.region
  project             = var.project_id
  key_management_mode = "MANUAL"

  service_resolvers {
    service_directory_service = google_service_directory_service.ekm.id
    hostname                  = "ekm.qkd.internal"

    server_certificates {
      raw_der = local.ekm_cert_der_b64
    }
  }

  # Cloud KMS opens a connection to the EKM host while creating this resource, so the
  # EKM VM must already be serving TLS on :8443 (null_resource.ekm_ready) and the
  # ingress rule for 35.199.192.0/19 must exist.
  depends_on = [
    google_service_directory_endpoint.ekm,
    google_project_iam_member.ekms_service_directory,
    google_compute_firewall.ekm_from_cloud_ekm,
    null_resource.ekm_ready,
  ]
}

# ---------------------------------------------------------------------------
# Key ring + EXTERNAL_VPC key backed by the EKM connection.
# ---------------------------------------------------------------------------

# The VMs need the key's resource name in their env before the key exists (the key is
# created only after ekm-vm is serving, because Cloud KMS probes the EKM host when the
# EKM connection is created). The name is fully determined at plan time, so compute it.
locals {
  key_ring_name = "qkd-ekm-${random_id.suffix.hex}"
  kms_key_name  = "projects/${var.project_id}/locations/${var.region}/keyRings/${local.key_ring_name}/cryptoKeys/qkd-external-key"
}

resource "google_kms_key_ring" "main" {
  name     = local.key_ring_name
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.services]
}

resource "google_kms_crypto_key" "external" {
  name     = "qkd-external-key"
  key_ring = google_kms_key_ring.main.id
  purpose  = "ENCRYPT_DECRYPT"

  version_template {
    protection_level = "EXTERNAL_VPC"
    algorithm        = "EXTERNAL_SYMMETRIC_ENCRYPTION"
  }

  crypto_key_backend            = google_kms_ekm_connection.ekm.id
  skip_initial_version_creation = true
}

# `google_compute_instance.vm` is "created", not "serving": the startup script still has to
# install the wheel and start the unit, which takes a couple of minutes. Cloud KMS calls the
# EKM synchronously while creating the EKM connection and again while creating v1, so wait
# until /healthz answers on ekm-vm before asking for either. This runs from the operator's
# workstation over IAP, so the identity running `terraform apply` must be in
# `var.operator_emails` (IAP tunnel + OS Login).
resource "null_resource" "ekm_ready" {
  triggers = {
    instance = google_compute_instance.vm["ekm"].instance_id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/sh", "-c"]
    command     = <<-EOT
      i=0
      while [ $i -lt 40 ]; do
        if gcloud compute ssh ${local.vm_name["ekm"]} --zone ${var.zone} --project ${var.project_id} \
             --tunnel-through-iap --quiet --command 'curl -skf https://localhost:8443/healthz >/dev/null && curl -skf -H "Authorization: Bearer $(sudo sed -n "s/^VPN_TOKEN=//p" /etc/qkd-ekm/env)" https://localhost:8443/api/state | grep -q "\"source_available\": *true" && curl -skf -H "Authorization: Bearer $(sudo sed -n "s/^VPN_TOKEN=//p" /etc/qkd-ekm/env)" https://localhost:8443/api/state | grep -qE "\"pool\": *\{[^}]*: *[1-9]"'; then
          echo "ekm-vm is serving and its QKD pool is filled"
          exit 0
        fi
        i=$((i + 1))
        echo "ekm-vm not ready yet (attempt $i/40); retrying in 15s"
        sleep 15
      done
      echo "ekm-vm never became ready (healthz + filled QKD pool) within 10 minutes; see docs/troubleshooting.md" >&2
      exit 1
    EOT
  }

  depends_on = [
    google_compute_instance.vm,
    google_compute_firewall.iap_ssh,
    google_project_iam_member.operator_iap_tunnel,
    google_project_iam_member.operator_os_login,
  ]
}

# The first version binds to the EKM's api/keys/v1 path. Cloud KMS calls the EKM VM
# synchronously while creating it, so everything on that call path must exist first:
# the VM itself (and its service, see null_resource.ekm_ready), the Service Directory
# endpoint, the EKM connection, and the two firewall rules that admit Cloud EKM
# (35.199.192.0/19) and the VPC to :8443.
resource "google_kms_crypto_key_version" "v1" {
  crypto_key = google_kms_crypto_key.external.id
  state      = "ENABLED"

  external_protection_level_options {
    ekm_connection_key_path = "api/keys/v1"
  }

  depends_on = [
    google_compute_instance.vm,
    null_resource.ekm_ready,
    google_service_directory_endpoint.ekm,
    google_kms_ekm_connection.ekm,
    google_compute_firewall.ekm_from_cloud_ekm,
    google_compute_firewall.ekm_internal,
  ]
}

# The provider has no "primary version" attribute on google_kms_crypto_key,
# so promote v1 with gcloud once it exists.
resource "null_resource" "primary_version" {
  triggers = {
    crypto_key = google_kms_crypto_key.external.id
    version    = google_kms_crypto_key_version.v1.name
  }

  provisioner "local-exec" {
    command = join(" ", [
      "gcloud kms keys update ${google_kms_crypto_key.external.name}",
      "--keyring ${google_kms_key_ring.main.name}",
      "--location ${var.region}",
      "--project ${var.project_id}",
      "--primary-version 1",
    ])
  }
}
