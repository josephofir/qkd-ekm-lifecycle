# Running against real HEQA Sceptre appliances

The default `qkd_backend = "sim"` deploys an ETSI-014 simulator so reviewers can reproduce the
package without hardware. `qkd_backend = "heqa"` points the same code at a real Sceptre pair —
the client and the EKM change nothing but their base URL, CA bundle and token.

Nothing in this document is executed by CI: it is the checklist the author's lab run follows.
Confirm ports and route names against your own appliance's manual — firmware versions differ.

## 1. SAE naming and roles

| Name in this package | Role | Talks to |
|---|---|---|
| `QKD1` | cloud-side, **master** SAE | `ekm-vm` (`GET /api/v1/keys/QKD2/enc_keys`) |
| `QKD2` | client-side, **slave** SAE | `client-vm` (`GET /api/v1/keys/QKD1/dec_keys?key_ID=…`) |

The EKM asks QKD1 to *originate* keys for the pair (`enc_keys` names the **slave**, `QKD2`), and
the client retrieves the same key from QKD2 by id (`dec_keys` names the **master**, `QKD1`).
Keep the SAE ids configured on the appliances equal to those strings, or set `EKM_PEERS` /
`--my-qkd` / `--peer-qkd` to whatever your appliances call themselves — but then the redacted
transcripts will not carry the `<PEER_A>` / `<PEER_B>` placeholders, since `redact.py` maps the
literal names `QKD1` and `QKD2`.

## 2. Ports

| Port | Interface | Used by |
|---|---|---|
| `8200` | ETSI GS QKD 014 key delivery (`/api/v1/keys/…`), TLS | `ekm-vm` → QKD1, `client-vm` → QKD2 |
| `8100` | appliance monitoring / management REST API on some firmware builds | `capture_qkd.py` |
| `443` | the web dashboard, and the monitoring API on other builds | `capture_qkd.py`, humans |

`capture_qkd.py --url` takes whichever of `8100` / `443` serves `/auth/login` and
`/monitoring/...` on your build; `qkd1_url` / `qkd2_url` in `terraform.tfvars` are always the
`:8200` key-delivery endpoints.

## 3. Exporting the appliance certificate

The EKM and the client verify the appliances' TLS certificates against a pinned CA bundle
(`QKD_CA_FILE`), so you need the certificate — or its issuer — as PEM. Either export it from the
dashboard, or read it off the key-delivery listener, which is also a good reachability test:

```sh
# confirm the ETSI interface answers and the SAE ids are what you think they are
curl --cacert qkd1.pem https://<qkd1-host>:8200/api/v1/keys/QKD2/status
```

The `status` response carries `slave_SAE_ID`, `master_SAE_ID`, `key_size`, `stored_key_count`
and the vendor `status_extension` block. If you have no CA yet, `openssl s_client -showcerts
-connect <qkd1-host>:8200 </dev/null` prints the chain the appliance presents; save the issuer
(or the leaf, for a self-signed appliance) as PEM. Do not disable verification — the ETSI
interface is where key material is delivered.

## 4. Monitoring API login

The Table 4 dashboard values come from the JWT-authenticated monitoring API, which is a separate
credential from the key-delivery interface:

```sh
curl -sk -X POST https://<host>:8100/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<password>"}'      # -> {"token": "<jwt>"}
```

`capture_qkd.py` performs that login itself given `--user` / `--password`, or accepts a
pre-issued JWT with `--token`.

## 5. `terraform.tfvars`

```hcl
project_id      = "my-qkd-project"
operator_emails = ["you@example.com"]

qkd_backend = "heqa"
qkd1_url    = "https://qkd1.lab.example:8200"   # reachable from ekm-vm
qkd2_url    = "https://qkd2.lab.example:8200"   # reachable from client-vm
qkd_token   = "…"                               # bearer token for the ETSI routes, if any
qkd_ca_pem  = <<-EOT
  -----BEGIN CERTIFICATE-----
  …
  -----END CERTIFICATE-----
EOT
```

With `qkd_backend = "heqa"` the simulator VM is still created (the module builds all five roles)
but nothing points at it; `qkd_token` and `qkd_ca_pem` replace the generated simulator token and
certificate in every env file. Run the experiment with `QKD_BACKEND=heqa
scripts/run_experiment.sh all` so the capture records the right provenance.

`run_experiment.sh continuity` calls `POST /sim/source` to inject an outage; a real appliance has
no such route, so that step is simulator-only. Reproduce the continuity observations on hardware
by interrupting the quantum channel (the paper's qualitative observation) and polling
`GET /api/state` on the EKM.

## 6. Network reachability (operator-provided)

This package does **not** build connectivity between Google Cloud and your lab. Two paths have
to exist before `terraform apply`, and both are yours to provide — typically a Cloud VPN tunnel
or a Dedicated/Partner Interconnect into `qkd-vpc-<suffix>` and `client-vpc`:

| From | To | Why |
|---|---|---|
| `ekm-vm` (qkd-vpc-<suffix>, `10.10.0.0/24`) | QKD1 `:8200` | pool refill (`enc_keys`) |
| `client-vm` (client-vpc, `10.30.0.0/24`) | QKD2 `:8200` | key retrieval (`dec_keys`) and the capture step |

Add the matching firewall rules on the appliance side (the `qkdsim-*` rules in `network.tf` are
for the simulator VM and do nothing for a real appliance), and confirm both hops with the
`curl … /status` call above before running the experiment. `preflight` will otherwise fail on the
first health check.

## 7. `capture_qkd.py --backend heqa` field mapping

```sh
uv run python scripts/capture_qkd.py --backend heqa \
  --url https://<host>:8100 --user <user> --password <password> \
  --ca qkd1.pem --sae QKD2 -o results/latest/qkd_capture.json
```

| Table 4 row | Field in `qkd_capture.json` | Source route |
|---|---|---|
| Secure Key Rate (bps) | `secure_bit_rate` | `/monitoring/qkd-qtx/secure-bit-rate/current` |
| Secure Key Rate (256bps), appliance label | `secure_key_rate_256` | derived: `secure_bit_rate / 256` |
| — (analysis input) | `derived_256bit_rate` | same division, kept separately as the rate the capacity equations consume |
| Signal QBER | `signal_qber` | `/monitoring/qkd-qtx/signal-qber/current` |
| Weak decoy QBER | `weak_decoy_qber` | `/monitoring/qkd-qtx/decoy-qber/current` |
| Signal QBER per state (×6) | `signal_qber_per_state` | `/monitoring/qkd-qtx/signal-states-qber/current` |
| Weak decoy QBER per state (×6) | `weak_decoy_qber_per_state` | `/monitoring/qkd-qtx/decoy-states-qber/current` |
| Available secured bits | `available_secured_bits` | `status_extension.current_bits` |
| Available 256-bit keys | `available_256bit_keys` | `status_extension.num_of_256_keys_available` |
| Consumed bits | `consumed_bits` | `status_extension.bits_consumed_from_the_beginning` |
| Consumed keys | `consumed_keys` | `status_extension.keys_consumed_from_the_beginning` |
| Key requests | `key_requests` | `status_extension.key_requests_from_the_beginning` |
| Failed key requests | `failed_key_requests` | `status_extension.num_failed_key_requests_from_the_beginning` |
| Maximal key length | `max_key_length` | `status_extension.maximum_key_length` |
| Generated bits | `generated_bits` | `status_extension.total_generated_bits` |
| Deleted bits | `deleted_bits` | `status_extension.deleted_bits` |

`run_experiment.sh capture` skips itself when `QKD_BACKEND` is not `sim` and prints a pointer
here, because the monitoring endpoint and its credential are not the ones in the VM's env file.
Run the command above by hand and write the result to `results/<run>/qkd_capture.json` before
the `analysis` step.

The `status_extension` block is read from `/api/v1/keys/<sae>/status`, where `<sae>` is
`--sae` (default `QKD2`) or, if the appliance serves `/kms/key-servers`, the `slaveSAE` it
reports there. On builds where the ETSI routes authenticate separately from the monitoring API,
pass that credential as `--etsi-token`; any field the appliance does not serve is written as
`null` rather than failing the capture.

Captured values are recorded as provenance and never compared for equality — see
`expected/qkd_capture_paper.json` for the published capture in the same shape.
