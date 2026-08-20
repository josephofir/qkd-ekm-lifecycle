#!/bin/bash
# Managed by Terraform (templatefile). Role: qkdsim-vm.
set -euo pipefail

${common}

qkd_install_base
qkd_install_dist
qkd_write_env
qkd_write_secret qkdsim_tls_cert /etc/qkd-ekm/qkdsim.crt
qkd_write_secret qkdsim_tls_key /etc/qkd-ekm/qkdsim.key
qkd_install_units qkd-ekm-qkdsim.service
qkd_enable_units qkd-ekm-qkdsim.service
qkd_log "qkdsim-vm ready"
