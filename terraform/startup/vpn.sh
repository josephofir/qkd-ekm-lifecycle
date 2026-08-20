#!/bin/bash
# Managed by Terraform (templatefile). Role: vpn-vm.
set -euo pipefail

${common}

qkd_install_base
qkd_install_dist
qkd_write_env
qkd_write_secret ekm_ca_cert /etc/qkd-ekm/ekm.crt
qkd_hosts_ekm

# WireGuard needs forwarding, NAT towards the workload subnet, and a server private key.
qkd_vpn_forwarding

if [ ! -s /etc/qkd-ekm/wg_private.key ]; then
  umask 077
  wg genkey > /etc/qkd-ekm/wg_private.key
fi
chmod 600 /etc/qkd-ekm/wg_private.key

qkd_install_units qkd-ekm-vpn.service
qkd_enable_units qkd-ekm-vpn.service
qkd_log "vpn-vm ready"
