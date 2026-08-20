locals {
  startup_common = file("${path.module}/startup/common.sh")

  # /etc/qkd-ekm/env bodies. Variable names are a contract with the Python services.
  env_files = {
    ekm = join("\n", [
      "EKM_DB=/var/lib/qkd-ekm/ekm.sqlite",
      "EKM_LOCAL_KEY_FILE=/var/lib/qkd-ekm/local.key",
      "QKD1_URL=${local.qkd1_url}",
      "QKD_CA_FILE=/etc/qkd-ekm/qkdsim.crt",
      "QKD_TOKEN=${local.qkd_token}",
      "EKM_PEERS=QKD2",
      "EKM_POOL_TARGET=${var.ekm_pool_target}",
      "EKM_POOL_TTL=${var.ekm_pool_ttl}",
      "EKMS_SA_EMAIL=${local.ekms_sa_email}",
      "EKM_JWT_AUDIENCES=${var.ekm_jwt_audiences}",
      "VPN_TOKEN=${random_password.vpn_token.result}",
      "EKM_PORT=8443",
      "EKM_TLS_CERT=/etc/qkd-ekm/ekm.crt",
      "EKM_TLS_KEY=/etc/qkd-ekm/ekm.key",
      "KMS_KEY=${local.kms_key_name}",
      "EKM_KEY_PATH_PREFIX=api/keys/",
      "LOG_FILE=/var/log/qkd-ekm/ekm.log",
      "",
    ])

    vpn = join("\n", [
      "VPN_IFACE=wg0",
      "VPN_LISTEN_PORT=51819",
      "VPN_ADDRESS=${cidrhost(var.vpn_tunnel_cidr, 1)}/${split("/", var.vpn_tunnel_cidr)[1]}",
      "VPN_PRIVATE_KEY_FILE=/etc/qkd-ekm/wg_private.key",
      "VPN_TUNNEL_CIDR=${var.vpn_tunnel_cidr}",
      "VPN_ALLOWED_IPS=${local.qkd_cidr},${var.vpn_tunnel_cidr}",
      "VPN_PUBLIC_ENDPOINT=${google_compute_address.vpn_external.address}:51819",
      "EKM_URL=${local.ekm_url}",
      "EKM_CA_FILE=/etc/qkd-ekm/ekm.crt",
      "VPN_TOKEN=${random_password.vpn_token.result}",
      "VPN_ALLOWED_EMAILS=${local.vpn_allowed_emails}",
      "VPN_ALLOWED_AUDIENCES=32555940559.apps.googleusercontent.com,qkd-ekm-vpn",
      "VPN_PORT=8080",
      "VPN_REFRESH_S=${var.vpn_refresh_seconds}",
      "VPN_ACTIVATION_DELAY_S=${var.vpn_activation_delay_seconds}",
      "LOG_FILE=/var/log/qkd-ekm/vpn.log",
      "",
    ])

    workload = join("\n", [
      "UPLOAD_PORT=8081",
      "EKM_URL=${local.ekm_url}",
      "EKM_CA_FILE=/etc/qkd-ekm/ekm.crt",
      "VPN_TOKEN=${random_password.vpn_token.result}",
      "GCS_BUCKET=${local.data_bucket_name}",
      "KMS_KEY_NAME=${local.kms_key_name}",
      "LOG_FILE=/var/log/qkd-ekm/upload.log",
      "",
    ])

    qkdsim = join("\n", [
      "SIM_PORT=8200",
      "SIM_SAES=QKD1,QKD2",
      "SIM_TOKEN=${random_password.sim_token.result}",
      "SIM_TLS_CERT=/etc/qkd-ekm/qkdsim.crt",
      "SIM_TLS_KEY=/etc/qkd-ekm/qkdsim.key",
      "SIM_USER=${local.sim_user}",
      "SIM_PASSWORD=${random_password.sim_password.result}",
      "LOG_FILE=/var/log/qkd-ekm/qkdsim.log",
      "",
    ])

    client = join("\n", [
      "QKD2_URL=${local.qkd2_url}",
      "QKD_CA_FILE=/etc/qkd-ekm/qkdsim.crt",
      "QKD_TOKEN=${local.qkd_token}",
      "VPN_VM_NAME=${local.vm_name["vpn"]}",
      "ZONE=${var.zone}",
      "VPN_EXTERNAL_IP=${google_compute_address.vpn_external.address}",
      "WORKLOAD_IP=${google_compute_address.workload_internal.address}",
      "SIM_TOKEN=${random_password.sim_token.result}",
      "SIM_USER=${local.sim_user}",
      "SIM_PASSWORD=${random_password.sim_password.result}",
      "LOG_FILE=/var/log/qkd-ekm/client.log",
      "",
    ])
  }

  vm_common_metadata = {
    enable-oslogin   = "TRUE"
    dist_url         = local.dist_url
    ekm_internal_ip  = google_compute_address.ekm_internal.address
    rotation_minutes = tostring(var.rotation_minutes)
  }

  vms = {
    ekm = {
      subnetwork     = google_compute_subnetwork.qkd.id
      network_ip     = google_compute_address.ekm_internal.address
      nat_ip         = null
      tags           = ["ekm"]
      can_ip_forward = false
      metadata = {
        ekm_tls_cert = tls_self_signed_cert.ekm.cert_pem
        ekm_tls_key  = tls_private_key.ekm.private_key_pem
        qkd_ca_cert  = local.qkd_ca_pem
      }
    }

    # can_ip_forward: the tunnel (10.20.0.0/24) is not a GCP subnet, so packets the
    # WireGuard peer sends on to workload-vm leave vpn-vm with a foreign source address.
    # Without this the VPC drops them; the NAT rule in startup/vpn.sh does the rest.
    vpn = {
      subnetwork     = google_compute_subnetwork.qkd.id
      network_ip     = null
      nat_ip         = google_compute_address.vpn_external.address
      tags           = ["vpn"]
      can_ip_forward = true
      metadata = {
        ekm_ca_cert     = tls_self_signed_cert.ekm.cert_pem
        vpn_tunnel_cidr = var.vpn_tunnel_cidr
      }
    }

    workload = {
      subnetwork     = google_compute_subnetwork.qkd.id
      network_ip     = google_compute_address.workload_internal.address
      nat_ip         = null
      tags           = ["workload"]
      can_ip_forward = false
      metadata = {
        ekm_ca_cert = tls_self_signed_cert.ekm.cert_pem
      }
    }

    qkdsim = {
      subnetwork     = google_compute_subnetwork.qkd.id
      network_ip     = google_compute_address.qkdsim_internal.address
      nat_ip         = google_compute_address.qkdsim_external.address
      tags           = ["qkdsim"]
      can_ip_forward = false
      metadata = {
        qkdsim_tls_cert = tls_self_signed_cert.qkdsim.cert_pem
        qkdsim_tls_key  = tls_private_key.qkdsim.private_key_pem
      }
    }

    client = {
      subnetwork     = google_compute_subnetwork.client.id
      network_ip     = null
      nat_ip         = google_compute_address.client.address
      tags           = ["client"]
      can_ip_forward = false
      metadata = {
        qkd_ca_cert = local.qkd_ca_pem
      }
    }
  }
}

resource "google_compute_instance" "vm" {
  for_each = local.vms

  name         = local.vm_name[each.key]
  project      = var.project_id
  zone         = var.zone
  machine_type = var.machine_type
  tags         = each.value.tags

  allow_stopping_for_update = true
  can_ip_forward            = each.value.can_ip_forward

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    subnetwork = each.value.subnetwork
    network_ip = each.value.network_ip

    # Ephemeral (or reserved) external IP on every VM: cheaper than Cloud NAT and
    # still needed for apt/gsutil. "Internal only" VMs are enforced by the absence
    # of any internet-sourced ingress firewall rule, not by the lack of an address.
    access_config {
      nat_ip = each.value.nat_ip
    }
  }

  service_account {
    email  = google_service_account.vm[each.key].email
    scopes = ["cloud-platform"]
  }

  metadata = merge(
    local.vm_common_metadata,
    each.value.metadata,
    {
      env_file       = local.env_files[each.key]
      startup-script = templatefile("${path.module}/startup/${each.key}.sh", { common = local.startup_common })
    },
  )

  depends_on = [
    google_storage_bucket_object.dist,
    google_storage_bucket_iam_member.dist_reader,
    google_project_service.services,
  ]
}
