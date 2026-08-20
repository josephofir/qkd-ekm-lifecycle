#!/usr/bin/env python
"""Redact captured service logs into the paper's published transcripts.

Merges one or more raw log files, sorts them by their leading timestamp and
replaces every environment-specific value with the placeholder the paper's
supplementary figures print (`<KEY_ID>`, `<PEER_A>`, `<CLIENT>`, ...). Lines
are otherwise preserved verbatim.

    uv run python scripts/redact.py results/raw/*.log -o results/transcripts/s1.txt --check

`--check` fails (exit 1) if anything that looks like an unredacted secret --
a uuid, an e-mail address, an IPv4 address, a WireGuard public key or a KMS
resource path -- survives into the output.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_IPV4 = r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w.])"
# WireGuard keys are 32 bytes of base64: 43 payload characters plus padding.
_WG_KEY = r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])"

# Order matters: uuids go first so that nothing later reaches inside a key id,
# and the peer names go last for the same reason.
RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_UUID), "<KEY_ID>"),
    (re.compile(r"KEKId: \S+"), "KEKId: <KEK_ID>"),
    # The object id is usually a uuid, already replaced above -- and `<KEY_ID>`
    # itself contains an underscore, so it needs its own alternative here.
    (re.compile(r"gs://[^/\s]+/(?:<KEY_ID>|[^_\s]+)_"), "gs://<BUCKET>/<OBJECT_ID>_"),
    (re.compile(r"projects/\S+/cryptoKeys/\S+"), "<EKM_KEY_NAME>"),
    (re.compile(_EMAIL), "<CLIENT>"),
    (re.compile(_IPV4), "<PRIVATE_IP>"),
    (re.compile(_WG_KEY), "<PUBKEY>"),
    (re.compile(r"\bQKD1\b"), "<PEER_A>"),
    (re.compile(r"\bQKD2\b"), "<PEER_B>"),
]

RESIDUAL: list[tuple[str, re.Pattern[str]]] = [
    ("uuid", re.compile(_UUID)),
    ("email", re.compile(_EMAIL)),
    ("ipv4", re.compile(_IPV4)),
    ("wireguard key", re.compile(_WG_KEY)),
    ("kms resource path", re.compile(r"projects/\S*/cryptoKeys/")),
]

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")


def redact_line(line: str) -> str:
    """Apply every redaction rule, in order, to one log line."""
    for pattern, replacement in RULES:
        line = pattern.sub(replacement, line)
    return line


def _timestamp(line: str) -> str:
    match = _TIMESTAMP.match(line)
    return match.group(0) if match else ""


def redact_files(paths: Iterable[str | Path]) -> list[str]:
    """Merge the given log files, sort by leading timestamp, redact each line."""
    lines: list[str] = []
    for path in paths:
        lines += Path(path).read_text().splitlines()
    lines.sort(key=_timestamp)  # stable: same-timestamp lines keep input order
    return [redact_line(line) for line in lines]


def residuals(lines: Iterable[str]) -> list[str]:
    """Names of the residual patterns still present after redaction."""
    text = "\n".join(lines)
    return [name for name, pattern in RESIDUAL if pattern.search(text)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="redact.py", description=__doc__)
    parser.add_argument("inputs", nargs="+", help="raw log files to merge and redact")
    parser.add_argument("-o", "--out", required=True, help="redacted transcript to write")
    parser.add_argument(
        "--check", action="store_true", help="fail if unredacted values remain in the output"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lines = redact_files(args.inputs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(line + "\n" for line in lines))
    if args.check:
        found = residuals(lines)
        if found:
            print(f"{out}: unredacted {', '.join(found)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
