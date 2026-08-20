# Redaction rules

Supplementary §10 requires that released records "preserve the original line order and
timestamps while replacing operational identifiers with explicit placeholders", and exclude
credentials, reusable tokens, private addresses and sensitive infrastructure identifiers.
`scripts/redact.py` is that rule set, executable.

```sh
uv run python scripts/redact.py results/latest/raw/*.log \
  -o results/latest/transcripts/all.txt --check
```

It merges the given files, sorts the lines by their leading timestamp (a **stable** sort, so
lines sharing a millisecond keep their input order), applies the rules below to each line, and
writes the result. `run_experiment.sh redact` does this per scenario; `make redact-check`
re-runs it over the most recent results directory.

## Placeholders

Applied in this order — the order matters, because an earlier rule must not leave text that a
later rule would reach into.

| # | Matches | Placeholder | Why |
|---:|---|---|---|
| 1 | any UUID | `<KEY_ID>` | QKD key identifiers and object ids. First, so nothing later rewrites part of one. |
| 2 | `KEKId: <token>` | `KEKId: <KEK_ID>` | the external key version Cloud KMS asked the EKM to wrap with (`v1`, `v2`, …) |
| 3 | `gs://<bucket>/<id>_` | `gs://<BUCKET>/<OBJECT_ID>_` | bucket name and object prefix, keeping the `_<filename>` suffix visible |
| 4 | `projects/…/cryptoKeys/…` | `<EKM_KEY_NAME>` | the full KMS resource path |
| 5 | e-mail address | `<CLIENT>` | the authenticated principal (a user or a service account) |
| 6 | IPv4 address, optionally with a prefix length | `<PRIVATE_IP>` | tunnel and VPC addresses |
| 7 | 44-character base64 WireGuard key | `<PUBKEY>` | peer public keys |
| 8 | `QKD1` | `<PEER_A>` | the cloud-side SAE |
| 9 | `QKD2` | `<PEER_B>` | the client-side SAE |

Peer names go last for the same reason UUIDs go first: they are short and would otherwise be
rewritten inside a longer token that an earlier rule had already replaced.

Timestamps, component names and message text are never altered. Key *material* never reaches a
log in the first place — the services log identifiers only — so redaction is a second line of
defence, not the only one.

## `--check`

With `--check`, the script re-scans its own output and exits 1 if any of these survived:

| Pattern | Name in the failure message |
|---|---|
| UUID | `uuid` |
| e-mail address | `email` |
| IPv4 address | `ipv4` |
| WireGuard public key | `wireguard key` |
| `projects/…/cryptoKeys/` | `kms resource path` |

The runner always passes `--check`, so a transcript that still carries an unredacted value fails
the step instead of being published. If you add a log line that prints a new kind of
environment-specific value, add a rule *and* a residual pattern for it here.
