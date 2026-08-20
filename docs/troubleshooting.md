# Troubleshooting

Failures seen while bringing this stack up, and what each one actually means. Log in to a VM
with `gcloud compute ssh <vm> --zone <zone> --tunnel-through-iap`; every service writes to
`/var/log/qkd-ekm/*.log` and to its journal (`journalctl -u qkd-ekm-<role> -o cat`).

## Deployment

### `scripts/gcp_bootstrap_project.sh` fails: "Cloud billing quota exceeded"

`gcloud billing projects link` refuses once the billing account already has its maximum number
of linked projects. Either unlink or delete a project you no longer need
(`gcloud billing projects unlink <old-project>`, or `gcloud projects delete <old-project>`) or
pass a different, less-loaded billing account (`gcloud billing accounts list`) and re-run the
script. The script itself now skips the link step entirely when the target project is already
billing-enabled, so re-running it after fixing the quota (or against a project you set up by
hand) is safe either way.

### `terraform apply` fails on `google_kms_ekm_connection.ekm`: "Permission denied when accessing the Service Directory" (`SD_RESOURCE_PERMISSION_DENIED`)

Observed on the first live run. `null_resource.ekm_ready` already confirmed `ekm-vm` itself is
serving `:8443` before this resource is created, but the Service Directory grants to the Cloud
EKM service agent (`roles/servicedirectory.viewer`, `roles/servicedirectory.pscAuthorizedService`
on `service-<number>@gcp-sa-ekms.iam.gserviceaccount.com`) can take about a minute longer to
propagate than the VM takes to boot. **Re-run `terraform apply`** — nothing needs recreating;
the grant has usually finished propagating by the time you re-run it.

### `terraform apply` fails on `google_kms_crypto_key_version.v1`

`terraform/kms.tf` makes both `google_kms_ekm_connection.ekm` and this resource depend on
`null_resource.ekm_ready`, which polls `ekm-vm`'s `/healthz` and `GET /api/state` for
`source_available: true` before either is created — because Cloud KMS opens a connection to the
EKM host synchronously when the *connection* is created (failing with `Timed out when trying to
access the EKM host` if nothing answers), and performs a `:wrap` against `api/keys/v1`
synchronously when the *version* is created (our EKM answered `503 UNAVAILABLE` here on the
very first apply, before the guard existed, because its QKD pool was still empty). If you still
hit either error — for example because `null_resource.ekm_ready` itself failed after its 10-minute
retry budget — wait for the VM to finish bootstrapping and **re-run `terraform apply`**; nothing
else needs recreating. Confirm the EKM is actually up first:

```sh
gcloud compute ssh ekm-vm --zone <zone> --tunnel-through-iap \
  --command 'systemctl is-active qkd-ekm-ekm; curl -sk https://localhost:8443/healthz'
```

### `google_project_iam_member.ekms_service_directory`: "member … does not exist"

The Cloud EKM service agent (`service-<number>@gcp-sa-ekms.iam.gserviceaccount.com`) has not
been provisioned. Terraform asks for it with `gcloud beta services identity create
--service=ekms.googleapis.com`, which is best effort. Force it by listing the resource once,
then re-apply:

```sh
gcloud kms ekm-connections list --location <region> --project <project>
terraform -chdir=terraform apply
```

### The bucket fails to create: "CMEK key does not have an enabled primary version"

GCS validates `default_kms_key_name` at bucket creation, so the CryptoKey needs an enabled
**primary** version by then. Terraform orders this for you (version 1, then
`null_resource.primary_version` runs `gcloud kms keys update --primary-version 1`, then the
bucket). If you see this, the primary promotion did not run — check `gcloud kms keys describe`
and re-apply.

### `terraform apply` fails with "Timed out when trying to access the EKM host" after a `destroy` → `apply` cycle

Observed on a from-zero redeploy in the same project: `google_kms_ekm_connection` (or a later
`:wrap`) times out reaching the EKM host — even for a connection that predated the redeploy —
while TLS to `ekm-vm:8443` works fine from inside the VPC. Cause: Cloud EKM could not route to a
recreated VPC / Service Directory namespace that reused the previous deployment's name. Also
relevant: Cloud KMS EKM connections cannot be deleted at all — `terraform destroy` only forgets
them, so a fixed connection name collides with the forgotten one on the next `apply`. Fix: the
VPC (`qkd-vpc-<suffix>`), the Service Directory namespace, and the EKM connection/key ring name
(`qkd-ekm-<suffix>`) are all suffixed per deployment (`random_id.suffix`), so a fresh `apply`
never reuses a name and this class of failure should not recur. **Names are suffixed
automatically — if you pinned any of them yourself, use a new project instead of destroy/apply
in place.** The forgotten connections and key rings from earlier cycles linger in the project at
no cost. See [terraform/README.md](../terraform/README.md).

### `me-west1` refuses the EKM connection

Cloud EKM via VPC is not available in every region. If `google_kms_ekm_connection` fails with a
location error, move `region` and `zone` in `terraform.tfvars` to the nearest region that
supports it (`gcloud kms ekm-connections list --location <candidate>` succeeding is a decent
probe), destroy and re-apply — and say so in `COMPARISON.md`, because the paper used
`me-west1`.

### First boot takes minutes / apt failures in the serial console

The startup script waits up to 600 s for the dpkg lock and retries `apt-get` five times,
because GCE's unattended-upgrade job holds the lock for a minute or two after boot. Watch it
with `gcloud compute instances get-serial-port-output <vm> --zone <zone> | grep qkd-ekm`. The VM
is ready when it prints `<role>-vm ready`.

## Access

### `Permission denied` / `Error while connecting [4033: Failed to lookup instance]` on ssh

There is no manual IAM step: `terraform/iam.tf` grants every address in `var.operator_emails`
both `roles/compute.osAdminLogin` and, at project level,
`roles/iap.tunnelResourceAccessor`, which is what the runner needs to reach all five VMs.

If you see tunnel errors anyway, check in this order: your account is actually in
`operator_emails` (add it and re-apply — the grants are `for_each`ed over that list); you are
running as that account (`gcloud config get-value account`); the propagation of a fresh IAM
binding has finished (up to a minute); and the `iap-ssh` / `iap-ssh-client` firewall rules still
admit `35.235.240.0/20` to tcp/22 on both VPCs.

```sh
gcloud projects get-iam-policy <project> \
  --flatten=bindings[].members --filter=bindings.role:iap.tunnelResourceAccessor \
  --format='value(bindings.members)'
```

### `run_experiment: ssh to <vm> failed (transport, attempt N/4); retrying in 5s`

A transient IAP-websocket or network blip on the *operator's* machine, not the VM — for example
`Error while connecting [[Errno 51] Network is unreachable]`. `gcloud compute ssh`/`scp` exit
255 for this class of failure specifically (as opposed to the remote command's own exit code),
and `run_experiment.sh` retries only that code, up to `SSH_RETRIES` (default 4, 5 s apart). If
you see the step fail after all retries, the network problem outlasted ~20 s; raise
`SSH_RETRIES` or fix the connection and re-run the step.

### `VPNServer: JWT rejected: email not verified`

`vpn-vm` pins its allow-list on the identity token's `email` claim, which requires
`email_verified` too. This is reachable only when the caller is not `client-vm` itself: the
`qkd-ekm-client` CLI prefers the GCE metadata server (`.../identity?audience=qkd-ekm-vpn&format=full`),
whose tokens always carry both claims, and falls back to `gcloud auth print-identity-token` only
when the metadata server is unreachable (e.g. from a laptop). A service-account identity token
from `gcloud` omits `email`/`email_verified` entirely, so a laptop run authenticating as a
service account is rejected here by design — run the step on `client-vm` instead, or authenticate
as a user account when driving the CLI from a laptop.

### The runner hangs on the IAP tunnel step

`s1` opens `gcloud compute start-iap-tunnel vpn-vm 8080 --local-host-port=localhost:18080` **on
`client-vm`**, whose service account holds the tunnel role. If it fails, read
`/tmp/iap-tunnel.log` there. A tunnel from your laptop works too, and the CLI takes
`--control-url`:

```sh
gcloud compute start-iap-tunnel vpn-vm 8080 --local-host-port=localhost:18080 --zone <zone>
```

## EKM ↔ Cloud KMS

### `EKM: JWT rejected: audience not allowed aud=<value> email=<value>`

The audience check is on **by default**: `ekm_jwt_audiences` defaults to
`https://ekm.qkd.internal`, which is the `https://<ekm hostname>` value Cloud EKM was confirmed
to send on the first live run. This rejection means the `aud` the EKM actually saw does not
match — set `ekm_jwt_audiences` to the value this log line reports (in
`/var/log/qkd-ekm/ekm.log`) and redeploy just the EKM:

```hcl
ekm_jwt_audiences = "<the aud the log printed>"
```

```sh
scripts/redeploy.sh ekm
```

Setting `ekm_jwt_audiences = ""` disables the audience check entirely (the signature, the issuer
and the `gcp-sa-ekms` service-agent e-mail are still enforced) — an opt-out for a nonstandard
setup, not something to reach for by default.

The same line reports the `email` claim. It must equal
`service-<project-number>@gcp-sa-ekms.iam.gserviceaccount.com`; if it does not, the request came
from something other than Cloud EKM and rejection is correct.

### Key version stuck in `PENDING_GENERATION`, or KMS reports the external key unreachable

```sh
gcloud kms keys versions describe 1 --key qkd-external-key \
  --keyring <ring> --location <region> --format='value(state,externalProtectionLevelOptions)'
```

Walk the path in order: is `qkd-ekm-ekm.service` active; does `curl -sk
https://localhost:8443/healthz` answer on the VM; does the firewall rule `ekm-from-cloud-ekm`
still admit `35.199.192.0/19` to tcp/8443; does the Service Directory endpoint hold `ekm-vm`'s
current internal address; and is the certificate pinned in the EKM connection the one the VM is
actually serving (Terraform generates both from the same `tls_self_signed_cert`, so they only
diverge if the VM was rebuilt without a re-apply).

### S2's `EKM: Got Key Wrap request` line is missing, or `qkd-ekm-rotate.service` reports failure

`run_experiment.sh s2` rotates the external key (`qkd-ekm-rotate.service` on `ekm-vm`) before
uploading, precisely because Cloud Storage can serve the wrapped DEK it cached from bucket
creation or an earlier write for some minutes — without the rotation, an upload can succeed
without the EKM ever seeing a fresh `:wrap`, so the paper's Fig. 8 `EKM: Got Key Wrap request`
line never appears. Confirmed on the second (from-zero) live cycle: the first deployment's run
had the line without an explicit rotation; a later from-zero deployment's did not until one was
forced. Check `s2/rotate.out` for `Rotated external key to version v<n+1>` and `kms_versions.json`
for at least two versions.

If `systemctl start qkd-ekm-rotate.service` (or `journalctl -u qkd-ekm-rotate`) shows the unit as
**failed** but the log still printed `Rotated external key to version v<n+1>`, the rotation
itself succeeded — an older build of the `qkd-ekm-rotate` console script returned the version
label through `sys.exit(...)`, which systemd reads as a non-zero exit even though nothing went
wrong. Fixed in the current build (it prints the label and exits 0); upgrade (`scripts/redeploy.sh
ekm`) rather than treating the failed unit status as a real rotation failure.

### `PSC`/Service Directory reachability

Cloud EKM reaches the VM from `35.199.192.0/19`, not from the VPC's own ranges, and the service
agent needs both `roles/servicedirectory.viewer` and
`roles/servicedirectory.pscAuthorizedService`. If the connection tests fine but wraps time out,
that firewall rule or those two grants are the first things to check.

## Workflows

### WireGuard: `connect` succeeds but the ping fails

```sh
sudo wg show                      # on client-vm and on vpn-vm
```

A peer with `latest handshake: (none)` means the handshake never completed: check that the
`vpn-wg` firewall rule admits `udp/51819` from `0.0.0.0/0` to `vpn-vm`, that the client is
dialling `vpn_external_ip`, and that both ends hold the *same* pre-shared key — the client
fetches it from QKD2 by the id the server allocated, so a `dec_keys` failure (already consumed,
unknown id) breaks the tunnel rather than the API call.

A completed handshake but no reply from `workload-vm` is a routing problem instead:
`vpn-vm` needs `can_ip_forward`, `net.ipv4.ip_forward=1` and the MASQUERADE rule for the tunnel
CIDR (`sudo iptables -t nat -L POSTROUTING -n`), because the tunnel range is not a VPC subnet.

### `upload` returns 404 "unknown key id"

The upload server caches the file key for `file_key_ttl_s` (600 s). If more than that elapsed
between `POST /api/file_key` and `POST /api/upload`, the key is gone — rerun the upload, which
requests a fresh one.

### `capture` returns `null` counters

`available_256bit_keys` and friends come from the ETSI-014 status block, which authenticates
with the appliance bearer token rather than the monitoring JWT. The runner passes
`--etsi-token`; without it those fields are `null` while the rates still populate.

### `continuity` never reaches `BUFFERED`

The pool only calls the QKD source when it is below `EKM_POOL_TARGET`, so a full pool never
learns that the source died. The runner spends a key (a VPN `refresh`) right after injecting the
outage for exactly this reason. If you drive `/sim/source` by hand, do the same, then poll
`GET /api/state`.

### `s1.sequence` or `s2.sequence` FAIL in COMPARISON.md

`compare.py` prints the first expected event it could not match. Two common causes:

- **A log file did not make it into the window.** Check `raw/marks.json` against the timestamps
  in `raw/*.log`. The marks are taken from *your laptop's* clock in UTC while the logs carry the
  VMs' clocks; a laptop more than a second off will clip the window. Re-run
  `scripts/run_experiment.sh redact -o results/<run>` after fixing the clock — the raw logs are
  still there.
- **A service was restarted mid-scenario**, so its log file starts after the scenario did. The
  journal copies in `raw/*-journal.txt` will show the restart.

### `kms_audit.json` is empty

Best effort by design: Cloud Audit ingestion lags by up to a few minutes, and the reader needs
`roles/logging.viewer`. Re-run `gcloud logging read 'protoPayload.serviceName="cloudkms.googleapis.com"'
--limit 5 --format json` by hand later; it does not affect the comparison.
