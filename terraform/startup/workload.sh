#!/bin/bash
# Managed by Terraform (templatefile). Role: workload-vm.
set -euo pipefail

${common}

qkd_install_base
qkd_install_dist
qkd_write_env
qkd_write_secret ekm_ca_cert /etc/qkd-ekm/ekm.crt
qkd_hosts_ekm
qkd_install_units qkd-ekm-upload.service
qkd_enable_units qkd-ekm-upload.service
qkd_log "workload-vm ready"
