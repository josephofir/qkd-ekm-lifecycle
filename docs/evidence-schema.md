# Evidence schema

What `scripts/run_experiment.sh` writes, and what each field means. One run is one directory:

```
results/<YYYYMMDD-HHMMSS>/        # results/latest is a symlink to the most recent one
├── env.json
├── preflight.txt
├── raw/
│   ├── marks.json
│   ├── client.log  vpn.log  ekm.log  upload.log
│   ├── client-connect.out  client-refresh.out  client-refresh-buffered.out
│   ├── s1-tunnel-checks.txt
│   ├── vpn-journal.txt  ekm-journal.txt
│   └── window-s1/  window-s2/
├── transcripts/     s1.txt  s2.txt  all.txt
├── s2/
│   ├── rotate.out  upload.out  bucket_listing.txt  sensitive.txt
│   ├── <uuid>_sensitive.txt
│   ├── kms_versions.json  kms_audit.json
├── qkd_capture.json
├── continuity.json
├── model/           states.csv  transitions.csv  seeded.csv  report.json  report.md
├── analysis/        table9.csv  scalars.json  fig4_capacity.png  fig5_reserve.png
│                    fig12_qber.png  fig13_refill.png
└── COMPARISON.md    COMPARISON.json
```

`results/` is gitignored: nothing from a run is committed by accident.

## `env.json`

Provenance for the whole run.

| Field | Meaning |
|---|---|
| `project`, `region` | parsed out of the `kms_key` output (`projects/P/locations/L/…`), which is the only place the module publishes them |
| `zone` | the `zone` terraform output |
| `bucket` | the CMEK data bucket |
| `kms_key`, `ekm_connection` | full KMS resource names |
| `qkd_backend` | `sim` or `heqa` (override with `QKD_BACKEND=heqa`) |
| `started_at` | UTC ISO-8601 |
| `commit`, `dirty_files` | git revision of the package, and how many files were uncommitted |
| `tools` | `gcloud`, `terraform`, `uv` version strings |

## `preflight.txt`

One tab-separated line per service: `label`, VM, and the raw `/healthz` body. The runner aborts
unless every body contains `"ok"`.

## `raw/`

Verbatim service logs, copied from `/var/log/qkd-ekm/*.log` on each VM — the same lines the
services print to stdout. Never publish these directly: they contain key ids, addresses,
identities and bucket names. `raw/*-journal.txt` are the last 500 journal lines of the two
long-running units, kept only as a fallback if a unit died before writing its log file.

`raw/marks.json` records the UTC window of each scenario:

```json
{"s1_start": "2026-08-19 10:15:03.000", "s1_end": "2026-08-19 10:15:41.999",
 "s2_start": "2026-08-19 10:15:42.000", "s2_end": "2026-08-19 10:15:49.999"}
```

`redact` filters each raw log to `[start, end]` by its leading timestamp (VMs run on UTC) and
writes the survivors to `raw/window-s1/` and `raw/window-s2/` before redacting them, so several
runs can share a directory without their transcripts bleeding into each other.

## `transcripts/`

Publishable. `s1.txt` and `s2.txt` are the per-scenario merges, `all.txt` the whole run. Every
line keeps its original timestamp and relative order; environment-specific values are replaced
by the placeholders in [redaction-rules.md](redaction-rules.md). `redact.py --check` fails the
step if a uuid, e-mail, IPv4 address, WireGuard key or KMS path survived.

## `s2/`

| File | Content |
|---|---|
| `rotate.out` | output of starting `qkd-ekm-rotate.service` on `ekm-vm` before the upload: creates CryptoKeyVersion `api/keys/v<n+1>` and makes it primary, so the wrap the upload triggers binds a fresh QKD unit rather than risking Cloud Storage's cached wrapped DEK from bucket creation or an earlier write. Expect `Rotated external key to version v<n+1>` (or, run standalone, just the version label `v<n+1>`) |
| `upload.out` | stdout of `qkd-ekm-client upload`: the client's log lines plus the `gs://…` object URI on the last line. **Never publish this file directly** — it is raw, unredacted output (bucket name, object uuid, key ids); publish the redacted `transcripts/s2.txt` instead |
| `bucket_listing.txt` | `gcloud storage ls gs://<bucket>/` |
| `sensitive.txt` | the plaintext as it was written on `client-vm` |
| `<uuid>_sensitive.txt` | the object downloaded back out of the CMEK bucket; `diff` against the above is part of the step |
| `kms_versions.json` | `gcloud kms keys versions list` — one `EXTERNAL_VPC` version per rotation, with `externalProtectionLevelOptions.ekmConnectionKeyPath` = `api/keys/v<n>`; because `s2` rotates before uploading, this now lists **at least two** versions (`v1` from `terraform apply`, plus `v<n+1>` from `rotate.out`) even on a single run |
| `kms_audit.json` | up to five recent `cloudkms.googleapis.com` audit entries (best effort: audit ingestion lags, and the reader needs `roles/logging.viewer`) |

## `qkd_capture.json`

Paper Table 4 / Table 7, as read from the appliance or the simulator. Rates and QBER come from
the monitoring API, counters from the ETSI-014 status block.

| Field | Unit | Paper value |
|---|---|---|
| `secure_bit_rate` | bit/s | 21,220 |
| `secure_key_rate_256` | 256-bit units/s, appliance label | 82 |
| `derived_256bit_rate` | 256-bit units/s, `secure_bit_rate / 256` | 82.890625 |
| `signal_qber`, `weak_decoy_qber` | % | 0.80, 1.00 |
| `signal_qber_per_state`, `weak_decoy_qber_per_state` | % ×6 | Fig. 12 bars |
| `available_secured_bits`, `available_256bit_keys` | bits, units | 838,847,064 / 3,275,971 |
| `consumed_keys`, `consumed_bits`, `key_requests`, `failed_key_requests` | counters | — |
| `max_key_length`, `generated_bits`, `deleted_bits` | bits | — |
| `captured_at`, `backend`, `source_label_secure_key_rate` | provenance | — |

`expected/qkd_capture_paper.json` holds the published capture in the same shape. A live capture
is recorded, never compared for equality: the simulator is an interface stand-in, not a source.
A field the endpoint does not serve comes back `null` rather than failing the capture.

## `continuity.json`

```json
{"sequence": ["READY","BUFFERED","BINDING_HOLDOVER","SUSPENDED","RECOVERY","READY"],
 "keys_drained": 49,
 "observations": {"ready": {…}, "source_down_buffered": {…},
                  "pool_empty_binding_holdover": {…},
                  "authority_withdrawn_suspended": {…},
                  "source_restored": {…}, "recovery_pending": {…},
                  "after_recovery_ack": {…}}}
```

Each observation is a verbatim `GET /api/state` body: `mode`, the consequence vector
(`storage`, `fresh_allocation`, `established_vpn`), `pool` sizes per peer, `bindings_count`,
`source_available`, `continuity_authority`, `recovery_pending`. `keys_drained` is how many
allocations the pool served after the outage before returning 503 — the buffered reserve.

Lifecycle modes are an **observed classification** derived from live dependency state; the
prototype does not use them as an admission gate on `/new` or `:wrap`. A request fails because
the pool is empty or the binding is missing, not because the mode says so.

A freshly deployed EKM can start in `RECOVERY`, not `READY`: its first pull can beat the QKD
simulator's own boot, so the source "returns" moments later and the lifecycle latches
recovery-pending (Fig. 3, T7–T10) before the scenario has done anything. The runner's first
action in `step_continuity` is therefore `POST /api/recovery/ack` (T11), which establishes the
`READY` baseline the rest of `sequence` is measured from — `observations.ready` is the state
*after* that ack, not the VM's boot-time state.

## `model/`

| File | Columns / fields |
|---|---|
| `states.csv` | one row per reachable state: `id`, `depth`, then the tuple of eq. (1) — `sq`, `se`, `pool`, `bindings`, `ke`, `kp`, `kv`, `epoch`, `authority`, `recovery` — plus the derived `mode` and consequence vector `C_S`, `C_N`, `C_V` |
| `transitions.csv` | `src`, `label`, `dst` (state ids) for all 307,680 labeled transitions |
| `seeded.csv` | `name`, `kind` (`state`/`transition`), `expected_check`, `detected_by` |
| `report.json` | `states`, `transitions`, `max_depth`, `invariants[]`, `guards[]`, `violations[]`, `seeded{cases,undetected,rows[]}` |
| `report.md` | the same as a readable summary |

Acceptance: 45,824 / 307,680 / depth 16, nine invariants, four guards, zero violations, and
9 + 4 seeded cases each detected by the check that should catch it.

## `analysis/`

- `table9.csv` — `pairs`, `demand` (units/s), `headroom` (equivalent pairs), `endurance_h`,
  `refill_min`, for 1 / 1,000 / 10,000 / 50,000 / 60,000 concurrent pairs. `refill_min` is
  `null` where demand exceeds the source rate (60,000 pairs).
- `scalars.json` — `D`, `mQ`, `reserve_24h_1000_pct`, `reserve_24h_10000_pct`,
  `depletion_50000_h`, `refill_1h_1000_min`, `refill_1h_10000_min`, `refill_1h_50000_h`.
- `table9_live.csv` — Table 9 recomputed from the live capture instead of the paper's values.
  Written only when `qkd_capture.json` carries both a source rate (`derived_256bit_rate`) and an
  inventory (`available_256bit_keys`); a partial capture just skips it.
- Figures 4 (capacity contour), 5 (reserve map), 12 (per-state QBER), 13 (refill map).

## `COMPARISON.md` / `.json`

One row per check: `name`, `expected`, `actual`, `result` (`PASS`/`FAIL`), `note`. The JSON is
the same rows as a list of objects. `compare.py` exits 1 if any row failed, so
`run_experiment.sh compare` (and `all`) exits non-zero on a failed reproduction. Row groups:
`model.*`, `table9[pairs].field`, `scalars.*`, then one row per scenario per component
(`s1.VPNClient`, `s2.FileUploadServer`, …) and a `s1.sequence` / `s2.sequence` summary. The
README maps those groups onto the paper's claims C1–C9.

The transcript check requires each component's expected events to appear in order **within that
component's own lines**, and deliberately does not constrain the order between components: the
client and the cloud services timestamp with independent clocks, so a merged transcript
interleaves them differently from run to run — the paper's Fig. 8 prints the client's "Getting
key" line after the EKM response that it causally precedes, for exactly that reason.
