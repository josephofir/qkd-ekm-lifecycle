# Architecture

How the deployed package realises the paper's boundary: components, the two workflow
sequences, the lifecycle modes, and the timers. The README has the topology diagram; this
document is the level below it.

## 1. Components

| Component | Runs on | Interface | Authentication |
|---|---|---|---|
| `qkd_ekm.ekm` | `ekm-vm` | `POST /api/keys/{key_id}:wrap`, `:unwrap` | Google OIDC JWT — signature, issuer, the `gcp-sa-ekms` service-agent e-mail, and (once `ekm_jwt_audiences` is non-empty, which is the default) the audience: `iss` = `accounts.google.com`, `email` = `service-<project-number>@gcp-sa-ekms.iam.gserviceaccount.com`, `aud` ∈ `ekm_jwt_audiences`. Confirmed on the first live run: Cloud EKM's `aud` is `https://<ekm hostname>`, i.e. `https://ekm.qkd.internal`, which `ekm_jwt_audiences` now defaults to; setting it to `""` opts out of the audience check |
| | | `GET /api/{peer}/new?purpose=vpn\|file` | shared bearer `VPN_TOKEN` |
| | | `GET /api/state`, `POST /api/authority`, `POST /api/recovery/ack` | shared bearer `VPN_TOKEN` |
| `qkd_ekm.vpn` | `vpn-vm` | `POST /api/start_connection`, `/api/refresh_connection`, `GET /api/status` | Google ID token of an allow-listed principal (`VPN_ALLOWED_EMAILS`) |
| `qkd_ekm.upload` | `workload-vm` | `POST /api/file_key`, `POST /api/upload` | none — reachability over the WireGuard tunnel *is* the authentication |
| `qkd_ekm.qkdsim` | `qkdsim-vm` | ETSI-014 `/api/v1/keys/{sae}/{status,enc_keys,dec_keys}`, `POST /sim/source` | bearer `SIM_TOKEN` |
| | | `/auth/login`, `/monitoring/...`, `/kms/key-servers` | JWT from `/auth/login` |
| `qkd_ekm.client` | `client-vm` | CLI `connect`, `refresh`, `upload`, `status`, `disconnect` | — |

Every service logs `YYYY-MM-DD HH:MM:SS.mmm <Component>: <message>` to stdout and to its
`LOG_FILE` under `/var/log/qkd-ekm/`. Components: `VPNClient`, `VPNServer`, `EKM`,
`FileUploadServer`, `QKDSim`. Key material is never logged — only key identifiers.

## 2. The wrap path: KMS → Service Directory → `ekm-vm`

`ekm-vm` has no public address and no public DNS name, so Cloud EKM reaches it over the VPC:

1. A `google_service_directory_endpoint` registers `ekm-vm`'s reserved internal address and port
   8443 in the namespace `qkd-ekm-<suffix>`, inside network `qkd-vpc-<suffix>` — both suffixed
   per deployment (`random_id.suffix`) so a `terraform destroy` → `terraform apply` cycle in the
   same project never reuses a VPC or namespace name; see §7.
2. A `google_kms_ekm_connection` (`key_management_mode = MANUAL`) points at that Service
   Directory *service*, declares the hostname `ekm.qkd.internal`, and pins the EKM's self-signed
   certificate as DER. Cloud KMS validates the TLS handshake against that pinned certificate.
3. The Cloud EKM service agent (`service-<number>@gcp-sa-ekms`) holds
   `roles/servicedirectory.viewer` and `roles/servicedirectory.pscAuthorizedService`; the
   firewall admits `35.199.192.0/19` to `ekm-vm:8443`.
4. The `EXTERNAL_VPC` CryptoKey's version *n* carries
   `ekm_connection_key_path = api/keys/v<n>`, which is exactly the path Cloud KMS then calls:
   `POST https://ekm.qkd.internal:8443/api/keys/v<n>:wrap`.
5. `ekm-vm` verifies the OIDC token, and on the first wrap of an unseen `key_id` allocates one
   QKD unit from the pool, persists the binding `(qkd_key_id, purpose=ekm, object_id=v<n>)`
   **before** answering, and uses that unit as the KEK: AES-256-GCM with the request's
   `additionalAuthenticatedData` as AAD. Every later wrap or unwrap of `v<n>` finds the same
   unit or fails closed.

Cloud Storage enters this path only indirectly: the bucket's `default_kms_key_name` is that
CryptoKey, so writing an object makes GCS ask KMS to wrap the object's data-encryption key, and
KMS asks the EKM. The provider-generated DEK never leaves Google — which is the paper's C1.

## 3. S1 — QKD-supported VPN access

```
client-vm                      vpn-vm                    ekm-vm            qkdsim-vm
   │ wg genkey                    │                         │                  │
   │ POST /api/start_connection ─►│  (Google ID token)      │                  │
   │   {qkd_id: QKD2, public_key} │ GET /api/QKD2/new ─────►│                  │
   │                              │                         │ pool.allocate()  │
   │                              │◄─ {key_id, key, QKD2} ──│ (pre-pulled with │
   │                              │ wg set peer <psk>       │  enc_keys) ◄─────│
   │◄─ {preshared_key_id, server_public_key, endpoint,      │                  │
   │    client_ip, allowed_ips, effective_time} ────────────│                  │
   │ GET /api/v1/keys/QKD1/dec_keys?key_ID=<preshared_key_id> ─────────────────►│
   │◄─ the same 32 bytes, delivered over the quantum link ──────────────────────│
   │ wg-quick up (PSK installed)  │                         │                  │
```

The control API never sends key *material* — only the identifier. Both ends end up holding a
pre-shared key that never crossed the public network, which is the paper's C3.

`refresh` repeats the allocation and returns `effective_time = now + VPN_ACTIVATION_DELAY_S`
(30 s). The server schedules `wg set` at that moment and the client sleeps until it, so the
tunnel changes PSK on both sides together instead of blackholing.

## 4. S2 — managed storage and external key

Before the sequence below, `run_experiment.sh s2` starts `qkd-ekm-rotate.service` on `ekm-vm`
(the same unit `qkd-ekm-rotate.timer` fires every `rotation_minutes`): it creates
CryptoKeyVersion `api/keys/v<n+1>` and makes it primary, so the wrap the upload triggers binds a
fresh QKD unit on its first use. Without that rotation, Cloud Storage can still serve the
wrapped DEK it cached from bucket creation or an earlier write for some minutes, so the upload
may complete without the EKM ever seeing a fresh `:wrap` — the `EKM: Got Key Wrap request` line
of the paper's Fig. 8 would then be missing. Confirmed on the second (from-zero) live cycle: the
first deployment's run showed the line without an explicit rotation, a later from-zero
deployment's did not until one was forced. The result is recorded in `s2/rotate.out`.

```
client-vm                       workload-vm              ekm-vm         GCS / Cloud KMS
   │ POST /api/file_key ─────────►│ GET /api/QKD2/new?purpose=file ─►│
   │◄─ {key_id} ──────────────────│◄──────────── {key_id, key} ──────│
   │ dec_keys(key_id) from QKD2   │  (cached by id, TTL 600 s)       │
   │ AES-256-GCM(file, aad=name)  │                                  │
   │ POST /api/upload ───────────►│ unwrap the client layer          │
   │      (through the tunnel)    │ upload gs://bucket/<uuid>_<name> ├──► GCS
   │                              │                                  │    │ CMEK
   │                              │                                  │◄───┘ wrap DEK
   │◄─ {object, size} ────────────│                                  │
```

Three protection layers, exactly as in supplementary Table 3: the optional client-side QKD layer
(removed at the workload before storage), the provider-managed DEK, and the external-key
operation the EKM performs. The object stored in the bucket is therefore the plaintext the
client wrote — `run_experiment.sh s2` downloads it and diffs it to prove the round trip.

## 5. Lifecycle modes

`GET /api/state` derives the mode live from pool, source, binding and operator inputs
(`qkd_ekm.ekm.lifecycle.derive`, shared with the reference model so both cannot drift):

| Mode | Condition | Managed storage | Established VPN | Fresh allocation | Paper |
|---|---|---|---|---|---|
| `READY` | source up, EKM reachable | normal | active | yes | Table 4 row 1 |
| `BUFFERED` | source down, pool non-empty | normal | active, refresh from buffer | yes | Table 4 row 2 |
| `BINDING_HOLDOVER` | source down, pool empty, an authoritative binding exists | existing versions still usable | active | no | Table 4 rows 3–4 |
| `EXHAUSTED` | source down, pool empty, no binding | — | — | no | Table 4 row 3 |
| `SUSPENDED` | source down, pool empty **and** continuity authority withdrawn — it outranks `BINDING_HOLDOVER` and `EXHAUSTED`, not `BUFFERED`; an explicit policy-suspension record outranks everything but `RECOVERY` | — | — | no | Fig. 3, T5/T6 |
| `RECOVERY` | the source came back and no operator has acknowledged it | resumes after ack | active | no | Table 4 row 5 |

The response also carries the consequence vector (`storage`, `fresh_allocation`,
`established_vpn`), the per-peer pool sizes, the binding count and the two operator flags.

Two behaviours are worth knowing before reading `continuity.json`:

- The pool only calls the QKD source when it is **below** `EKM_POOL_TARGET`, so a full pool does
  not notice a dead source until a key is spent. The runner therefore spends one (a VPN
  `refresh`) right after injecting the outage.
- `RECOVERY` is latched on the first `/api/state` read that sees the source back after having
  seen it down, and cleared only by `POST /api/recovery/ack`. Reading the state is part of the
  protocol, not just observation.
- A freshly deployed EKM can boot straight into `RECOVERY`: its first pull can run before the
  QKD simulator has finished booting, so when the source "returns" moments later the lifecycle
  latches recovery-pending exactly as Fig. 3's T7–T10 describe, with no fault ever injected.
  Confirmed on the first live run, this is why `run_experiment.sh continuity` POSTs
  `/api/recovery/ack` (T11) *before* anything else, to establish the `READY` baseline the rest
  of the walk assumes.

## 6. Timers

| Timer | Where | Period | Effect |
|---|---|---|---|
| Key pulling | `ekm-vm` | `EKM_PULL_INTERVAL` (2 s) | tops each peer's pool up to `EKM_POOL_TARGET` (50) via `enc_keys` |
| Key TTL | `ekm-vm` | 2 s sweep | drops pooled keys unused for `EKM_POOL_TTL` (600 s) |
| External key rotation | `ekm-vm`, `qkd-ekm-rotate.timer` | `rotation_minutes` (15) | creates CryptoKeyVersion *n+1* with key path `api/keys/v<n+1>` and makes it primary; the EKM binds a fresh QKD unit on its first wrap |
| VPN refresh | `vpn-vm`, per peer | `vpn_refresh_seconds` (3600) | flags `refresh_due`; the client drives the actual rotation |
| Coordinated activation | both ends | `vpn_activation_delay_seconds` (30) | the announced `effective_time` at which the new PSK is installed |

Those two cadences are what the paper's demand equation consumes: one EKM rotation per 900 s
plus one VPN refresh per 3600 s is 0.001388889 256-bit units per second (claim C5). `s2`
triggers `qkd-ekm-rotate.service` out of band (§4); the timer keeps running independently, so a
long-lived deployment still rotates every `rotation_minutes` regardless of scenario runs.

## 7. Per-deployment naming

`qkd-vpc-<suffix>` and the Service Directory namespace `qkd-ekm-<suffix>` (§2), the KMS key
ring, and the EKM connection name all carry a `random_id.suffix` generated once per `terraform
apply`. Two things forced this: Cloud KMS EKM connections, like key rings and keys, cannot be
deleted — `terraform destroy` only forgets them, so a fixed connection name would collide with
the forgotten one on the next `apply` — and after recreating a same-named VPC or Service
Directory namespace, Cloud EKM could not reach the new VPC at all (`Timed out when trying to
access the EKM host`, including for connections that predated the recreation, even though
in-VPC TLS to `ekm-vm` worked). Confirmed on the second (from-zero) live cycle: with the
suffixes in place, `terraform destroy` → `terraform apply` in the same project succeeds in one
apply. Forgotten connections and key rings linger at no cost. See
[terraform/README.md](../terraform/README.md) and
[docs/troubleshooting.md](troubleshooting.md).
