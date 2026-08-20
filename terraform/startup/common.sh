# shellcheck shell=bash
# Bootstrap helpers, inlined verbatim into every role startup script by Terraform
# (templatefile "${common}"). Injected template values are never re-templated, so
# ordinary shell ${...} expansions in this file are safe.

qkd_log() {
  echo "qkd-ekm-bootstrap: $*"
}

# Read one instance metadata attribute.
qkd_meta() {
  curl -fsS -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}

QKD_PACKAGES="python3-venv python3-pip wireguard iptables"

# apt-get with a long wait for the dpkg/apt locks: on a fresh GCE boot the unattended
# upgrade / apt-daily jobs hold them for a minute or two.
qkd_apt() {
  apt-get -o DPkg::Lock::Timeout=600 "$@"
}

# qkd_apt_retry <apt-get args...> — up to five attempts, 15 s apart.
qkd_apt_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if qkd_apt "$@"; then
      return 0
    fi
    qkd_log "apt-get $1 failed (attempt $attempt/5); retrying in 15s"
    sleep 15
  done
  return 1
}

qkd_pkgs_present() {
  local pkg
  for pkg in $QKD_PACKAGES; do
    [ "$(dpkg-query -W -f='${db:Status-Status}' "$pkg" 2> /dev/null)" = "installed" ] || return 1
  done
}

qkd_install_base() {
  mkdir -p /opt/qkd-ekm /etc/qkd-ekm /var/lib/qkd-ekm /var/log/qkd-ekm
  chmod 700 /etc/qkd-ekm /var/lib/qkd-ekm

  # The startup script runs on every boot; skip the package work once it is done.
  if qkd_pkgs_present; then
    qkd_log "base packages already installed"
    return 0
  fi

  qkd_log "installing base packages"
  export DEBIAN_FRONTEND=noninteractive
  qkd_apt_retry update -y || qkd_log "apt-get update never succeeded; trying the install anyway"
  # shellcheck disable=SC2086
  qkd_apt_retry install -y $QKD_PACKAGES
}

# Download the tarball Terraform uploaded, unpack it and install the wheel.
# Idempotent: on a reboot the venv is already there, so this is a no-op.
qkd_install_dist() {
  local url
  url="$(qkd_meta dist_url)"

  if [ -x /opt/qkd-ekm/venv/bin/python ] && [ -f /opt/qkd-ekm/.installed ] &&
    [ "$(cat /opt/qkd-ekm/.installed)" = "$url" ]; then
    qkd_log "wheel from $url already installed"
    return 0
  fi

  # Insurance: the Debian 12 GCE image ships gsutil, but a minimal/updated image may
  # not, and the whole bootstrap hinges on this one download.
  command -v gsutil > /dev/null 2>&1 || qkd_apt_retry install -y google-cloud-cli

  qkd_log "fetching $url"
  gsutil cp "$url" /opt/qkd-ekm/dist.tar.gz
  rm -rf /opt/qkd-ekm/dist
  mkdir -p /opt/qkd-ekm/dist
  tar xzf /opt/qkd-ekm/dist.tar.gz -C /opt/qkd-ekm/dist
  [ -x /opt/qkd-ekm/venv/bin/python ] || python3 -m venv /opt/qkd-ekm/venv
  /opt/qkd-ekm/venv/bin/pip install --upgrade pip
  /opt/qkd-ekm/venv/bin/pip install --force-reinstall /opt/qkd-ekm/dist/wheel/*.whl
  printf '%s\n' "$url" > /opt/qkd-ekm/.installed
}

# Write /etc/qkd-ekm/env from the env_file metadata attribute.
qkd_write_env() {
  qkd_meta env_file > /etc/qkd-ekm/env
  chmod 600 /etc/qkd-ekm/env
}

# qkd_write_secret <metadata-key> <destination>
qkd_write_secret() {
  qkd_meta "$1" > "$2"
  chmod 600 "$2"
}

# qkd_install_units <unit>...
qkd_install_units() {
  local unit
  for unit in "$@"; do
    install -m 644 "/opt/qkd-ekm/dist/systemd/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
}

# qkd_enable_units <unit>...
qkd_enable_units() {
  local unit
  for unit in "$@"; do
    qkd_log "enabling $unit"
    systemctl enable --now "$unit"
  done
}

# Resolve ekm.qkd.internal to the reserved internal address of the ekm VM.
qkd_hosts_ekm() {
  local ip
  ip="$(qkd_meta ekm_internal_ip)"
  if ! grep -q 'ekm\.qkd\.internal' /etc/hosts; then
    echo "$ip ekm.qkd.internal" >> /etc/hosts
  fi
}

# Turn vpn-vm into a router for the WireGuard tunnel: forwarding on (persisted), and a
# MASQUERADE rule so tunnel-sourced packets reach workload-vm with vpn-vm's own address.
# Idempotent — the VPN service applies the same rule with the same -C/-A guard.
qkd_vpn_forwarding() {
  local cidr iface
  cidr="$(qkd_meta vpn_tunnel_cidr)"
  iface="$(ip -o -4 route show to default | awk '{print $5}' | head -n1)"

  echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-qkd-ekm.conf
  sysctl -w net.ipv4.ip_forward=1

  if [ -z "$iface" ]; then
    qkd_log "no default route interface; skipping MASQUERADE"
    return 0
  fi

  qkd_log "MASQUERADE $cidr out of $iface"
  iptables -t nat -C POSTROUTING -s "$cidr" -o "$iface" -j MASQUERADE 2> /dev/null ||
    iptables -t nat -A POSTROUTING -s "$cidr" -o "$iface" -j MASQUERADE
}

# client-vm only: the runner logs in as an ordinary SSH user, so /etc/qkd-ekm/env and the
# QKD CA have to be world-readable, and the env has to reach non-login `sudo -E` shells too.
# Values here are tokens, URLs and paths — no single quotes — so the quoting below is safe.
qkd_client_readable_config() {
  chmod 755 /etc/qkd-ekm
  chmod 644 /etc/qkd-ekm/env /etc/qkd-ekm/qkdsim.crt

  {
    echo '# Managed by the qkd-ekm startup script; mirrors /etc/qkd-ekm/env.'
    while IFS= read -r line; do
      case "$line" in
        '' | '#'*) continue ;;
      esac
      printf "export %s='%s'\n" "${line%%=*}" "${line#*=}"
    done < /etc/qkd-ekm/env
  } > /etc/profile.d/qkd-ekm.sh
  chmod 644 /etc/profile.d/qkd-ekm.sh
}

# Override the rotation timer interval with the Terraform variable.
qkd_rotate_interval() {
  local mins
  mins="$(qkd_meta rotation_minutes)"
  mkdir -p /etc/systemd/system/qkd-ekm-rotate.timer.d
  printf '[Timer]\nOnBootSec=%smin\nOnUnitActiveSec=%smin\n' "$mins" "$mins" \
    > /etc/systemd/system/qkd-ekm-rotate.timer.d/override.conf
  systemctl daemon-reload
}
