#!/usr/bin/env python
"""Capture the paper's Table 4 dashboard values from a QKD appliance.

Works against a real HEQA appliance or the bundled simulator, which serves the
same monitoring routes:

    uv run python scripts/capture_qkd.py --backend sim --url http://127.0.0.1:8100 \
        --user admin --password admin -o results/qkd_capture.json

The written JSON carries the Table-4 fields plus the capture provenance
(`captured_at`, `backend`), the paper's own label for the secure key rate and
`derived_256bit_rate` -- the secure bit rate divided by 256, which is the
source rate the capacity analysis consumes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from qkd_ekm.qkd.heqa import _COUNTER_FIELDS, HeqaMonitor

SOURCE_LABEL = "Secure Key Rate (256bps)"


def _counters(monitor: HeqaMonitor, etsi_token: str, sae: str) -> dict:
    """The Table-4 counter block, read with the *key-delivery* credential.

    The monitoring API and the ETSI-014 key-delivery API authenticate
    separately -- the deployed simulator guards `/api/v1/keys/{sae}/status`
    with the appliance bearer token while `/monitoring/...` wants the login
    JWT -- so the counters need a second pass with the other credential.
    """
    monitor.token = etsi_token
    return monitor.get(f"/api/v1/keys/{sae}/status")["status_extension"]


def capture(
    url: str,
    user: str,
    password: str,
    backend: str,
    ca: str | None = None,
    token: str | None = None,
    etsi_token: str | None = None,
    sae: str = "QKD2",
) -> dict:
    monitor = HeqaMonitor(url, user, password, verify=ca or True)
    if token:
        monitor.token = token  # skip /auth/login when a JWT is supplied
    try:
        result = monitor.capture()
        if etsi_token and result.get("available_256bit_keys") is None:
            ext = _counters(monitor, etsi_token, sae)
            for field, ext_key in _COUNTER_FIELDS.items():
                result[field] = ext.get(ext_key)
    finally:
        monitor.close()

    rate = result.get("secure_bit_rate")
    result["derived_256bit_rate"] = rate / 256 if rate is not None else None
    result["captured_at"] = dt.datetime.now(dt.UTC).isoformat()
    result["backend"] = backend
    result["source_label_secure_key_rate"] = SOURCE_LABEL
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture_qkd.py", description=__doc__)
    # NOTE: --backend is recorded provenance only -- the simulator answers the
    # same monitoring routes, so one client covers both.
    parser.add_argument("--backend", choices=("sim", "heqa"), required=True)
    parser.add_argument("--url", required=True, help="appliance base URL")
    parser.add_argument("--user", default="", help="monitoring API username")
    parser.add_argument("--password", default="", help="monitoring API password")
    parser.add_argument("--ca", help="CA bundle used to verify the appliance certificate")
    parser.add_argument("--token", help="pre-issued JWT, used instead of logging in")
    parser.add_argument(
        "--etsi-token",
        help="bearer token for the ETSI-014 routes, if they authenticate separately "
        "from the monitoring API (the simulator does); used only to read the counters",
    )
    parser.add_argument("--sae", default="QKD2", help="SAE whose status block holds the counters")
    parser.add_argument("-o", "--out", default="results/qkd_capture.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = capture(
        args.url,
        args.user,
        args.password,
        args.backend,
        args.ca,
        args.token,
        args.etsi_token,
        args.sae,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"secure_bit_rate={result['secure_bit_rate']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
