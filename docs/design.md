# qkd-ekm-lifecycle — design spec

Reproducibility package for *A Resilient External Key Lifecycle Model for QKD-Supported
Cloud Key Management in Critical Infrastructure* (Joseph, Aviv, Dahan, Hadar). Public,
MIT, built from scratch from the manuscript + supplementary + HEQA Sceptre API/manual.

Goal: `git clone` → `terraform apply` → `./scripts/run_experiment.sh` → `results/` that
reproduces the paper's evidence (S1 VPN workflow, S2 storage workflow, QKD capture,
finite-state enumeration, analytics/figures) and a pass/fail comparison with the published
numbers.

## 1. Decisions (already made with the author)

| Topic | Decision |
|---|---|
| Language | Python 3.12 everywhere (services, client, simulator, model, analytics). One `pyproject.toml`, package `qkd_ekm`, installed with `uv`/pip. |
| Cloud | GCP, new project, region `me-west1` (fallback: nearest region where EKM-via-VPC is available; documented if used). |
| KMS↔EKM path | Cloud EKM **via VPC** (Service Directory + self-signed server cert, no public DNS). |
| QKD | `QKD_BACKEND=sim` (ETSI GS QKD 014 simulator, default; reviewers/CI) or `heqa` (real Sceptre pair; author's lab). Same client code. |
| VPN client | CLI (`qkd-ekm-client`) running on a Linux "client VM" outside the cloud VPC. Avalonia GUI not rebuilt. |
| Reproduction scope | Exact: model counts (45,824 / 307,680 / depth 16, 9 invariants, 4 guards, 9+4 seeded detections), analytics (Table 9, Figs 4/5/13, 59,681.25). Functional: S1/S2 event sequences match the redacted logs. Device values: captured via script, not compared for equality. |
| Hosting | 5 small GCE VMs + systemd; no containers in prod. Bootstrap from a tarball Terraform uploads to a bucket (works before the repo is public). |

## 2. Architecture

```
                 ┌──────────────────── GCP project ────────────────────────────────────┐
                 │  VPC "qkd-vpc"  (10.10.0.0/24)                                        │
 client VM ──────┼─► vpn-vm  (ext IP, WG udp/51819, control API :8080 via IAP tunnel)    │
 (separate VPC,  │      │  GET /api/{qkd}/new  (bearer VPN_TOKEN)                         │
  Google ID tok) │      ▼                                                                 │
                 │   ekm-vm (internal only, :8443 TLS)  ◄── Cloud KMS (EKM via VPC,       │
                 │      │  POST /api/keys/{id}:wrap|:unwrap      Service Directory, PSC)  │
                 │      │  GET  /{sae}/enc_keys ──► qkd-sim-vm "QKD1" (:8200)             │
                 │      │                               (ext IP, also serves "QKD2" to    │
                 │      │                                the client VM's IP)              │
                 │   workload-vm (internal only; FileUploadServer :8081)                  │
                 │      │  writes to GCS bucket (CMEK → KMS EXTERNAL_VPC key → ekm-vm)    │
                 └──────────────────────────────────────────────────────────────────────┘
```

Maps 1:1 to supplementary Fig. 1/3/4: EKM = REST endpoints + SQLite + temp pool +
KeyPullingService + KeyTTLService + AES-256 service; VPN = WireGuard + REST control +
VPN manager + connection timer; client = QKD2 retrieval + WG PSK + file encrypt/upload.

### 2.1 Components (package `qkd_ekm`)

| Module | Runs on | Purpose |
|---|---|---|
| `qkd_ekm.qkd` | lib | ETSI-014 client (`enc_keys`, `dec_keys`, `status`) + HEQA monitoring client (`/auth/login`, `/monitoring/...`). TLS verify via configured CA. |
| `qkd_ekm.qkdsim` | qkd-sim-vm / local | FastAPI. Two SAEs (`QKD1`,`QKD2`) over one key store (random 256-bit units, UUID key_ID), paired delivery; HEQA-shaped monitoring subset (`secure-bit-rate`, `signal-qber`, `decoy-qber`, `*-states-qber`, key counters, `/auth/login` JWT) so `capture_qkd.py` works against both. Fault injection: `POST /sim/source {available:bool}`. |
| `qkd_ekm.ekm` | ekm-vm | FastAPI + SQLite. `POST /api/keys/{key_id}:wrap`, `:unwrap` (Google OIDC JWT: `iss accounts.google.com`, `email == service-<num>@gcp-sa-ekms…`, `aud == configured`). `GET /api/{qkd_name}/new` (bearer `VPN_TOKEN`) → `{key_id, qkd_name}`. Background: KeyPullingService (poll QKD1 at `POOL_TARGET`), KeyTTLService (drop unused pool entries older than `POOL_TTL`). Persistent tables: `bindings(key_id, purpose ∈ {ekm,vpn}, object_id, qkd_key_id, created)`; pool in memory. Wrap = AES-256-GCM with the bound QKD unit as KEK, AAD = `additionalAuthenticatedData`. First use of an unseen `key_id` allocates+binds one QKD unit (persist before responding). `GET /api/state` → lifecycle mode + consequence vector (derived live from source/EKM/pool/bindings). |
| `qkd_ekm.vpn` | vpn-vm | FastAPI. `POST /api/start_connection {qkd_id, public_key}` → `GET ekm/api/QKD1/new` returns `{key_id, key (b64, 32B), qkd_name}` (as in supplementary Fig. 1); `wg set` peer with PSK = that key; returns `{qkd_id:"QKD1", server_public_key, endpoint, preshared_key_id, allowed_ips, client_ip, effective_time}`. `POST /api/refresh_connection` → new id + `effective_time = now+30s`; timer service applies PSK at effective time. Auth: Google ID token of an allowed principal (`ALLOWED_EMAILS`). |
| `qkd_ekm.upload` | workload-vm | FastAPI `POST /upload` (reachable only over the tunnel) → `gs://<bucket>/<uuid>_<name>` via GCS client; bucket CMEK triggers KMS→EKM wrap. Logs mirror paper's `FileUploadServer:` lines. |
| `qkd_ekm.client` | client VM | CLI: `connect`, `refresh`, `upload <file>`, `status`, `disconnect`. Uses `wg`/`wg-quick`; pulls `dec_keys?key_ID=` from QKD2; client-side AES-GCM of the file with a second QKD unit (the paper's optional client protection layer); logs mirror `VPNClient:` lines. |
| `qkd_ekm.model` | anywhere | Lifecycle reference model (§4). |
| `qkd_ekm.analysis` | anywhere | Equations, tables, figures (§5). |

Log format everywhere: `YYYY-MM-DD HH:MM:SS.mmm <Component>: <message>` — exactly the
transcript style in supplementary Figs 5/7/8 so redaction produces comparable output.

### 2.2 Terraform (`terraform/`)

Single root module, `terraform.tfvars.example`. Resources:
project services; VPC `qkd-vpc` + subnet; VPC `client-vpc` + subnet; Cloud NAT for both;
firewall (IAP `35.235.240.0/20`→22/8080 on vpn; EKM `35.199.192.0/19`→8443 on ekm;
vpn→ekm 8443; ekm→qkd-sim 8200; client-vm-ext-IP→qkd-sim 8200; world→vpn udp/51819;
tunnel subnet→workload 8081); service accounts (ekm: KMS admin on the keyring for rotation
+ storage none; workload: objectCreator on bucket; vpn/client: none); `random_password`
VPN_TOKEN + `tls_self_signed_cert` for ekm (`ekm.qkd.internal`); Service Directory
namespace/service/endpoint → ekm internal IP:8443; `google_kms_ekm_connection` (manual
mode, hostname, DER cert); keyring + `google_kms_crypto_key` (`EXTERNAL_VPC`,
`crypto_key_backend`, `skip_initial_version_creation`) + initial
`google_kms_crypto_key_version` with `ekm_connection_key_path = api/keys/v1`; IAM for
`service-<num>@gcp-sa-ekms` (servicedirectory.viewer, pscAuthorizedService) and
`service-<num>@gs-project-accounts` (cryptoKeyEncrypterDecrypter); GCS bucket with
`default_kms_key_name`; 5 VMs (e2-small, Debian 12) with startup scripts that download the
uploaded `dist.tar.gz`, `pip install`, write `/etc/qkd-ekm/env`, enable systemd units;
IAP tunnel IAM for `var.operator_emails`. Outputs: IPs, bucket, key resource name, commands.

Rotation: systemd timer on ekm-vm (`qkd-ekm-rotate.timer`, 15 min) runs
`qkd-ekm-rotate` → creates CryptoKeyVersion with key path `api/keys/v<n+1>` and makes it
primary (EKM binds a fresh QKD unit on first wrap). VPN refresh timer: 60 min per
connection, 30 s coordinated activation (`connection timer service`).

### 2.3 Experiment runner (`scripts/run_experiment.sh`)

Runs from the operator's laptop with `gcloud` + `terraform output`:
1. `preflight` — terraform outputs, IAP tunnel up, services healthy.
2. `S1` — ssh client-vm: `qkd-ekm-client connect --server <vpn-ext-ip> --qkd QKD2`; verify
   tunnel (`wg show`, curl workload over tunnel); trigger `refresh`; collect client/server/EKM
   logs.
3. `S2` — ssh client-vm: `qkd-ekm-client upload sensitive.txt`; verify object exists,
   `gsutil cp` back, inspect; collect upload-server + EKM wrap log; show
   `gcloud kms keys versions describe` and Cloud Audit log line for EKM request.
4. `capture` — `capture_qkd.py` → `results/qkd_capture.json` (sim or HEQA values).
5. `continuity` — inject `source unavailable` on sim → show EKM `/api/state` = BUFFERED,
   allocation still works from pool; restore → READY (qualitative observation in paper §8
   of supplementary).
6. `model` — `python -m qkd_ekm.model enumerate` → tables + report.
7. `analysis` — `python -m qkd_ekm.analysis all` → CSVs + PNG figures.
8. `redact` — `redact.py` turns raw logs into faithful transcripts (`<KEY_ID>`, `<PEER_A>`,
   `<BUCKET>`…), preserving order/timestamps.
9. `compare` — `compare.py results/ expected/` → `results/COMPARISON.md` pass/fail per
   published number + S1/S2 event-sequence match.

`make test` locally: pytest over EKM crypto/JWT/bindings, sim ETSI semantics, VPN control
(wg mocked), client flows against in-process sim, model counts, analytics values. No
WireGuard locally.

## 3. Security boundaries (kept, not simplified)
- EKM validates Google OIDC JWT (JWKS from googleapis, iss/aud/email). No dev bypass flag.
- VPN control API validates Google ID token, allow-list of emails.
- EKM↔VPN shared bearer token; ekm-vm has no public IP; QKD sim is TLS + token.
- Client-side file encryption: AES-256-GCM; QKD material never logged; redaction before
  publishing any log.
- Key material in SQLite only as bindings of *identifiers*; the EKM stores the 32-byte KEK
  encrypted at rest with a VM-local key (file 0600) — good enough for a prototype,
  flagged in README (production: HSM/secret manager).

## 4. Lifecycle reference model (`qkd_ekm.model`)

State `X=(SQ, SE, B, M, KE, KP, KV, E, C, R)` per paper eq. (1); finite domains: ids
{k1..k4}, EKM versions {v1,v2}, epochs {none,1,2,3}; events: ingest(k), expire(k),
alloc_ekm(v), alloc_vpn, activate_vpn(e), reject_stale, src_down/up, ekm_down/up,
authority_grant/withdraw, suspend, recover. Derived `L=f(X)` ∈ {READY, BUFFERED,
BINDING_HOLDOVER, EXHAUSTED, RECOVERY, SUSPENDED}; `Y=g(X,Γ)=(C_S,C_N,C_V)`.
BFS to closure; assert I1–I9 on every state, G1–G4 on every transition; 9 seeded invalid
states + 4 seeded invalid transitions must each trip ≥1 check. Outputs:
`states.csv`, `transitions.csv`, `seeded.csv`, `report.json/.md`.
**Acceptance: 45,824 states, 307,680 transitions, max depth 16.** The exact enablement
rules are tuned during implementation to hit these counts; if the published counts cannot
be reproduced from the paper's stated semantics, the delta is reported in
`COMPARISON.md`, not hidden.

## 5. Analytics (`qkd_ekm.analysis`)
Inputs: `expected/qkd_capture_paper.json` (Table 4 values) or a live capture. Equations
(3)–(8): D, m_Q, T_out, T_dep, T_refill; Table 9 rows (1/1k/10k/50k/60k pairs); Fig. 4
capacity contour (rotation × refresh), Fig. 5 reserve map (β=1), Fig. 13 refill map;
Fig. 12 per-state QBER bars. pandas/numpy/matplotlib, deterministic.

## 6. Repo layout
```
README.md  LICENSE  pyproject.toml  Makefile
terraform/            main.tf variables.tf outputs.tf vms.tf kms.tf network.tf startup/*.sh
src/qkd_ekm/          qkd/ qkdsim/ ekm/ vpn/ upload/ client/ model/ analysis/ common/
scripts/              run_experiment.sh capture_qkd.py redact.py compare.py build_dist.sh
expected/             paper_numbers.json  s1_events.txt  s2_events.txt  qkd_capture_paper.json
docs/                 architecture.md  heqa-setup.md (real devices)  evidence-schema.md
                      redaction-rules.md  troubleshooting.md  design.md
tests/                unit + local integration
results/ (gitignored) + docs/sample-results/ (one verified run, redacted)
```

## 7. Verification plan (what "done" means)
1. `make test` green locally.
2. New GCP project → `terraform apply` clean from zero (and `destroy` clean).
3. `run_experiment.sh` end-to-end on GCP with the simulator; `COMPARISON.md` all-pass
   for model + analytics; S1/S2 event sequences equal `expected/s*_events.txt`
   (order-insensitive within the same millisecond).
4. README walkthrough followed literally by me in a fresh shell.
5. Author runs the same with `QKD_BACKEND=heqa` (docs/heqa-setup.md) — out of my reach.

## 8. Out of scope
Avalonia GUI; production crypto profile (NIST SP 800-38F wrapping); HA/latency
qualification; AWS/Azure transfer; Cisco SKIP.
