#!/usr/bin/env python
"""Sweep the plausible readings of the paper's lifecycle prose against its
published enumeration counts (45,824 states / 307,680 transitions / depth 16).

Every knob is a boolean in `qkd_ekm.model.events.Rules`; each combination is a
full breadth-first enumeration. Results are printed as a markdown table (the
evidence kept in docs/model-calibration.md) and the sweep stops as soon as one
combination reproduces all three published numbers.

    uv run python scripts/model_calibrate.py [--stage singles|structural|edges|round2|all]
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from qkd_ekm.model import enumerate as enumeration
from qkd_ekm.model.events import DEFAULT_RULES, Rules

REPO_ROOT = Path(__file__).resolve().parent.parent
PAPER = json.loads((REPO_ROOT / "expected" / "paper_numbers.json").read_text())
TARGET = (PAPER["states"], PAPER["transitions"], PAPER["max_depth"])

# Knobs that change which states are reachable.
STRUCTURAL = (
    "alloc_vpn_needs_no_active",
    "vpn_pending_replaceable",
    "recover_clears_pending",
    "activate_merged",
    "ekm_down_clears_active",
    "restore_recovery_needs_full",
)
# Knobs that mostly change how many labeled edges join the same states.
EDGES = (
    "reject_stale_needs_active",
    "dep_toggles_unconditional",
    "ingest_needs_ekm",
    "expire_needs_source_down",
    "alloc_ekm_needs_source",
    "authority_only_when_degraded",
    "recover_needs_authority",
)
# Round-2 knobs: readings that add a degree of freedom to the state tuple.
ROUND2 = (
    "expire_marks_used",
    "suspend_state",
    "restoration_detected",
    "abandon_pending",
    "abandon_consumes_epoch",
    "down_clears_pending",
    "session_end",
    "recover_clears_active",
    "fifo_pool",
    "alloc_vpn_any_future_epoch",
    "reject_stale_per_epoch",
)
# The round-2 families swept as full products, each seeded with the round-1
# near miss (`expire_marks_used` + `ekm_down_clears_active`, 44,880 / 253,818 /
# depth 16). The first keeps the depth-preserving knobs together; the second
# explores session/epoch teardown; the third the ordered pool.
ROUND2_PRODUCTS = (
    (
        "expire_marks_used",
        "ekm_down_clears_active",
        "abandon_pending",
        "down_clears_pending",
        "suspend_state",
        "restoration_detected",
        "vpn_pending_replaceable",
    ),
    (
        "expire_marks_used",
        "ekm_down_clears_active",
        "session_end",
        "recover_clears_active",
        "recover_clears_pending",
        "alloc_vpn_needs_no_active",
    ),
    (
        "expire_marks_used",
        "ekm_down_clears_active",
        "abandon_pending",
        "abandon_consumes_epoch",
        "fifo_pool",
    ),
    (
        "alloc_vpn_any_future_epoch",
        "reject_stale_per_epoch",
        "expire_marks_used",
        "ekm_down_clears_active",
        "abandon_pending",
        "down_clears_pending",
    ),
)
# Round-1 readings the round-2 knobs are swept on top of, one knob at a time.
ROUND2_SEEDS = (
    (),
    ("expire_marks_used",),
    ("expire_marks_used", "ekm_down_clears_active"),
)
MAX_STATES = 150_000  # abandon a reading whose closure runs away

BASE = Rules()  # the brief's starting rule set


def describe(rules: Rules) -> str:
    on = [f for f in rules.__slots__ if getattr(rules, f) != getattr(BASE, f)]
    return "+".join(on) if on else "base"


def measure(rules: Rules) -> dict:
    t0 = time.perf_counter()
    result = enumeration.run(rules, max_states=MAX_STATES)
    return {
        "rules": describe(rules),
        "states": len(result.states),
        "transitions": len(result.transitions),
        "max_depth": result.max_depth,
        "violations": len(result.violations),
        "aborted": result.aborted,
        "seconds": round(time.perf_counter() - t0, 2),
        "distance": abs(len(result.states) - TARGET[0])
        + abs(len(result.transitions) - TARGET[1]),
    }


def combos(stage: str) -> list[Rules]:
    out = [BASE]
    if stage in ("singles", "all"):
        out += [replace(BASE, **{f: not getattr(BASE, f)}) for f in BASE.__slots__]
    if stage in ("structural", "all"):
        for bits in itertools.product((False, True), repeat=len(STRUCTURAL)):
            out.append(replace(BASE, **dict(zip(STRUCTURAL, bits, strict=True))))
    if stage in ("edges", "all"):
        # Edge knobs never change which states are reachable, so they are swept
        # on top of the base (paper-literal) structural reading.
        for bits in itertools.product((False, True), repeat=len(EDGES)):
            out.append(replace(BASE, **dict(zip(EDGES, bits, strict=True))))
    if stage in ("round2", "all"):
        for seed in ROUND2_SEEDS:
            start = replace(BASE, **dict.fromkeys(seed, True))
            out.append(start)
            out += [replace(start, **{f: True}) for f in ROUND2]
        for family in ROUND2_PRODUCTS:
            for bits in itertools.product((False, True), repeat=len(family)):
                out.append(replace(BASE, **dict(zip(family, bits, strict=True))))
    seen: dict[str, Rules] = {}
    for r in out:
        seen.setdefault(describe(r), r)
    return list(seen.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="model_calibrate")
    ap.add_argument(
        "--stage", default="all", choices=("singles", "structural", "edges", "round2", "all")
    )
    ap.add_argument("--top", type=int, default=0, help="print only the N closest readings")
    args = ap.parse_args(argv)

    plan = combos(args.stage)
    with ProcessPoolExecutor() as pool:
        rows = list(pool.map(measure, plan))

    shown = sorted(rows, key=lambda r: r["distance"]) if args.stage == "round2" else rows
    if args.top:
        shown = sorted(rows, key=lambda r: r["distance"])[: args.top]
    print("| # | rules | states | transitions | depth | violations |")
    print("| ---: | --- | ---: | ---: | ---: | ---: |")
    for i, row in enumerate(shown, start=1):
        states = "aborted" if row["aborted"] else row["states"]
        print(
            f"| {i} | {row['rules']} | {states} | {row['transitions']} "
            f"| {row['max_depth']} | {row['violations']} |"
        )

    match = next(
        (
            r
            for r in rows
            if (r["states"], r["transitions"], r["max_depth"]) == TARGET and not r["violations"]
        ),
        None,
    )
    best = min(rows, key=lambda r: (abs(r["states"] - TARGET[0]), abs(r["transitions"] - TARGET[1])))
    print()
    print(f"target: states={TARGET[0]} transitions={TARGET[1]} depth={TARGET[2]}")
    if match:
        print(f"EXACT MATCH: {match['rules']}")
    else:
        print(f"no exact match in {len(rows)} enumerations; closest on states: {best}")
    print(f"defaults in use: {describe(DEFAULT_RULES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
