output "project_number" {
  description = "Numeric id of the project (used to derive the Google service-agent emails)."
  value       = local.project_number
}

output "zone" {
  description = "Zone every VM lives in."
  value       = var.zone
}

output "vpn_vm_name" {
  description = "Name of the WireGuard / VPN control VM."
  value       = google_compute_instance.vm["vpn"].name
}

output "client_vm_name" {
  description = "Name of the client VM that drives the experiment."
  value       = google_compute_instance.vm["client"].name
}

output "vpn_external_ip" {
  description = "Public IP of the VPN VM (WireGuard endpoint, udp/51819)."
  value       = google_compute_address.vpn_external.address
}

output "client_external_ip" {
  description = "Public IP of the client VM (allow-listed on the QKD simulator)."
  value       = google_compute_address.client.address
}

output "qkdsim_external_ip" {
  description = "Public IP of the QKD simulator VM (QKD2 endpoint for the client)."
  value       = google_compute_address.qkdsim_external.address
}

output "workload_internal_ip" {
  description = "Internal IP of the file-upload workload VM (reachable over the tunnel, tcp/8081)."
  value       = google_compute_address.workload_internal.address
}

output "data_bucket" {
  description = "CMEK-protected bucket the upload service writes to."
  # local, not google_storage_bucket.data.name: the bucket is created after the KMS key
  # version, which is created after the VMs, which read this name from their env file.
  value = local.data_bucket_name
}

output "kms_key" {
  description = "Full resource name of the EXTERNAL_VPC CryptoKey."
  value       = google_kms_crypto_key.external.id
}

output "ekm_connection" {
  description = "Full resource name of the Cloud EKM connection."
  value       = google_kms_ekm_connection.ekm.id
}

output "sim_token" {
  description = "Bearer token for the QKD simulator."
  value       = random_password.sim_token.result
  sensitive   = true
}

output "sim_user" {
  description = "Username for the QKD simulator's /auth/login (capture script)."
  value       = local.sim_user
  sensitive   = true
}

output "sim_password" {
  description = "Password for the QKD simulator's /auth/login (capture script)."
  value       = random_password.sim_password.result
  sensitive   = true
}

output "vpn_token" {
  description = "Shared bearer token between the VPN/upload services and the EKM."
  value       = random_password.vpn_token.result
  sensitive   = true
}

output "ssh_client_cmd" {
  description = "Command that opens a shell on the client VM through IAP."
  value       = "gcloud compute ssh ${google_compute_instance.vm["client"].name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}

output "iap_tunnel_cmd" {
  description = "Command that forwards the VPN control API (:8080) to localhost."
  value       = "gcloud compute start-iap-tunnel ${google_compute_instance.vm["vpn"].name} 8080 --local-host-port=localhost:8080 --zone ${var.zone} --project ${var.project_id}"
}
