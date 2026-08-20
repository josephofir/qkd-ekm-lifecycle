# `terraform/` — GCP topology for the QKD/EKM reproducibility package

One root module, no sub-modules. It builds the whole paper environment: two VPCs, five
Debian 12 VMs, a Cloud EKM connection reached over VPC (Service Directory + PSC), an
`EXTERNAL_VPC` KMS key backed by the EKM VM, and a CMEK-protected Cloud Storage bucket.

## Prerequisites

- Terraform ≥ 1.5 and the `google` / `google-beta` providers ≥ 5.40 (`terraform init` fetches them).
- `gcloud`, authenticated (`gcloud auth login && gcloud auth application-default login`).
  Terraform shells out to `gcloud` once, to promote CryptoKeyVersion 1 to primary.
- `uv` (to build the wheel) and GNU `tar`.
- A billing-enabled project. `scripts/gcp_bootstrap_project.sh PROJECT_ID BILLING_ACCOUNT`
  creates one, links billing and enables the APIs.
- A region where **Cloud EKM via VPC** is available. The paper used `me-west1`; if you
  substitute another region, say so in `results/COMPARISON.md`.

No `openssl` is needed: the EKM connection's `raw_der` is derived from the Terraform-generated
PEM in pure HCL (a PEM body *is* base64-encoded DER, so stripping the armour and the newlines
is the conversion).

## Apply

```sh
scripts/gcp_bootstrap_project.sh my-project 0X0X0X-0X0X0X-0X0X0X   # once
scripts/build_dist.sh                                             # dist/qkd-ekm-lifecycle.tar.gz
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # set project_id, operator_emails
terraform -chdir=terraform init
terraform -chdir=terraform apply
```

`terraform apply` must be run with the tarball already built — the `dist` bucket object is
uploaded from `var.dist_tarball` and every VM downloads it at first boot.

Ordering Terraform handles for you, in case an apply is interrupted:

1. Reserved addresses (`ekm-internal`, `workload-internal`, `qkdsim-internal`,
   `vpn-external`, `qkdsim-external`, `client-external`). Every address a startup script
   bakes into `/etc/qkd-ekm/env` is reserved first, so no VM depends on another VM.
2. Service Directory namespace/service/endpoint → the EKM VM's reserved internal IP:8443,
   plus the `servicedirectory.viewer` / `servicedirectory.pscAuthorizedService` grants to
   `service-<project-number>@gcp-sa-ekms.iam.gserviceaccount.com`.
3. The five VMs and the firewall rules that admit Cloud EKM / the VPC to `ekm-vm:8443`, and
   the tunnel CIDR to `workload-vm` (tcp/8081 and ICMP, the latter for the S1 reachability check).
4. `null_resource.ekm_ready`: from the operator's workstation over IAP, polls `ekm-vm` until
   `/healthz` answers **and** `GET /api/state` reports `source_available: true` (up to 10
   minutes, 15 s apart). This exists because the two KMS resources below call the EKM host
   synchronously while being created — Cloud KMS opens a TLS connection to it when the
   *connection* is created (failing with `Timed out when trying to access the EKM host` if
   nothing answers) and performs a `:wrap` against `api/keys/v1` when the *version* is created
   (which returned `503 UNAVAILABLE` on our first apply, before this guard existed, because the
   QKD pool was still empty) — and neither failure is one Terraform can retry on its own.
5. `google_kms_ekm_connection` (MANUAL mode, hostname `ekm.qkd.internal`, the self-signed
   server certificate as DER), depending on `null_resource.ekm_ready`.
6. Key ring + `EXTERNAL_VPC` CryptoKey (`skip_initial_version_creation`), and the GCS
   service agent's `cryptoKeyEncrypterDecrypter` grant.
7. CryptoKeyVersion 1 with `ekm_connection_key_path = api/keys/v1`, depending on
   `null_resource.ekm_ready` and the connection above. A `null_resource` then runs
   `gcloud kms keys update --primary-version 1`.
8. The CMEK data bucket, last: GCS validates the default KMS key when the bucket is created,
   so the key needs an enabled primary version by then. Nothing upstream reads the bucket
   resource — its name is computed in `local.data_bucket_name` and that local is what the
   workload VM's env file and the `data_bucket` output use, which is what keeps this
   ordering acyclic.

If step 4 or 7 fails because the VM is still bootstrapping (the wheel install takes a couple of
minutes), re-run `terraform apply`.

Confirmed on the first live run: even after `null_resource.ekm_ready` succeeds, step 5
(`google_kms_ekm_connection`) can still fail with `Permission denied when accessing the Service
Directory` (`SD_RESOURCE_PERMISSION_DENIED`) — the `servicedirectory.viewer` /
`servicedirectory.pscAuthorizedService` grants from step 2 can take about a minute longer to
propagate than the VM takes to boot. Re-running `terraform apply` succeeds once the grant has
caught up; nothing needs recreating. See [docs/troubleshooting.md](../docs/troubleshooting.md).

If the first apply fails on `google_project_iam_member.ekms_service_directory` with
"member … does not exist", the Cloud EKM service agent has not been provisioned yet.
`null_resource.service_identities` asks for it via
`gcloud beta services identity create --service=ekms.googleapis.com`, which is best effort;
if that did not take, run `gcloud kms ekm-connections list --location <region>` once (listing
provisions the agent) and re-apply.

Once the stack is up, `scripts/redeploy.sh [role ...]` gets a code or `terraform.tfvars` change
onto the running VMs without recreating any of this: it rebuilds the wheel, applies (so updated
instance metadata goes out), then re-runs the named VMs' startup scripts and restarts their
units.

`terraform destroy` is clean; both buckets are `force_destroy`. Confirmed on a second,
from-zero `terraform destroy` → `terraform apply` cycle in the same project: it now succeeds in
one apply. Two things had to be fixed for that:

- **Cloud KMS EKM connections cannot be deleted** — like key rings and keys, `terraform destroy`
  only forgets them, it does not remove them from the project. A fixed connection name would
  therefore collide with the forgotten one on the next `apply`. `random_id.suffix` (generated
  once per `apply`, in `main.tf`) now names the connection, namespace and key ring
  `qkd-ekm-<suffix>`, so every deployment gets fresh names automatically. The forgotten
  connections and key rings from earlier cycles linger in the project at no cost — nothing to
  clean up, but `gcloud kms ekm-connections list` / `gcloud kms keyrings list` will keep
  accumulating entries across cycles.
- **Cloud EKM could not reach a recreated VPC of the same name.** After destroying and
  re-applying with a same-named VPC and Service Directory namespace, `google_kms_ekm_connection`
  failed with `Timed out when trying to access the EKM host` — for the *new* connection, and
  even for connections that predated the recreation — while TLS to `ekm-vm:8443` from inside the
  VPC worked fine. The VPC (`qkd-vpc-<suffix>` in `network.tf`) is now suffixed the same way, so
  a fresh `apply` never reuses a VPC name either.

If you pin these names yourself (for example by hardcoding a connection or VPC name instead of
using `random_id.suffix`), a `destroy` → `apply` cycle in the same project is not guaranteed to
work — use a new project instead. See
[docs/troubleshooting.md](../docs/troubleshooting.md) and
[docs/architecture.md §7](../docs/architecture.md#7-per-deployment-naming).

## Outputs

| Output | Use |
|---|---|
| `project_number` | derives the Google service-agent emails |
| `zone`, `vpn_vm_name`, `client_vm_name` | targets for `gcloud compute ssh` / IAP tunnels |
| `vpn_external_ip` | WireGuard endpoint (udp/51819) |
| `client_external_ip` | source allow-listed on the QKD simulator |
| `qkdsim_external_ip` | QKD2 endpoint the client pulls keys from |
| `workload_internal_ip` | file-upload service, reachable over the tunnel (tcp/8081) |
| `data_bucket` | CMEK bucket the upload service writes to |
| `kms_key`, `ekm_connection` | full KMS resource names for the audit-log checks |
| `sim_token`, `vpn_token`, `sim_user`, `sim_password` | sensitive; `terraform output -raw <name>` |
| `ssh_client_cmd`, `iap_tunnel_cmd` | ready-made gcloud commands |

## Operator access

Every address in `var.operator_emails` receives `roles/compute.osAdminLogin` and, at project
level, `roles/iap.tunnelResourceAccessor`, so `gcloud compute ssh --tunnel-through-iap` reaches
all five VMs. `scripts/run_experiment.sh` needs that: the preflight health checks and the EKM /
upload-server logs behind the S2 transcript live on the three VMs with no external ingress.
One per-instance binding remains: the client VM's service account on `vpn-vm`, which is how
`client-vm` opens the tunnel to the VPN control API from inside the experiment. Operators get no
per-instance bindings, because the project-level grant already covers them.

**The identity running `terraform apply` must itself be in `var.operator_emails`.**
`null_resource.ekm_ready` polls `https://localhost:8443/healthz` on `ekm-vm` through
`gcloud compute ssh --tunnel-through-iap` before the first CryptoKeyVersion is created (Cloud
KMS calls the EKM synchronously while creating it), and that needs the operator's IAP tunnel
and OS Login grants. It retries every 15 s for up to 10 minutes, then fails loudly; if it does,
re-running `terraform apply` once the VM has finished bootstrapping is still the fallback.

Watch item (Task 13, first cloud run): the client SA's `roles/compute.viewer` is granted per
instance on `vpn-vm` rather than project-wide, on the reading that
`gcloud compute start-iap-tunnel` only needs `compute.instances.get` on the target. If the
tunnel step of S1 fails with a permission error on the client VM, widen that binding back to
the project (`google_project_iam_member`) and note it in the run report.

## Prototype shortcuts (deliberate)

- The EKM's TLS certificate **and private key** are delivered through instance metadata, and
  the shared bearer tokens live in `/etc/qkd-ekm/env` (mode 0600). Good enough for a
  reproducibility package; production would use Secret Manager or an HSM.
- All five VMs get an external IP. Cloud NAT would be tidier but costs more; the
  "internal-only" VMs (`ekm-vm`, `workload-vm`) are internal-only because **no ingress
  firewall rule admits internet traffic to them**, not because they lack an address.
- Self-signed certificates for the EKM and the simulator, pinned by CA file on each client.
- On `client-vm` only, `/etc/qkd-ekm/env` and the QKD CA are mode 0644 and mirrored into
  `/etc/profile.d/qkd-ekm.sh`, so the runner's SSH user (and `sudo -E`) sees the settings.
  The other VMs keep `/etc/qkd-ekm` at 0700 with 0600 files.
- `vpn-vm` has `can_ip_forward = true` and NATs the tunnel CIDR (`var.vpn_tunnel_cidr`,
  default `10.20.0.0/24`) out of its primary interface, so tunnel traffic reaches
  `workload-vm:8081`. The VPC would drop those packets otherwise: the tunnel range is not
  one of its subnets.
