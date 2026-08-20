#!/bin/bash
# Managed by Terraform (templatefile). Role: ekm-vm.
set -euo pipefail

${common}

qkd_install_base
qkd_install_dist
qkd_write_env
qkd_write_secret ekm_tls_cert /etc/qkd-ekm/ekm.crt
qkd_write_secret ekm_tls_key /etc/qkd-ekm/ekm.key
qkd_write_secret qkd_ca_cert /etc/qkd-ekm/qkdsim.crt
qkd_install_units qkd-ekm-ekm.service qkd-ekm-rotate.service qkd-ekm-rotate.timer
qkd_rotate_interval
qkd_enable_units qkd-ekm-ekm.service qkd-ekm-rotate.timer
qkd_log "ekm-vm ready"
