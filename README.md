# qkd-ekm-lifecycle

Reproducibility package for **“A Resilient External Key Lifecycle Model for QKD-Supported
Cloud Key Management in Critical Infrastructure”** — Ofir Joseph, Itzhak Aviv, Eran Dahan,
Irit Hadar (DOI: `10.xxxx/xxxxxx`, to be assigned).

The paper places quantum-distributed key material at the external key-encryption boundary of
a managed cloud: Cloud KMS keeps generating provider data-encryption keys, while every
key-encryption operation is routed to a customer-run **External Key Manager (EKM)** that binds
each external key version to one QKD unit delivered over ETSI GS QKD 014. Around that boundary
the prototype runs two service paths — a QKD-keyed WireGuard VPN and a CMEK Cloud Storage
upload — and a six-mode lifecycle that says what each path may still do when the QKD source,
the EKM, or continuity authority goes away. This package rebuilds that prototype from scratch
on Google Cloud, replays both workflows, captures the QKD operating point, re-runs the
exhaustive lifecycle enumeration and the capacity analytics, and prints a pass/fail table
against the published numbers.

Clone it, `terraform apply`, run one script, read `results/<run>/COMPARISON.md`.

## What this package reproduces

| Evidence | Paper | Produced by | Reproduction |
|---|---|---|---|
| S1 — QKD-supported VPN access workflow | §6.1, supp. §3.1, Figs 5–6 | `run_experiment.sh s1` | functional: the redacted transcript contains the published event sequence |
| S2 — managed storage / external-key workflow | §6.2, supp. §3.2, Figs 7–10 | `run_experiment.sh s2` | functional: external-key rotation, then transcript + object listing, download and byte-for-byte inspection |
| QKD operating point (Table 4 / Table 7) | §7.1, supp. §4 | `run_experiment.sh capture` | captured, **not** compared for equality — the simulator is not the appliance |
| Continuity modes (Table 4, Fig. 3, supp. §8) | §7.3 | `run_experiment.sh continuity` | observed: READY → BUFFERED → BINDING_HOLDOVER → SUSPENDED → RECOVERY → READY |
| Lifecycle enumeration | §7.5, Table 5 | `run_experiment.sh model` | exact: **45,824** states, **307,680** transitions, depth **16**; 9 invariants, 4 guards; 9 + 4 seeded detections |
| Capacity analytics (Table 9, Figs 4, 5, 12, 13) | §7.4, §7.6 | `run_experiment.sh analysis` | exact: every Table 9 row, and the ceiling of **59,681.25** equivalent lifecycle pairs |

The model counts are reproduced exactly, but only under one reading of §5.3 that the paper does
not state outright; the two-round calibration that establishes it is written up in
[docs/model-calibration.md](docs/model-calibration.md).

This package was verified 2026-08-19 on GCP `me-west1`, 43/43 checks, single-apply from-zero
deployment (a full `terraform destroy` → `terraform apply` cycle in the same project, then one
clean apply with no re-run needed), including the continuity walk READY → BUFFERED →
BINDING_HOLDOVER → SUSPENDED → RECOVERY → READY; see
[docs/sample-results/README.md](docs/sample-results/README.md).

## Architecture

```
  operator laptop                      ┌──────────────── GCP project ─────────────────────────┐
  gcloud + terraform ──IAP tunnel──────┼─► ssh :22 on every VM                                │
                                       │                                                      │
                                       │  client-vpc 10.30.0.0/24                             │
                                       │  ┌──────────────┐                                    │
                                       │  │  client-vm   │  qkd-ekm-client (CLI, root)        │
                                       │  │  ext IP      │  IAP tunnel :18080 ─► vpn-vm :8080 │
                                       │  └──┬────────┬──┘                                    │
                                       │     │        │ TLS :8200 (dec_keys, "QKD2")          │
                       WireGuard udp/51819   │        └───────────────────────┐               │
                                       │     ▼                                ▼               │
                                       │  qkd-vpc-<suffix> 10.10.0.0/24 ┌───────────┐         │
                                       │  ┌──────────────┐              │ qkdsim-vm │ "QKD1"  │
                                       │  │   vpn-vm     │◄─ ETSI-014 ──┤  :8200    │ "QKD2"  │
                                       │  │ ext IP, wg0  │   enc_keys   └───────────┘         │
                                       │  │ ctrl :8080   │                    ▲               │
                                       │  └──────┬───────┘                    │               │
                                       │         │ GET /api/QKD2/new          │ pool refill   │
                                       │         ▼  (bearer VPN_TOKEN)        │               │
                                       │  ┌──────────────┐                    │               │
   Cloud KMS ──Service Directory/PSC───┼─►│    ekm-vm    ├────────────────────┘               │
   EXTERNAL_VPC key                    │  │ :8443 TLS    │  wrap/unwrap, bindings (SQLite)    │
        ▲                              │  └──────────────┘  Google OIDC JWT from KMS          │
        │ CMEK                         │         ▲                                            │
        │                              │         │ GET /api/QKD2/new?purpose=file             │
  ┌─────┴───────┐   tunnel only  ┌─────┴────────┐│                                            │
  │ GCS bucket  │◄───────────────┤ workload-vm  ├┘                                            │
  │ (CMEK)      │   :8081 upload │ internal only│                                             │
  └─────────────┘                └──────────────┘                                             │
                                       └──────────────────────────────────────────────────────┘
```

Details, including the exact S1/S2 sequences and the lifecycle-mode mapping, are in
[docs/architecture.md](docs/architecture.md).

## Prerequisites

- **gcloud** (recent enough for `gcloud storage`), authenticated:
  `gcloud auth login && gcloud auth application-default login`.
- **Terraform ≥ 1.5** (the `google` / `google-beta` providers are fetched by `terraform init`).
- **[uv](https://docs.astral.sh/uv/)** — builds the wheel and runs everything Python.
- **jq** — the runner parses `terraform output -json` with it.
- A **GCP billing account** (`gcloud billing accounts list`).
- A region where **Cloud EKM via VPC** is available. The paper used `me-west1`; see
  [docs/troubleshooting.md](docs/troubleshooting.md) if yours is not on the list.

No `openssl`, no Docker, no local WireGuard.

**Cost.** Five `e2-small` VMs, three reserved external addresses, one key ring and two buckets:
roughly **US$3–4 per day** while the stack is up, and **$0 after `terraform destroy`** (both
buckets are `force_destroy`). Nothing here is free-tier eligible.

## Quick start

```sh
# 0. one-time: project, billing, APIs
scripts/gcp_bootstrap_project.sh my-qkd-project 0X0X0X-0X0X0X-0X0X0X

# 1. build the bootstrap tarball the VMs install at first boot
make dist

# 2. configure: set project_id and operator_emails (your Google account)
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
$EDITOR terraform/terraform.tfvars

# 3. deploy (~5 min wall-clock, single shot on a clean project: ~2 min VM bootstrap, then
#    Service Directory + KMS wire up the EKM connection and create key version 1)
terraform -chdir=terraform init
terraform -chdir=terraform apply

# 4. if the apply raced VM bootstrap or IAM propagation, re-run it — see the note below
terraform -chdir=terraform apply

# 5. run the experiment (~8-9 min, mostly the coordinated 30 s VPN re-key and the
#    continuity polling)
scripts/run_experiment.sh all        # or: make experiment

# 6. read the verdict
cat results/latest/COMPARISON.md

# 7. tear everything down
terraform -chdir=terraform destroy
```

**Step 4, the re-apply.** Two ordering effects can stop the first `apply` partway through, and
re-running it is the fix for both — nothing has to be recreated. The Service Directory IAM
grant that lets the Cloud EKM service agent reach `ekm-vm` can take about a minute to
propagate, so `google_kms_ekm_connection` can fail with `Permission denied when accessing the
Service Directory` (`SD_RESOURCE_PERMISSION_DENIED`) even though `null_resource.ekm_ready`
confirmed the VM itself was serving. Less commonly, the VM is still installing the wheel when
`null_resource.ekm_ready`'s own polling starts. `terraform/README.md` documents the full
ordering.

**Access.** Every address in `operator_emails` gets `roles/compute.osAdminLogin` and
`roles/iap.tunnelResourceAccessor`, so `gcloud compute ssh --tunnel-through-iap` works on all
five VMs — the runner needs shells on `ekm-vm`, `workload-vm` and `qkdsim-vm` for the health
checks and for the EKM / upload logs the S2 transcript is built from. There is no manual IAM
step; if you add an operator later, add them to `terraform.tfvars` and re-apply.

**Running steps individually / from a laptop.** `scripts/run_experiment.sh` always drives
`qkd-ekm-client` over SSH on `client-vm`, where it reads the GCE metadata server first — that
token carries `email`/`email_verified`, which `vpn-vm` checks against its allow-list. If you run
`qkd-ekm-client` by hand from your own machine instead (for example against a tunnel opened with
`--control-url`), the CLI falls back to `gcloud auth print-identity-token`, which for a service
account omits those claims and gets a 403 from the VPN server — see
[docs/troubleshooting.md](docs/troubleshooting.md).

## What gets deployed

| VM | Network | Listens on | Who may reach it | Role |
|---|---|---|---|---|
| `ekm-vm` | qkd-vpc-<suffix>, internal only | `:8443` TLS (self-signed) | Cloud EKM (`35.199.192.0/19`) and the VPC | wrap/unwrap for KMS (Google OIDC JWT), `GET /api/{peer}/new` for the VPN and upload services (shared bearer), QKD key pool + TTL, SQLite bindings, `GET /api/state` |
| `vpn-vm` | qkd-vpc-<suffix>, external IP | `udp/51819` WireGuard, `:8080` control | world (WG), IAP only (`:8080`) | issues QKD-derived PSK *identifiers*, coordinates refresh, routes the tunnel to `workload-vm` |
| `workload-vm` | qkd-vpc-<suffix>, internal only | `:8081` | the WireGuard tunnel CIDR and the VPC | `FileUploadServer`: unwraps the client layer, writes to the CMEK bucket |
| `qkdsim-vm` | qkd-vpc-<suffix>, external IP | `:8200` TLS | the VPC, plus `client-vm`'s address only | ETSI-014 simulator serving both SAEs (`QKD1`, `QKD2`) plus a HEQA-shaped monitoring API and `POST /sim/source` fault injection |
| `client-vm` | client-vpc, external IP | — | IAP (ssh) | stands in for the external operator laptop: runs `qkd-ekm-client` |

Plus: a Service Directory namespace/endpoint and a `google_kms_ekm_connection` (manual mode) so
Cloud KMS reaches `ekm-vm` over the VPC with no public DNS; an `EXTERNAL_VPC` CryptoKey whose
versions map to `api/keys/v<n>` on the EKM; a CMEK Cloud Storage bucket; and a 15-minute
rotation timer on `ekm-vm`. See [terraform/README.md](terraform/README.md).

With `qkd_backend = "heqa"` the simulator VM is left unused and the EKM and client talk to a
real Sceptre pair — [docs/heqa-setup.md](docs/heqa-setup.md).

## Running the experiment

```
scripts/run_experiment.sh [all|preflight|s1|s2|capture|continuity|model|analysis|redact|compare]
                          [-o results/<dir>]
```

Every step is idempotent and can be run on its own. Without `-o`, a single step joins the most
recent run (`results/latest`) while `all` starts a new timestamped directory.

| Step | What it does |
|---|---|
| `preflight` | checks the toolchain, curls `/healthz` on all four services over SSH, writes `env.json` |
| `s1` | opens the IAP tunnel on `client-vm`, `connect`, verifies `wg show` + ping + workload health *through* the tunnel, then a coordinated `refresh`; collects logs |
| `s2` | rotates the external key (`qkd-ekm-rotate.service` on `ekm-vm`, so the wrap the upload triggers binds a fresh QKD unit instead of hitting Cloud Storage's cached wrapped DEK), writes `sensitive.txt`, `upload`s it through the tunnel, lists and downloads the object, diffs it against the plaintext, lists key versions, reads the KMS audit log |
| `capture` | runs `capture_qkd.py` on `client-vm` against the QKD endpoint → `qkd_capture.json` |
| `continuity` | injects a source outage and walks the lifecycle modes, restoring READY at the end → `continuity.json` |
| `model` | `qkd-ekm-model all` → `model/{states,transitions,seeded}.csv`, `report.json` |
| `analysis` | `qkd-ekm-analysis all` → `analysis/table9.csv`, `scalars.json`, four figures |
| `redact` | cuts each scenario's time window out of the raw logs and redacts it (`--check` on) |
| `compare` | `compare.py results/<run> expected` → `COMPARISON.md`, and exits non-zero on any FAIL |

`DRY_RUN=1 scripts/run_experiment.sh all` prints every command it would run and makes no cloud
calls (it creates the empty results directory and nothing else) — the quickest way to see what
the runner does to your project before you let it. `SSH_RETRIES=<n>` (default 4) controls how
many times a dropped IAP transport is retried before `gssh`/`gscp` give up.

`model`, `analysis`, `redact` and `compare` need no cloud at all, so a reviewer can re-derive
every *exact* number in the table above without a GCP account:

```sh
make setup && make test
uv run qkd-ekm-model all -o out/model
uv run qkd-ekm-analysis all -o out/analysis
uv run python scripts/compare.py out expected --only model,analysis -o out/COMPARISON.md
```

`--only` matters here: without it `compare.py` also looks for `transcripts/s1.txt` and
`s2.txt`, reports them missing and exits 1, because those come from a deployment. The groups are
`model`, `analysis` and `transcripts`.

## Reading the results

```
results/20260819-101500/
├── env.json                 project, region, key, commit, tool versions
├── preflight.txt            one line per service health check
├── raw/                     verbatim service logs + marks.json (scenario time windows)
├── transcripts/             s1.txt, s2.txt, all.txt — redacted, publishable
├── s2/                      rotation output, upload output, bucket listing, the object, key
│                            versions (≥2 after rotation), audit log
├── qkd_capture.json         Table 4 fields as captured
├── continuity.json          the observed lifecycle-mode walk
├── model/                   states.csv, transitions.csv, seeded.csv, report.json/.md
├── analysis/                table9.csv, scalars.json, fig4/5/12/13 .png
└── COMPARISON.md/.json      the pass/fail table
```

Field-by-field schemas: [docs/evidence-schema.md](docs/evidence-schema.md).

`transcripts/s1.txt` and `s2.txt` are the package's counterparts to the paper's redacted
figures: `s1.txt` corresponds to supplementary Figs 5–6 (client session plus control trace) and
`s2.txt` to Figs 7–9 (client protection layer, EKM wrap request, upload success) — preceded by
the `s2` step's own external-key rotation, which is what makes the `EKM: Got Key Wrap request`
line reliable rather than served from Cloud Storage's cached wrapped DEK. Both are
merges of the client, VPN, EKM and upload logs sorted by timestamp, with operational values
replaced by the placeholders in [docs/redaction-rules.md](docs/redaction-rules.md); order and
timestamps are preserved exactly, which is what makes them comparable with the published
figures. The comparison checks each component's events in order *within that component's own
log* and never across components: the client and the cloud services timestamp with independent
clocks, which is why the paper's Fig. 8 itself prints the client's "Getting key" line after the
EKM response it causally precedes.

`COMPARISON.md` rows map onto the paper's claim register (supplementary Table 11):

| COMPARISON rows | Claim |
|---|---|
| `s1.sequence` | **C3** — QKD identifiers coordinate WireGuard PSK activation |
| `s2.sequence`, plus `s2/` object listing and diff | **C1**, **C2** — external KEK boundary preserves provider DEKs; persistent version binding and purpose separation |
| `qkd_capture.json` (recorded, not compared) | **C4** — the captured operating point |
| `scalars.D` | **C5** — 0.001388889 units/s of configured lifecycle demand |
| `scalars.mQ`, `table9[*]` | **C6** — the 59,681.25-pair source-side ceiling, and Table 9 |
| `continuity.json` | **C7** — buffered continuity and binding holdover. Lifecycle modes are an observed classification derived from live dependency state; the prototype does not use them as an admission gate on `/new` or `:wrap` |
| `model.*` | **C8** — nine state properties and four transition guards across the enumerated graph |
| — | **C9** is an analytical transferability argument; nothing here executes it |

## Local development

```sh
make setup        # uv sync --extra dev
make test         # pytest: EKM crypto/JWT/bindings, simulator ETSI semantics, VPN control
make lint         # ruff
make tf-validate  # terraform validate, no cloud calls
```

The tests drive the client CLI end to end against an in-process simulator with WireGuard mocked,
so no root and no network are needed.

Three of the four services also run on a laptop, in three terminals — plaintext, a local
directory instead of Cloud Storage, and the simulator's defaults for everything else:

```sh
# 1. QKD simulator (ETSI-014 + the HEQA-shaped monitoring API), no TLS, no token
SIM_PORT=8200 uv run qkd-ekm-qkdsim

# 2. EKM. EKM_PLAINTEXT_HTTP=1 is the EKM's explicit opt-out from requiring a
#    certificate; EKMS_SA_EMAIL is the caller Cloud KMS would authenticate as.
EKM_PLAINTEXT_HTTP=1 EKM_PORT=8443 \
QKD1_URL=http://127.0.0.1:8200 EKM_PEERS=QKD2 \
EKM_DB=out/ekm.sqlite EKM_LOCAL_KEY_FILE=out/local.key \
EKMS_SA_EMAIL=service-000000000000@gcp-sa-ekms.iam.gserviceaccount.com \
VPN_TOKEN=local-token uv run qkd-ekm-ekm

# 3. Upload server, writing to out/bucket instead of GCS (DirSink)
UPLOAD_PORT=8081 UPLOAD_SINK_DIR=out/bucket \
GCS_BUCKET=local-bucket KMS_KEY_NAME=projects/local/locations/local/keyRings/l/cryptoKeys/l \
EKM_URL=http://127.0.0.1:8443 VPN_TOKEN=local-token uv run qkd-ekm-upload

# then, for example: a QKD key allocated through the EKM for an upload
curl -s -X POST http://127.0.0.1:8081/api/file_key \
  -H 'Content-Type: application/json' -d '{"peer":"QKD2"}'
uv run python scripts/capture_qkd.py --backend sim --url http://127.0.0.1:8200 \
  --user admin --password admin -o out/qkd_capture.json
```

The VPN control service is the exception: it creates a WireGuard interface at start-up, so it
needs `wg`, `CAP_NET_ADMIN` and root. Run it on the deployed `vpn-vm`, or exercise it through
`tests/test_vpn.py`, where `wg` is mocked.

### Iterating on the code

Once the stack is deployed, `scripts/redeploy.sh [role ...]` gets a code change onto the running
VMs without touching infrastructure: it rebuilds the wheel, `terraform apply`s (so any changed
`terraform.tfvars` value reaches instance metadata), then re-runs each named VM's startup script
and restarts its `qkd-ekm-*` units. With no arguments it does all five roles; `scripts/redeploy.sh
ekm` only touches `ekm-vm`, which is what picking up a new `ekm_jwt_audiences` value needs.

## Reproducibility and redaction

Every log this package publishes goes through `scripts/redact.py`, which replaces key ids,
addresses, e-mail identities, WireGuard public keys, bucket names and KMS resource paths with
fixed placeholders while preserving line order and timestamps, then refuses (`--check`) to emit
a transcript in which anything that looks like an unredacted secret survived. The rules, and the
order they are applied in, are in [docs/redaction-rules.md](docs/redaction-rules.md). `make
redact-check` re-runs that verification over the latest results directory.

QKD key material is never logged anywhere — only key *identifiers* — and `results/` is
gitignored, so nothing from a run is committed by accident.

## Security notes (this is a prototype)

The security boundaries the paper relies on are real and not bypassable: the EKM validates the
Google OIDC token Cloud KMS presents — signature, issuer, the `gcp-sa-ekms` service-agent
e-mail, and, by default, the audience (`ekm_jwt_audiences` defaults to
`https://ekm.qkd.internal`, the `https://<ekm hostname>` value Cloud EKM actually sends; setting
it to `""` is the only way to opt out of the audience check) — the VPN control API validates a
Google ID token against an allow-list, the EKM↔VPN channel carries a shared bearer token, and
`ekm-vm` and `workload-vm` admit no traffic from the internet. There is no development bypass
flag for any of them.

The convenience shortcuts, all deliberate and all inappropriate for production:

- self-signed TLS for the EKM and the simulator, pinned by CA file on each client;
- the EKM's TLS **private key** is delivered through instance metadata, and the shared tokens
  live in `/etc/qkd-ekm/env`;
- the EKM encrypts its 32-byte KEKs at rest with a VM-local key file — production would use an
  HSM or Secret Manager;
- on `client-vm` only, `/etc/qkd-ekm/env` is mode 0644 and mirrored into
  `/etc/profile.d/qkd-ekm.sh` so the runner's SSH user can read it;
- the upload server is unauthenticated: completing the QKD-keyed WireGuard handshake *is* its
  authentication, so anything that reaches the tunnel can also drain the EKM key pool;
- the VPN control API authorises callers against an allow-list of Google identities, but there
  is no peer↔identity binding: any allow-listed principal may refresh *any* peer's pre-shared
  key by presenting that peer's public key;
- all five VMs have an external address (cheaper than Cloud NAT); "internal only" is enforced by
  the absence of any internet-sourced firewall rule, not by the absence of an address.

## Limitations

- The default `sim` backend is an ETSI-014 **simulator**, not a QKD appliance. It reproduces the
  interface and the paper's Table 4 shape, not physics: captured values are recorded as
  provenance and never compared for equality. The author's HEQA Sceptre run is the real
  measurement ([docs/heqa-setup.md](docs/heqa-setup.md)).
- **No performance evaluation.** The paper does not qualify EKM latency, throughput or
  availability, and neither does this package; the analytics are source-side capacity bounds
  under declared assumptions, not measured service behaviour.
- The **production cryptographic profile** (NIST SP 800-38F key wrapping, HSM custody) is out of
  scope, as it is in the paper; wrapping here is AES-256-GCM with the bound QKD unit as KEK.
- The Avalonia GUI client of the original campaign was not rebuilt; the CLI carries the same
  workflow.
- The published upload-server figure prints the peer pair as `<PEER_B><-->PEER_A>`, where the
  second placeholder lost its opening angle bracket in typesetting. This package emits the
  well-formed `<PEER_B><--><PEER_A>`, so a character-by-character diff against the figure
  differs by that one bracket.

## Citation

```bibtex
@article{joseph2026qkdekm,
  title   = {A Resilient External Key Lifecycle Model for QKD-Supported Cloud Key
             Management in Critical Infrastructure},
  author  = {Joseph, Ofir and Aviv, Itzhak and Dahan, Eran and Hadar, Irit},
  journal = {TBD},
  year    = {2026},
  doi     = {10.xxxx/xxxxxx}
}
```

## License

MIT — see [LICENSE](LICENSE).
