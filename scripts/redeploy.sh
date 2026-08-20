#!/usr/bin/env bash
# Rebuild the wheel, upload it, and re-run the bootstrap on the VMs so a code change
# lands without recreating infrastructure. Usage: scripts/redeploy.sh [vm-role ...]
# (default: all five roles).
set -euo pipefail
cd "$(dirname "$0")/.."
roles=("$@")
[ ${#roles[@]} -gt 0 ] || roles=(ekm vpn workload qkdsim client)

scripts/build_dist.sh
terraform -chdir=terraform apply -input=false -auto-approve >/dev/null
zone=$(terraform -chdir=terraform output -raw zone)
project=$(terraform -chdir=terraform output -json | jq -r '.kms_key.value | split("/")[1]')
for r in "${roles[@]}"; do
  echo "== ${r}-vm: reinstalling wheel and restarting services"
  # shellcheck disable=SC2016  # $1/$3 expand on the VM, inside awk
  # The reinstall's exit code is saved as $rc and re-raised at the end (`exit $rc`), so a
  # failed `google_metadata_script_runner startup` fails this ssh command loudly instead of
  # being masked by the `;`-joined restart/list-units that follow it. The restart itself
  # keeps its `|| true`: not every role has every qkd-ekm-* unit, so a non-matching glob is
  # expected, not an error.
  gcloud compute ssh "${r}-vm" --zone "$zone" --project "$project" --tunnel-through-iap --quiet \
    --command 'sudo rm -f /opt/qkd-ekm/.installed && sudo google_metadata_script_runner startup >/dev/null 2>&1; rc=$?; sudo systemctl restart "qkd-ekm-*.service" 2>/dev/null || true; systemctl list-units "qkd-ekm-*" --no-legend --plain | awk "{print \$1, \$3}"; exit $rc'
done
