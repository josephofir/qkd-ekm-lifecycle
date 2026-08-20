#!/bin/bash
# Managed by Terraform (templatefile). Role: client-vm.
# No service unit: run_experiment.sh drives qkd-ekm-client over SSH.
set -euo pipefail

${common}

qkd_install_base
qkd_install_dist
qkd_write_env
qkd_write_secret qkd_ca_cert /etc/qkd-ekm/qkdsim.crt
qkd_client_readable_config

# Make the CLI available to interactive shells.
ln -sf /opt/qkd-ekm/venv/bin/qkd-ekm-client /usr/local/bin/qkd-ekm-client
qkd_log "client-vm ready"
