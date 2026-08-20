#!/usr/bin/env python
"""Compare a results directory against the paper's published numbers.

    uv run python scripts/compare.py results expected -o results/COMPARISON.md

Checks, in order:

* `model/report.json` -- reachable states, labeled transitions, maximum depth,
  the invariant/guard counts and the seeded detections;
* `analysis/table9.csv` -- every Table 9 row, field by field;
* `analysis/scalars.json` -- the ceiling and the reserve/depletion/refill spot
  values quoted in the text;
* `transcripts/s1.txt` and `transcripts/s2.txt` -- the redacted transcripts must
  contain the expected event lines as an ordered subsequence *per component*
  (extra lines, such as "Connected", are allowed). Order is only required
  within one component's own log: the client and the cloud services keep
  independent clocks, so their lines interleave differently from run to run --
  the paper's own Figure 8 prints the client's "Getting key" line after the
  EKM response it precedes causally.

Writes a markdown table plus the same rows as JSON, and exits 1 on any FAIL.
`--only model,analysis` restricts it to the groups a run without a deployment
can produce.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import NamedTuple

CALIBRATION_NOTE = "not reproduced; see docs/model-calibration.md"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s*")
_COMPONENT = re.compile(r"^([A-Za-z]+): ")

TABLE9_TOL = {"demand": 0.001, "headroom": 0.001, "endurance_h": 0.01, "refill_min": 0.01}
SCALARS = (
    # (key in scalars.json, key in paper_numbers.json, absolute tolerance)
    ("D", "demand_units_per_s", 1e-6),
    ("mQ", "ceiling_pairs", 0.01),
    ("reserve_24h_1000_pct", "reserve_24h_1000_pct", 0.05),
    ("reserve_24h_10000_pct", "reserve_24h_10000_pct", 0.05),
    ("depletion_50000_h", "depletion_50000_h", 0.05),
    ("refill_1h_1000_min", "refill_1h_1000_min", 0.05),
    ("refill_1h_10000_min", "refill_1h_10000_min", 0.05),
    ("refill_1h_50000_h", "refill_1h_50000_h", 0.05),
)


class Check(NamedTuple):
    name: str
    expected: str
    actual: str
    result: str
    note: str = ""


def _fmt(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _check(name: str, expected: object, actual: object, ok: bool, note: str = "") -> Check:
    return Check(name, _fmt(expected), _fmt(actual), "PASS" if ok else "FAIL", note if not ok else "")


def _close(expected: float | None, actual: float | None, tol: float) -> bool:
    if expected is None or actual is None or _is_nan(expected) or _is_nan(actual):
        return _missing(expected) and _missing(actual)
    return abs(expected - actual) <= tol


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _missing(value: object) -> bool:
    return value is None or _is_nan(value)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- (a) lifecycle model ---------------------------------------------------


def _model_checks(results: Path, paper: dict) -> list[Check]:
    path = results / "model" / "report.json"
    report = _load_json(path)
    if report is None:
        return [Check("model.report", "readable json", f"missing {path}", "FAIL", "run `make model`")]

    rows = report.get("seeded", {}).get("rows", [])

    def detected(kind: str) -> int:
        return sum(
            1 for r in rows if r["kind"] == kind and r["expected_check"] in r["detected_by"]
        )

    return [
        _check("model.states", paper["states"], report.get("states"),
               report.get("states") == paper["states"], CALIBRATION_NOTE),
        _check("model.transitions", paper["transitions"], report.get("transitions"),
               report.get("transitions") == paper["transitions"], CALIBRATION_NOTE),
        _check("model.max_depth", paper["max_depth"], report.get("max_depth"),
               report.get("max_depth") == paper["max_depth"], CALIBRATION_NOTE),
        _check("model.invariants", paper["invariants"], len(report.get("invariants", [])),
               len(report.get("invariants", [])) == paper["invariants"]),
        _check("model.guards", paper["guards"], len(report.get("guards", [])),
               len(report.get("guards", [])) == paper["guards"]),
        _check("model.seeded_states_detected", paper["seeded_states"], detected("state"),
               detected("state") == paper["seeded_states"], "run `qkd-ekm-model all`"),
        _check("model.seeded_transitions_detected", paper["seeded_transitions"],
               detected("transition"), detected("transition") == paper["seeded_transitions"],
               "run `qkd-ekm-model all`"),
    ]


# --- (b) Table 9 -----------------------------------------------------------


def _as_float(text: str | None) -> float | None:
    if text is None or text.strip() == "":
        return None
    value = float(text)
    return None if math.isnan(value) else value


def _table9_checks(results: Path, paper: dict) -> list[Check]:
    path = results / "analysis" / "table9.csv"
    if not path.exists():
        return [Check("table9", "readable csv", f"missing {path}", "FAIL", "run `make analysis`")]

    with path.open(newline="") as fh:
        actual = {int(float(row["pairs"])): row for row in csv.DictReader(fh)}

    checks: list[Check] = []
    for row in paper["table9"]:
        pairs = row["pairs"]
        got = actual.get(pairs)
        if got is None:
            checks.append(Check(f"table9[{pairs}]", "row present", "missing", "FAIL",
                                "no row for this concurrent-pair count"))
            continue
        for field, tol in TABLE9_TOL.items():
            value = _as_float(got.get(field))
            checks.append(
                _check(f"table9[{pairs}].{field}", row[field], value,
                       _close(row[field], value, tol), f"absolute tolerance {tol}")
            )
    return checks


# --- (c) scalar analytics --------------------------------------------------


def _scalar_checks(results: Path, paper: dict) -> list[Check]:
    path = results / "analysis" / "scalars.json"
    scalars = _load_json(path)
    if scalars is None:
        return [
            Check("scalars", "readable json", f"missing {path}", "FAIL", "run `make analysis`")
        ]
    return [
        _check(f"scalars.{key}", paper[paper_key], scalars.get(key),
               _close(paper[paper_key], scalars.get(key), tol), f"absolute tolerance {tol}")
        for key, paper_key, tol in SCALARS
    ]


# --- (d) redacted transcripts ---------------------------------------------


def _events(text: str) -> list[str]:
    return [_TIMESTAMP.sub("", line).strip() for line in text.splitlines() if line.strip()]


def _by_component(events: list[str]) -> dict[str, list[str]]:
    """Split `Component: message` lines into one ordered list per component."""
    grouped: dict[str, list[str]] = {}
    for event in events:
        match = _COMPONENT.match(event)
        if match:
            grouped.setdefault(match.group(1), []).append(event)
    return grouped


def _matched_prefix(expected: list[str], actual: list[str]) -> int:
    """How many leading expected events appear, in order, inside `actual`."""
    matched = 0
    for event in actual:
        if matched < len(expected) and event == expected[matched]:
            matched += 1
    return matched


def _sequence_checks(name: str, results: Path, expected_dir: Path) -> list[Check]:
    """One row per component, then a scenario summary row.

    Each component's own log has to carry its expected events in order; the
    relative order *between* components is not checked, because the client and
    the cloud services timestamp with independent clocks.
    """
    path = results / "transcripts" / f"{name}.txt"
    expected = _by_component(_events((expected_dir / f"{name}_events.txt").read_text()))
    total = sum(len(events) for events in expected.values())
    if not path.exists():
        return [Check(f"{name}.sequence", f"{total} events in order", f"missing {path}",
                      "FAIL", "capture and redact the scenario transcript")]

    actual = _by_component(_events(path.read_text()))
    checks: list[Check] = []
    matched_total = 0
    for component, events in expected.items():
        matched = _matched_prefix(events, actual.get(component, []))
        matched_total += matched
        checks.append(Check(
            f"{name}.{component}",
            f"{len(events)} events in order",
            f"{matched} of {len(events)} matched",
            "PASS" if matched == len(events) else "FAIL",
            "" if matched == len(events) else f"first missing event: {events[matched]}",
        ))
    ok = matched_total == total
    checks.append(Check(
        f"{name}.sequence",
        f"{total} events in order",
        f"{matched_total} of {total} matched",
        "PASS" if ok else "FAIL",
        "" if ok else "see the per-component rows above",
    ))
    return checks


# --- assembly --------------------------------------------------------------


GROUPS = ("model", "analysis", "transcripts")


def compare(
    results_dir: str | Path, expected_dir: str | Path, only: list[str] | None = None
) -> list[Check]:
    """Every check, or only the named groups (see GROUPS)."""
    results, expected = Path(results_dir), Path(expected_dir)
    paper = json.loads((expected / "paper_numbers.json").read_text())
    groups = set(only or GROUPS)
    checks: list[Check] = []
    if "model" in groups:
        checks += _model_checks(results, paper)
    if "analysis" in groups:
        checks += _table9_checks(results, paper) + _scalar_checks(results, paper)
    if "transcripts" in groups:
        checks += _sequence_checks("s1", results, expected)
        checks += _sequence_checks("s2", results, expected)
    return checks


def markdown(checks: list[Check]) -> str:
    failed = sum(1 for c in checks if c.result == "FAIL")
    lines = [
        "# Comparison against the published numbers",
        "",
        f"{len(checks) - failed} of {len(checks)} checks pass.",
        "",
        "| check | expected | actual | result | note |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    lines += [f"| {c.name} | {c.expected} | {c.actual} | {c.result} | {c.note} |" for c in checks]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compare.py", description=__doc__)
    parser.add_argument("results_dir", help="directory holding model/, analysis/, transcripts/")
    parser.add_argument("expected_dir", help="directory holding paper_numbers.json and s*_events")
    parser.add_argument("-o", "--out", default="results/COMPARISON.md")
    parser.add_argument(
        "--only",
        help="comma-separated subset of " + "/".join(GROUPS) + ", for a run that has no "
        "transcripts (the offline model + analytics reproduction, say)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    only = [g.strip() for g in args.only.split(",")] if args.only else None
    unknown = sorted(set(only or []) - set(GROUPS))
    if unknown:
        parser = build_parser()
        parser.error(f"unknown group(s) {', '.join(unknown)}; choose from {', '.join(GROUPS)}")
    checks = compare(args.results_dir, args.expected_dir, only)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown(checks))
    out.with_suffix(".json").write_text(
        json.dumps([c._asdict() for c in checks], indent=2) + "\n"
    )
    failed = [c for c in checks if c.result == "FAIL"]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks pass -> {out}")
    for c in failed:
        print(f"FAIL {c.name}: expected {c.expected}, got {c.actual} ({c.note})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
