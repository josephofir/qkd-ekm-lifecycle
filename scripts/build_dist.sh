#!/usr/bin/env bash
# Build the bootstrap tarball the VMs download at first boot.
#
#   dist/qkd-ekm-lifecycle.tar.gz
#     wheel/qkd_ekm_lifecycle-*.whl
#     systemd/qkd-ekm-*.service|.timer
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Start clean: a stale wheel from an earlier version would otherwise ship in the tarball
# too, and `pip install dist/wheel/*.whl` on the VMs would see two wheels.
rm -rf dist/wheel dist/stage

uv build --wheel -o dist/wheel

mkdir -p dist/stage
cp -r dist/wheel terraform/systemd dist/stage/

tar czf dist/qkd-ekm-lifecycle.tar.gz -C dist/stage .

echo "built dist/qkd-ekm-lifecycle.tar.gz"
tar tzf dist/qkd-ekm-lifecycle.tar.gz
