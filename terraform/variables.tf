variable "project_id" {
  description = "Target GCP project id (created by scripts/gcp_bootstrap_project.sh)."
  type        = string
}

variable "region" {
  description = "Region for the VPCs, subnets, Service Directory, KMS key ring and EKM connection."
  type        = string
  default     = "me-west1"
}

variable "zone" {
  description = "Zone for all five VMs."
  type        = string
  default     = "me-west1-a"
}

variable "operator_emails" {
  description = "Human operators: IAP tunnel + OS Login access, and allowed callers of the VPN control API."
  type        = list(string)
  default     = []
}

variable "machine_type" {
  description = "Machine type for every VM."
  type        = string
  default     = "e2-small"
}

variable "dist_tarball" {
  description = "Path to the bootstrap tarball built by scripts/build_dist.sh."
  type        = string
  default     = "../dist/qkd-ekm-lifecycle.tar.gz"
}

variable "qkd_backend" {
  description = "'sim' (deploy the ETSI-014 simulator VM and point the EKM/client at it) or 'heqa' (real Sceptre pair)."
  type        = string
  default     = "sim"

  validation {
    condition     = contains(["sim", "heqa"], var.qkd_backend)
    error_message = "qkd_backend must be either 'sim' or 'heqa'."
  }
}

variable "qkd1_url" {
  description = "ETSI-014 base URL of QKD1 (cloud side). Used only when qkd_backend = \"heqa\"."
  type        = string
  default     = ""
}

variable "qkd2_url" {
  description = "ETSI-014 base URL of QKD2 (client side). Used only when qkd_backend = \"heqa\"."
  type        = string
  default     = ""
}

variable "qkd_ca_pem" {
  description = "PEM CA bundle that validates the QKD appliances' TLS certificates. Used only when qkd_backend = \"heqa\"."
  type        = string
  default     = ""
}

variable "qkd_token" {
  description = "Bearer token for the QKD appliances. Used only when qkd_backend = \"heqa\"."
  type        = string
  default     = ""
  sensitive   = true
}

variable "rotation_minutes" {
  description = "Interval of the EKM key-rotation systemd timer, in minutes."
  type        = number
  default     = 15
}

variable "vpn_tunnel_cidr" {
  description = "CIDR of the WireGuard tunnel network. Used by the workload firewall rule, the VPN server address and the NAT rule on vpn-vm."
  type        = string
  default     = "10.20.0.0/24"
}

variable "ekm_pool_target" {
  description = "Number of pre-fetched QKD keys the EKM keeps per peer."
  type        = number
  default     = 50
}

variable "ekm_pool_ttl" {
  description = "Lifetime of a pooled QKD key on the EKM, in seconds."
  type        = number
  default     = 600
}

variable "vpn_refresh_seconds" {
  description = "VPN pre-shared-key refresh period advertised to clients, in seconds."
  type        = number
  default     = 3600
}

variable "vpn_activation_delay_seconds" {
  description = "Coordinated activation delay for a refreshed VPN pre-shared key, in seconds."
  type        = number
  default     = 30
}

variable "ekm_jwt_audiences" {
  description = "Comma-separated list of accepted 'aud' values in the Cloud KMS OIDC token. Observed on the first apply (2026-08-19, me-west1): Cloud EKM sets aud to https://<ekm hostname>, i.e. https://ekm.qkd.internal, which is the default. Empty disables the audience check (signature, issuer and the gcp-sa-ekms e-mail are still enforced); if the EKM log shows `JWT rejected: audience not allowed aud=...`, put that value here."
  type        = string
  default     = "https://ekm.qkd.internal"
}
