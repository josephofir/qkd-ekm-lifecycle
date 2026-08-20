#!/usr/bin/env bash
# Drive the paper's evidence run against a stack deployed by terraform/.
#
#   scripts/run_experiment.sh [all|preflight|s1|s2|capture|continuity|model|analysis|redact|compare]
#                            [-o results/<dir>]
#
# Runs from the operator's laptop and needs gcloud, terraform, uv and jq. Every
# command that touches the deployment goes through `gcloud compute ssh
# --tunnel-through-iap`: no VM in the stack accepts traffic from the internet
# except the WireGuard endpoint, so there is nothing to curl directly from here.
#
# DRY_RUN=1 prints every command instead of running it (no cloud calls at all).
#
# SSH_RETRIES=<n> (default 4) is how many times gssh/gscp retry a dropped IAP transport
# before giving up.
#
# Single-quoted `$VAR` inside a --command string is deliberate throughout: those
# variables are expanded by the shell on the VM, from /etc/profile.d/qkd-ekm.sh
# or /etc/qkd-ekm/env, not by this script.
# shellcheck disable=SC2016
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TF_DIR="$REPO_ROOT/terraform"
DRY_RUN="${DRY_RUN:-0}"
QKD_BACKEND="${QKD_BACKEND:-sim}"     # provenance recorded in the capture file
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-120}"

# Instance names are `${role}-vm` (terraform local.vm_name); only vpn-vm and
# client-vm are published as outputs.
EKM_VM="ekm-vm"
WORKLOAD_VM="workload-vm"
QKDSIM_VM="qkdsim-vm"

# The EKM listens on its Service Directory hostname, resolved through /etc/hosts
# on every VM inside qkd-vpc.
EKM_URL="https://ekm.qkd.internal:8443"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- plumbing ---------------------------------------------------------------

log() {
  printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*" >&2
}

die() {
  printf 'run_experiment: %s\n' "$*" >&2
  exit 1
}

dry() {
  [ "$DRY_RUN" = 1 ]
}

need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" > /dev/null || die "required tool not found: $tool"
  done
}

# run <cmd...> — echo the command, then run it (unless DRY_RUN=1).
run() {
  log "+ $*"
  if dry; then return 0; fi
  "$@"
}

# run_to <file> <cmd...> — same, with stdout redirected into <file>.
run_to() {
  local out=$1
  shift
  log "+ $* > $out"
  if dry; then return 0; fi
  "$@" > "$out"
}

# grab <cmd...> — same, but stdout stays on stdout so `x=$(grab ...)` works.
# Under DRY_RUN the caller gets the literal string DRY_RUN.
grab() {
  log "+ $*"
  if dry; then echo "DRY_RUN"; return 0; fi
  "$@"
}

GCLOUD_SSH_FLAGS=()
SSH_RETRIES=${SSH_RETRIES:-4}

# gscp <src> <dst> — gcloud compute scp through IAP with the same transport retry.
gscp() {
  local attempt=1 rc
  while :; do
    rc=0
    gcloud compute scp "$1" "$2" "${GCLOUD_SSH_FLAGS[@]}" || rc=$?
    if [ "$rc" -ne 255 ] || [ "$attempt" -ge "$SSH_RETRIES" ]; then return "$rc"; fi
    echo "run_experiment: scp failed (transport, attempt $attempt/$SSH_RETRIES); retrying in 5s" >&2
    attempt=$((attempt + 1))
    sleep 5
  done
}

# gssh <vm> <remote shell command> — gcloud compute ssh through IAP, retried on the
# ssh transport failure code (255: IAP websocket / network blips from the operator's
# machine). A non-255 status is the remote command's own exit code and is returned as is.
# A retry re-runs the remote command in full: this is at-least-once, not at-most-once,
# so a command with side effects (e.g. step_s2's rotation) may run twice on a flaky
# transport. Each attempt's stdout is buffered in a temp file rather than streamed
# straight through, so a transport failure mid-attempt cannot leave its partial output
# concatenated ahead of the next attempt's in whatever the caller redirects gssh's
# stdout to (a file via run_to, or a variable via $(...) in grab).
gssh() {
  local vm=$1 attempt=1 rc tmp
  shift
  tmp="$(mktemp)"
  while :; do
    rc=0
    gcloud compute ssh "$vm" "${GCLOUD_SSH_FLAGS[@]}" --command "$1" > "$tmp" || rc=$?
    if [ "$rc" -ne 255 ]; then
      cat "$tmp"
      rm -f "$tmp"
      return "$rc"
    fi
    if [ "$attempt" -ge "$SSH_RETRIES" ]; then
      rm -f "$tmp"
      return "$rc"
    fi
    echo "run_experiment: ssh to $vm failed (transport, attempt $attempt/$SSH_RETRIES); retrying in 5s" >&2
    attempt=$((attempt + 1))
    sleep 5
  done
}

# vm_ssh <vm> <remote shell command>
vm_ssh() {
  local vm=$1
  shift
  run gssh "$vm" "$1"
}

# vm_ssh_to <file> <vm> <remote shell command>
vm_ssh_to() {
  local out=$1 vm=$2
  shift 2
  run_to "$out" gssh "$vm" "$1"
}

# vm_ssh_out <vm> <remote shell command> — stdout of the remote command.
vm_ssh_out() {
  local vm=$1
  shift
  grab gssh "$vm" "$1"
}

# --- terraform outputs ------------------------------------------------------

# Enough shape for DRY_RUN to print realistic commands without any state file.
PLACEHOLDER_OUTPUTS='{
  "zone": {"value": "me-west1-a"},
  "vpn_vm_name": {"value": "vpn-vm"},
  "client_vm_name": {"value": "client-vm"},
  "vpn_external_ip": {"value": "203.0.113.10"},
  "qkdsim_external_ip": {"value": "203.0.113.20"},
  "workload_internal_ip": {"value": "10.10.0.4"},
  "data_bucket": {"value": "qkd-ekm-data-abc123"},
  "kms_key": {"value": "projects/PROJECT/locations/me-west1/keyRings/qkd-ekm-abc123/cryptoKeys/qkd-external-key"},
  "ekm_connection": {"value": "projects/PROJECT/locations/me-west1/ekmConnections/qkd-ekm-abc123"}
}'

TF_JSON=""

tf() {
  printf '%s' "$TF_JSON" | jq -r --arg k "$1" '.[$k].value // empty'
}

load_outputs() {
  local json=""
  json="$(terraform -chdir="$TF_DIR" output -json 2> /dev/null || true)"
  if [ -z "$json" ] || [ "$json" = "{}" ]; then
    if dry; then
      log "no terraform state; using placeholder outputs"
      json="$PLACEHOLDER_OUTPUTS"
    else
      die "no terraform outputs — run \`terraform -chdir=terraform apply\` first"
    fi
  fi
  TF_JSON="$json"

  ZONE="$(tf zone)"
  VPN_VM="$(tf vpn_vm_name)"
  CLIENT_VM="$(tf client_vm_name)"
  BUCKET="$(tf data_bucket)"
  KMS_KEY="$(tf kms_key)"
  EKM_CONNECTION="$(tf ekm_connection)"

  # The module publishes no project/region output; the CryptoKey resource name
  # carries both, plus the key ring and key names the KMS commands below need:
  # projects/P/locations/L/keyRings/R/cryptoKeys/K
  PROJECT="$(printf '%s' "$KMS_KEY" | cut -d/ -f2)"
  REGION="$(printf '%s' "$KMS_KEY" | cut -d/ -f4)"
  KEYRING="$(printf '%s' "$KMS_KEY" | cut -d/ -f6)"
  KEYNAME="$(printf '%s' "$KMS_KEY" | cut -d/ -f8)"

  GCLOUD_SSH_FLAGS=(
    --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap --quiet
  )
}

# --- scenario time marks ----------------------------------------------------
#
# Every service logs `YYYY-MM-DD HH:MM:SS.mmm <Component>: ...` in the VM's local
# time, and GCE VMs run on UTC. The runner records a UTC window per scenario and
# `redact` filters the raw logs to it, so one results directory can hold several
# runs without their transcripts bleeding into each other.

MARKS=""

marks_file() {
  echo "$RESULTS/raw/marks.json"
}

mark() { # mark <name> <start|end>
  local now
  # .000 for a start, .999 for an end: half a second of slack at each edge,
  # without needing a portable "date minus n seconds".
  if [ "$2" = start ]; then
    now="$(date -u +'%Y-%m-%d %H:%M:%S').000"
  else
    now="$(date -u +'%Y-%m-%d %H:%M:%S').999"
  fi
  log "mark $1_$2 = $now"
  if dry; then return 0; fi
  MARKS="$(marks_file)"
  [ -f "$MARKS" ] || echo '{}' > "$MARKS"
  jq --arg k "$1_$2" --arg v "$now" '.[$k] = $v' "$MARKS" > "$MARKS.tmp"
  mv "$MARKS.tmp" "$MARKS"
}

read_mark() { # read_mark <name>_<start|end> <fallback>
  if [ ! -f "$(marks_file)" ]; then
    echo "$2"
    return 0
  fi
  jq -r --arg k "$1" --arg d "$2" '.[$k] // $d' "$(marks_file)"
}

# --- remote helpers ---------------------------------------------------------
#
# The shared EKM bearer token is read out of /etc/qkd-ekm/env on vpn-vm rather
# than passed from here, so it never appears in this process's argv (or in the
# VM's process list).
EKM_TOKEN='T=$(sudo sed -n "s/^VPN_TOKEN=//p" /etc/qkd-ekm/env)'

ekm_state() {
  vm_ssh_out "$VPN_VM" "$EKM_TOKEN
curl -sk --max-time 10 -H \"Authorization: Bearer \$T\" $EKM_URL/api/state"
}

ekm_authority() { # ekm_authority true|false
  vm_ssh_out "$VPN_VM" "$EKM_TOKEN
curl -sk --max-time 10 -X POST -H 'Content-Type: application/json' \
  -H \"Authorization: Bearer \$T\" -d '{\"continuity_authority\":$1}' $EKM_URL/api/authority"
}

ekm_ack() {
  vm_ssh_out "$VPN_VM" "$EKM_TOKEN
curl -sk --max-time 10 -X POST -H \"Authorization: Bearer \$T\" $EKM_URL/api/recovery/ack"
}

# Allocate keys until the pool refuses: with the source down this is what takes
# the EKM from BUFFERED to BINDING_HOLDOVER (paper Table 4, row 3).
ekm_drain() {
  vm_ssh_out "$VPN_VM" "$EKM_TOKEN
n=0
for _ in \$(seq 1 200); do
  code=\$(curl -sk -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer \$T\" $EKM_URL/api/QKD2/new)
  if [ \"\$code\" != 200 ]; then break; fi
  n=\$((n + 1))
done
echo \$n"
}

sim_source() { # sim_source true|false — fault injection on the QKD simulator
  vm_ssh_out "$CLIENT_VM" ". /etc/profile.d/qkd-ekm.sh
curl -sk --max-time 10 -X POST -H 'Content-Type: application/json' \
  -H \"Authorization: Bearer \$SIM_TOKEN\" -d '{\"available\":$1}' \$QKD2_URL/sim/source"
}

state_mode() {
  printf '%s' "$1" | jq -r '.mode // "unknown"' 2> /dev/null || echo unknown
}

# poll_state <mode> — the EKM's /api/state once it reports <mode>; fails loudly.
poll_state() {
  local want=$1 body="" deadline=$((SECONDS + POLL_TIMEOUT_S))
  if dry; then
    printf '{"mode":"%s"}\n' "$want"
    return 0
  fi
  while [ "$SECONDS" -lt "$deadline" ]; do
    # A dropped SSH session inside the budget is worth another try, not a failure.
    body="$(ekm_state)" || body=""
    if [ "$(state_mode "$body")" = "$want" ]; then
      printf '%s\n' "$body"
      return 0
    fi
    sleep 5
  done
  die "EKM never reported mode $want within ${POLL_TIMEOUT_S}s (last: ${body:-none})"
}

# health <vm> <label> <remote curl command>
health() {
  local vm=$1 label=$2 body
  body="$(vm_ssh_out "$vm" "$3")"
  if ! dry; then
    printf '%s\t%s\t%s\n' "$label" "$vm" "$body" >> "$RESULTS/preflight.txt"
  fi
  case "$body" in
    *'"ok"'* | DRY_RUN) log "$label: healthy" ;;
    *) die "$label on $vm is not healthy: ${body:-no response}" ;;
  esac
}

# collect <vm> <remote log path> <local name> — one service log, verbatim.
collect() {
  vm_ssh_to "$RESULTS/raw/$3" "$1" "sudo cat $2 2>/dev/null || true"
}

# --- steps ------------------------------------------------------------------

step_preflight() {
  need gcloud terraform uv jq
  log "project=$PROJECT region=$REGION zone=$ZONE bucket=$BUCKET"
  if ! dry; then : > "$RESULTS/preflight.txt"; fi

  # Health checks run on the VMs: the simulator only admits client-vm's address,
  # and ekm-vm/workload-vm admit no internet traffic at all.
  health "$QKDSIM_VM" qkdsim 'curl -sk --max-time 10 https://localhost:8200/healthz'
  health "$VPN_VM" vpn 'curl -s --max-time 10 http://localhost:8080/healthz'
  health "$EKM_VM" ekm 'curl -sk --max-time 10 https://localhost:8443/healthz'
  health "$WORKLOAD_VM" workload 'curl -s --max-time 10 http://localhost:8081/healthz'

  if dry; then return 0; fi
  jq -n \
    --arg project "$PROJECT" \
    --arg region "$REGION" \
    --arg zone "$ZONE" \
    --arg bucket "$BUCKET" \
    --arg kms_key "$KMS_KEY" \
    --arg ekm_connection "$EKM_CONNECTION" \
    --arg qkd_backend "$QKD_BACKEND" \
    --arg started_at "$STARTED_AT" \
    --arg commit "$(git rev-parse HEAD 2> /dev/null || echo unknown)" \
    --arg dirty "$(git status --porcelain 2> /dev/null | wc -l | tr -d ' ')" \
    --arg gcloud "$(gcloud version 2> /dev/null | head -1)" \
    --arg terraform "$(terraform version -json 2> /dev/null | jq -r .terraform_version)" \
    --arg uv "$(uv --version 2> /dev/null)" \
    '{project: $project, region: $region, zone: $zone, bucket: $bucket,
      kms_key: $kms_key, ekm_connection: $ekm_connection, qkd_backend: $qkd_backend,
      started_at: $started_at, commit: $commit, dirty_files: ($dirty | tonumber),
      tools: {gcloud: $gcloud, terraform: $terraform, uv: $uv}}' \
    > "$RESULTS/env.json"
  log "wrote $RESULTS/env.json"
}

# S1 — QKD-supported VPN access workflow (paper §6.1, supplementary §3.1).
step_s1() {
  mark s1 start

  # The control API is reachable only through IAP. client-vm's service account
  # holds the tunnel role, so the tunnel is opened there and the CLI's default
  # control URL (http://localhost:18080) already points at it.
  vm_ssh "$CLIENT_VM" '. /etc/profile.d/qkd-ekm.sh
if ss -ltn | grep -q ":18080 "; then echo "IAP tunnel already listening"; exit 0; fi
nohup gcloud compute start-iap-tunnel "$VPN_VM_NAME" 8080 \
  --local-host-port=localhost:18080 --zone "$ZONE" > /tmp/iap-tunnel.log 2>&1 < /dev/null &
for _ in $(seq 1 30); do
  if ss -ltn | grep -q ":18080 "; then echo "IAP tunnel up"; exit 0; fi
  sleep 1
done
echo "IAP tunnel did not open :18080" >&2
tail -20 /tmp/iap-tunnel.log >&2
exit 1'

  # sudo -i, not sudo -E: wg-quick needs root, and a root login shell is the one
  # way to get /etc/profile.d/qkd-ekm.sh into the CLI's environment that does not
  # depend on the sudoers `setenv` privilege.
  vm_ssh_to "$RESULTS/raw/client-connect.out" "$CLIENT_VM" \
    'sudo -i qkd-ekm-client connect'

  # set -e, so a dead data plane fails the step instead of leaving a quiet
  # transcript: wg has to show the peer, workload-vm has to answer ICMP (the
  # workload-icmp firewall rule) and :8081 has to serve /healthz through the tunnel.
  # The exit code is held rather than propagated: a failure here is exactly when
  # the service logs are worth having, so collect_logs runs first and the step
  # dies afterwards.
  local rc=0 failed="tunnel checks"
  vm_ssh_to "$RESULTS/raw/s1-tunnel-checks.txt" "$CLIENT_VM" '. /etc/profile.d/qkd-ekm.sh
set -ex
sudo wg show
ping -c1 -W3 "$WORKLOAD_IP"
curl -fsS --max-time 10 "http://$WORKLOAD_IP:8081/healthz"' || rc=$?

  if [ "$rc" = 0 ]; then
    # Coordinated re-key: the CLI waits for the server's announced effective_time
    # (VPN_ACTIVATION_DELAY_S, 30 s by default) before installing the new PSK.
    failed="refresh"
    vm_ssh_to "$RESULTS/raw/client-refresh.out" "$CLIENT_VM" \
      'sudo -i qkd-ekm-client refresh' || rc=$?
  fi

  mark s1 end
  collect_logs
  [ "$rc" = 0 ] || die "S1 $failed failed (logs collected in $RESULTS/raw)"
}

# S2 — managed storage and external key workflow (supplementary §3.2).
step_s2() {
  mark s2 start

  # Rotate the external key first (the paper's 15-minute cadence, triggered explicitly
  # here so the run does not depend on the timer): Cloud KMS gets a new primary version
  # whose EKM key path the EKM binds to a fresh QKD unit on first use. Cloud Storage
  # caches the wrapped DEK of the previous version for a few minutes, so without the
  # rotation the upload may not produce the "Got Key Wrap request" the paper's Fig. 8 shows.
  # `;`, not `&&`, between the two remote commands: a failed rotation must still leave
  # its journal tail in rotate.out for the grep check below (and for post-mortem).
  log "rotating the external key before the upload"
  vm_ssh_to "$RESULTS/s2/rotate.out" "$EKM_VM" 'sudo systemctl start qkd-ekm-rotate.service; sudo journalctl -u qkd-ekm-rotate -o cat --no-pager | tail -3'
  if ! dry; then
    grep -Eq "^v[0-9]+$|Rotated external key" "$RESULTS/s2/rotate.out" || die "external key rotation did not report success (see $RESULTS/s2/rotate.out)"
  fi

  vm_ssh_to "$RESULTS/s2/upload.out" "$CLIENT_VM" '. /etc/profile.d/qkd-ekm.sh
echo "sensitive data $(date -u)" > /tmp/sensitive.txt
sudo -i qkd-ekm-client upload /tmp/sensitive.txt --upload-url "http://$WORKLOAD_IP:8081"'

  mark s2 end
  collect_logs

  local uri="gs://$BUCKET/<uuid>_sensitive.txt"
  if ! dry; then
    uri="$(grep -o 'gs://[^[:space:]]*' "$RESULTS/s2/upload.out" | tail -1)"
    [ -n "$uri" ] || die "no object URI in $RESULTS/s2/upload.out"
  fi
  log "uploaded object: $uri"

  run_to "$RESULTS/s2/bucket_listing.txt" gcloud storage ls "gs://$BUCKET/" --project "$PROJECT"
  run gcloud storage cp "$uri" "$RESULTS/s2/" --project "$PROJECT"
  vm_ssh_to "$RESULTS/s2/sensitive.txt" "$CLIENT_VM" 'cat /tmp/sensitive.txt'

  # The upload server strips the client's AES-GCM layer and hands Cloud Storage
  # the plaintext, which the bucket's CMEK then wraps through KMS -> EKM. So the
  # object read back must equal what the client wrote.
  run diff "$RESULTS/s2/sensitive.txt" "$RESULTS/s2/$(basename "$uri")"

  run_to "$RESULTS/s2/kms_versions.json" gcloud kms keys versions list \
    --key "$KEYNAME" --keyring "$KEYRING" --location "$REGION" --project "$PROJECT" \
    --format json

  # Best effort: audit ingestion lags the request, and the reader may lack
  # roles/logging.viewer.
  log "+ gcloud logging read 'protoPayload.serviceName=\"cloudkms.googleapis.com\"' > $RESULTS/s2/kms_audit.json"
  if ! dry; then
    gcloud logging read 'protoPayload.serviceName="cloudkms.googleapis.com"' \
      --limit 5 --format json --project "$PROJECT" > "$RESULTS/s2/kms_audit.json" \
      || log "kms audit read failed (best effort); see docs/troubleshooting.md"
  fi
}

# Table 4 dashboard values, read from the appliance (or the simulator standing in
# for it). The script runs on client-vm: its address is the only one the
# simulator's firewall admits, and the appliance CA is already installed there.
step_capture() {
  if [ "$QKD_BACKEND" != sim ]; then
    # A real appliance serves the monitoring API on another port, with another
    # credential than the key-delivery endpoint in /etc/profile.d/qkd-ekm.sh.
    log "capture: backend is $QKD_BACKEND — run capture_qkd.py by hand, see docs/heqa-setup.md"
    return 0
  fi
  run gscp scripts/capture_qkd.py "$CLIENT_VM:/tmp/capture_qkd.py"
  vm_ssh "$CLIENT_VM" ". /etc/profile.d/qkd-ekm.sh
/opt/qkd-ekm/venv/bin/python /tmp/capture_qkd.py --backend $QKD_BACKEND \
  --url \"\$QKD2_URL\" --ca \"\$QKD_CA_FILE\" \
  --user \"\$SIM_USER\" --password \"\$SIM_PASSWORD\" --etsi-token \"\$SIM_TOKEN\" \
  -o /tmp/qkd_capture.json"
  vm_ssh_to "$RESULTS/qkd_capture.json" "$CLIENT_VM" 'cat /tmp/qkd_capture.json'
}

# The continuity scenario walks the lifecycle modes of paper Table 4 / Figure 3:
# READY -> BUFFERED -> BINDING_HOLDOVER -> SUSPENDED -> RECOVERY -> READY.
step_continuity() {
  local ready buffered holdover suspended restored recovery final drained refresh_rc=0

  # Baseline. A freshly deployed EKM can legitimately sit in RECOVERY: if the QKD
  # simulator finished booting after the EKM's first pull attempt, the source "returned"
  # and the lifecycle latched recovery-pending (paper Fig. 3, T7-T10). Acknowledging it
  # is the operator's reconciliation step (T11) and yields the READY baseline.
  ekm_ack > /dev/null
  ready="$(poll_state READY)"
  log "initial mode: $(state_mode "$ready")"

  log "injecting: QKD source unavailable"
  sim_source false > /dev/null

  # The pool only calls the source when it is below target, so nothing notices a
  # dead source until a key is spent. This refresh is also the paper's
  # observation that buffered inventory keeps serving allocations.
  vm_ssh_to "$RESULTS/raw/client-refresh-buffered.out" "$CLIENT_VM" \
    'sudo -i qkd-ekm-client refresh' || refresh_rc=$?
  [ "$refresh_rc" = 0 ] || die "refresh failed while the source was down (expected BUFFERED continuity)"
  buffered="$(poll_state BUFFERED)"

  drained="$(ekm_drain)"
  log "drained $drained keys from the pool"
  holdover="$(poll_state BINDING_HOLDOVER)"

  log "withdrawing continuity authority"
  ekm_authority false > /dev/null
  suspended="$(poll_state SUSPENDED)"
  ekm_authority true > /dev/null

  log "restoring: QKD source available"
  sim_source true > /dev/null
  # A one-shot read, unlike the poll_state calls around it: an SSH hiccup here
  # must not feed `jq --argjson` an empty string and abort the whole scenario.
  restored="$(ekm_state)"
  [ -n "$restored" ] || restored='{}'
  recovery="$(poll_state RECOVERY)"
  ekm_ack > /dev/null
  final="$(poll_state READY)"

  if dry; then return 0; fi
  jq -n \
    --argjson ready "$ready" \
    --argjson buffered "$buffered" \
    --argjson holdover "$holdover" \
    --argjson suspended "$suspended" \
    --argjson restored "$restored" \
    --argjson recovery "$recovery" \
    --argjson final "$final" \
    --arg drained "$drained" \
    '{sequence: ["READY","BUFFERED","BINDING_HOLDOVER","SUSPENDED","RECOVERY","READY"],
      keys_drained: ($drained | tonumber),
      observations: {ready: $ready, source_down_buffered: $buffered,
                     pool_empty_binding_holdover: $holdover,
                     authority_withdrawn_suspended: $suspended,
                     source_restored: $restored, recovery_pending: $recovery,
                     after_recovery_ack: $final}}' \
    > "$RESULTS/continuity.json"
  log "wrote $RESULTS/continuity.json"
}

step_model() {
  run uv run qkd-ekm-model all -o "$RESULTS/model"
}

step_analysis() {
  run uv run qkd-ekm-analysis all \
    --capture "$RESULTS/qkd_capture.json" \
    --paper-capture expected/qkd_capture_paper.json \
    -o "$RESULTS/analysis"
}

# Pull every service log the scenarios touch. Cheap and idempotent, so both S1
# and S2 call it: the transcripts are cut out of these files by time window.
collect_logs() {
  collect "$CLIENT_VM" /var/log/qkd-ekm/client.log client.log
  collect "$VPN_VM" /var/log/qkd-ekm/vpn.log vpn.log
  collect "$EKM_VM" /var/log/qkd-ekm/ekm.log ekm.log
  collect "$WORKLOAD_VM" /var/log/qkd-ekm/upload.log upload.log
  collect "$QKDSIM_VM" /var/log/qkd-ekm/qkdsim.log qkdsim.log
  # Same lines as the files above when the services are healthy; kept because it
  # is the only record left if a unit died before its first log write.
  vm_ssh_to "$RESULTS/raw/vpn-journal.txt" "$VPN_VM" \
    'sudo journalctl -u qkd-ekm-vpn -o cat --no-pager | tail -500'
  vm_ssh_to "$RESULTS/raw/ekm-journal.txt" "$EKM_VM" \
    'sudo journalctl -u qkd-ekm-ekm -o cat --no-pager | tail -500'
}

step_redact() {
  local scenario start end window
  for scenario in s1 s2; do
    start="$(read_mark "${scenario}_start" "0000-01-01 00:00:00.000")"
    end="$(read_mark "${scenario}_end" "9999-12-31 23:59:59.999")"
    window="$RESULTS/raw/window-$scenario"
    log "$scenario window: $start .. $end"
    if ! dry; then
      mkdir -p "$window"
      for f in "$RESULTS"/raw/*.log; do
        [ -e "$f" ] || die "no raw logs in $RESULTS/raw — run s1/s2 first"
        awk -v a="$start" -v b="$end" \
          'substr($0, 1, 23) >= a && substr($0, 1, 23) <= b' "$f" \
          > "$window/$(basename "$f")"
      done
    fi
    run uv run python scripts/redact.py "$window"/*.log \
      -o "$RESULTS/transcripts/$scenario.txt" --check
  done
  run uv run python scripts/redact.py "$RESULTS"/raw/*.log \
    -o "$RESULTS/transcripts/all.txt" --check
}

step_compare() {
  local rc=0
  run uv run python scripts/compare.py "$RESULTS" expected \
    -o "$RESULTS/COMPARISON.md" || rc=$?
  if ! dry; then cat "$RESULTS/COMPARISON.md"; fi
  return "$rc"
}

step_all() {
  step_preflight
  step_s1
  step_s2
  step_capture
  step_continuity
  step_model
  step_analysis
  step_redact
  step_compare
}

# --- main -------------------------------------------------------------------

STEP=""
RESULTS=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o | --out)
      [ "$#" -ge 2 ] || die "$1 needs a directory"
      RESULTS="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*) die "unknown option: $1" ;;
    *)
      [ -z "$STEP" ] || die "only one step at a time (got $STEP and $1)"
      STEP="$1"
      shift
      ;;
  esac
done
STEP="${STEP:-all}"

case "$STEP" in
  all | preflight | s1 | s2 | capture | continuity | model | analysis | redact | compare) ;;
  *)
    usage >&2
    die "unknown step: $STEP"
    ;;
esac

if [ -z "$RESULTS" ]; then
  # A single step joins the most recent run; `all` always starts a new one.
  if [ "$STEP" != all ] && [ -d results/latest ]; then
    RESULTS="results/latest"
  else
    RESULTS="results/$(date -u +%Y%m%d-%H%M%S)"
  fi
fi
mkdir -p "$RESULTS/raw" "$RESULTS/s2" "$RESULTS/transcripts"
case "$RESULTS" in
  results/latest) ;;
  results/*) ln -sfn "${RESULTS#results/}" results/latest ;;
esac
# The results directory itself is the one thing a dry run creates; nothing is
# written into it, and no cloud call is made.

need jq
case "$STEP" in
  # model/analysis/redact/compare need nothing but the repo, so they also work
  # on a laptop that has never run terraform.
  model | analysis | redact | compare) ;;
  *) load_outputs ;;
esac
log "step=$STEP results=$RESULTS${DRY_RUN:+ dry_run=$DRY_RUN}"
"step_$STEP"
log "done: $RESULTS"
