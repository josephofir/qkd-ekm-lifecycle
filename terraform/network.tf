locals {
  qkd_cidr    = "10.10.0.0/24"
  tunnel_cidr = var.vpn_tunnel_cidr
  client_cidr = "10.30.0.0/24"

  iap_cidr       = "35.235.240.0/20"
  cloud_ekm_cidr = "35.199.192.0/19"
}

# ---------------------------------------------------------------------------
# VPCs
# ---------------------------------------------------------------------------

resource "google_compute_network" "qkd" {
  # Suffixed: Cloud EKM reaches this network over Private Service Connect and resolves it by
  # name; destroying and recreating a same-named network left Cloud EKM unable to reach the
  # new one ("Timed out when trying to access the EKM host") on the first re-apply cycle.
  name                    = "qkd-vpc-${random_id.suffix.hex}"
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "qkd" {
  name                     = "qkd-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.qkd.id
  ip_cidr_range            = local.qkd_cidr
  private_ip_google_access = true
}

resource "google_compute_network" "client" {
  name                    = "client-vpc"
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "client" {
  name                     = "client-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.client.id
  ip_cidr_range            = local.client_cidr
  private_ip_google_access = true
}

# ---------------------------------------------------------------------------
# Reserved addresses
#
# Every address a startup script needs to bake into /etc/qkd-ekm/env is reserved
# up front, so no VM has to depend on another VM (which would deadlock the
# for_each over google_compute_instance.vm).
# ---------------------------------------------------------------------------

resource "google_compute_address" "ekm_internal" {
  name         = "ekm-internal"
  project      = var.project_id
  region       = var.region
  subnetwork   = google_compute_subnetwork.qkd.id
  address_type = "INTERNAL"
}

resource "google_compute_address" "workload_internal" {
  name         = "workload-internal"
  project      = var.project_id
  region       = var.region
  subnetwork   = google_compute_subnetwork.qkd.id
  address_type = "INTERNAL"
}

resource "google_compute_address" "qkdsim_internal" {
  name         = "qkdsim-internal"
  project      = var.project_id
  region       = var.region
  subnetwork   = google_compute_subnetwork.qkd.id
  address_type = "INTERNAL"
}

resource "google_compute_address" "vpn_external" {
  name         = "vpn-external"
  project      = var.project_id
  region       = var.region
  address_type = "EXTERNAL"

  depends_on = [google_project_service.services]
}

resource "google_compute_address" "qkdsim_external" {
  name         = "qkdsim-external"
  project      = var.project_id
  region       = var.region
  address_type = "EXTERNAL"

  depends_on = [google_project_service.services]
}

resource "google_compute_address" "client" {
  name         = "client-external"
  project      = var.project_id
  region       = var.region
  address_type = "EXTERNAL"

  depends_on = [google_project_service.services]
}

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------

resource "google_compute_firewall" "iap_ssh" {
  name          = "iap-ssh"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.iap_cidr]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "iap_ssh_client" {
  name          = "iap-ssh-client"
  project       = var.project_id
  network       = google_compute_network.client.id
  direction     = "INGRESS"
  source_ranges = [local.iap_cidr]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "iap_vpn_control" {
  name          = "iap-vpn-control"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.iap_cidr]
  target_tags   = ["vpn"]

  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}

resource "google_compute_firewall" "ekm_from_cloud_ekm" {
  name          = "ekm-from-cloud-ekm"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.cloud_ekm_cidr]
  target_tags   = ["ekm"]

  allow {
    protocol = "tcp"
    ports    = ["8443"]
  }
}

resource "google_compute_firewall" "ekm_internal" {
  name          = "ekm-internal"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.qkd_cidr]
  target_tags   = ["ekm"]

  allow {
    protocol = "tcp"
    ports    = ["8443"]
  }
}

resource "google_compute_firewall" "qkdsim_internal" {
  name          = "qkdsim-internal"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.qkd_cidr]
  target_tags   = ["qkdsim"]

  allow {
    protocol = "tcp"
    ports    = ["8200"]
  }
}

resource "google_compute_firewall" "qkdsim_from_client" {
  name          = "qkdsim-from-client"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = ["${google_compute_address.client.address}/32"]
  target_tags   = ["qkdsim"]

  allow {
    protocol = "tcp"
    ports    = ["8200"]
  }
}

resource "google_compute_firewall" "vpn_wg" {
  name          = "vpn-wg"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["vpn"]

  allow {
    protocol = "udp"
    ports    = ["51819"]
  }
}

resource "google_compute_firewall" "workload_from_vpn" {
  name          = "workload-from-vpn"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.tunnel_cidr, local.qkd_cidr]
  target_tags   = ["workload"]

  allow {
    protocol = "tcp"
    ports    = ["8081"]
  }
}

# The S1 check pings workload-vm through the tunnel to show the data plane works, which needs
# ICMP as well as the tcp/8081 rule above. Same sources: the WireGuard tunnel and the VPC.
resource "google_compute_firewall" "workload_icmp" {
  name          = "workload-icmp"
  project       = var.project_id
  network       = google_compute_network.qkd.id
  direction     = "INGRESS"
  source_ranges = [local.tunnel_cidr, local.qkd_cidr]
  target_tags   = ["workload"]

  allow {
    protocol = "icmp"
  }
}
