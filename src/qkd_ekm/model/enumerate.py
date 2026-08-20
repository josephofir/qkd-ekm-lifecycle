"""Exhaustive breadth-first enumeration of the reachable-state closure."""

from __future__ import annotations

import csv
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import checks
from .events import DEFAULT_RULES, Rules, enabled
from .state import State, consequences, initial, mode


@dataclass(slots=True)
class Result:
    """Reachable states (mapped to their BFS depth) and labeled transitions.

    `rules` records the reading the enumeration actually ran with, so
    `write()`/`summary()` can report it by default instead of silently
    falling back to `DEFAULT_RULES` regardless of what `run()` used.
    """

    states: dict[State, int] = field(default_factory=dict)
    transitions: list[tuple[int, str, int]] = field(default_factory=list)
    max_depth: int = 0
    violations: list[dict] = field(default_factory=list)
    index: dict[State, int] = field(default_factory=dict)
    aborted: bool = False
    rules: Rules = DEFAULT_RULES


def run(rules: Rules = DEFAULT_RULES, max_states: int | None = None) -> Result:
    """Enumerate every reachable state, checking I1-I9 and G1-G4 on the way.

    `max_states` abandons the search once the closure grows past that many
    states; the calibration sweep uses it to skip runaway readings.
    """
    start = initial()
    depth = {start: 0}
    index = {start: 0}
    edges: set[tuple[int, str, int]] = set()
    violations: list[dict] = []
    queue = deque([start])
    retained_epoch = rules.session_end or rules.recover_clears_active or rules.abandon_consumes_epoch
    aborted = False

    for name in checks.invariants(start, retained_epoch=retained_epoch):
        violations.append({"kind": "invariant", "check": name, "state": 0})

    while queue and not aborted:
        s = queue.popleft()
        src = index[s]
        d = depth[s] + 1
        for label, nxt in enabled(s, rules):
            for name in checks.guards(label, s, nxt):
                violations.append({"kind": "guard", "check": name, "state": src, "label": label})
            known = index.get(nxt)
            if known is None:
                known = index[nxt] = len(index)
                depth[nxt] = d
                for name in checks.invariants(nxt, retained_epoch=retained_epoch):
                    violations.append({"kind": "invariant", "check": name, "state": known})
                queue.append(nxt)
            edges.add((src, label, known))
        if max_states is not None and len(index) > max_states:
            aborted = True

    return Result(
        states=depth,
        transitions=sorted(edges),
        max_depth=max(depth.values()),
        violations=violations,
        index=index,
        aborted=aborted,
        rules=rules,
    )


def write(result: Result, out_dir: str | Path, rules: Rules | None = None) -> dict:
    """Write states.csv, transitions.csv, report.json and report.md."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with (out / "states.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "id", "depth", "sq", "se", "pool", "bindings", "ke", "kp", "kv",
                "epoch", "authority", "recovery", "mode", "C_S", "C_N", "C_V",
            ]
        )
        for s, i in sorted(result.index.items(), key=lambda kv: kv[1]):
            c = consequences(s)
            w.writerow(
                [
                    i,
                    result.states[s],
                    int(s.sq),
                    int(s.se),
                    "|".join(sorted(s.pool)),
                    "|".join(sorted(f"{a}:{b}:{c2}" for (a, b, c2) in s.bindings)),
                    s.ke or "",
                    f"{s.kp[0]}:{s.kp[1]}" if s.kp else "",
                    f"{s.kv[0]}:{s.kv[1]}" if s.kv else "",
                    s.epoch,
                    int(s.authority),
                    int(s.recovery),
                    mode(s),
                    int(c["storage"]),
                    int(c["fresh_allocation"]),
                    c["established_vpn"],
                ]
            )

    with (out / "transitions.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["src", "label", "dst"])
        w.writerows(result.transitions)

    report = summary(result, rules)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "report.md").write_text(markdown(report))
    return report


def summary(result: Result, rules: Rules | None = None) -> dict:
    """Build the JSON-serialisable report for `result`.

    `rules` defaults to `result.rules` -- the reading `run()` actually used --
    rather than `DEFAULT_RULES`, so `report["rules"]` reflects the run even
    when it was produced with a non-default reading (e.g. a calibration
    sweep). Pass `rules` explicitly only to report a different reading than
    the one `result` was enumerated with.
    """
    effective_rules = result.rules if rules is None else rules
    by_mode: dict[str, int] = {}
    for s in result.states:
        by_mode[mode(s)] = by_mode.get(mode(s), 0) + 1
    return {
        "states": len(result.states),
        "transitions": len(result.transitions),
        "max_depth": result.max_depth,
        "invariants": sorted(checks.INVARIANTS),
        "guards": sorted(checks.GUARDS),
        "violations": result.violations,
        "states_by_mode": dict(sorted(by_mode.items())),
        "rules": {f: getattr(effective_rules, f) for f in effective_rules.__slots__},
    }


def markdown(report: dict) -> str:
    lines = [
        "# Lifecycle reference model report",
        "",
        f"- Reachable states: {report['states']}",
        f"- Labeled transitions: {report['transitions']}",
        f"- Maximum shortest-path depth: {report['max_depth']}",
        f"- State properties checked: {', '.join(report['invariants'])}",
        f"- Transition guards checked: {', '.join(report['guards'])}",
        f"- Violations: {len(report['violations'])}",
        "",
        "## Reachable states by lifecycle mode",
        "",
        "| Mode | States |",
        "| --- | ---: |",
    ]
    lines += [f"| {m} | {n} |" for m, n in report["states_by_mode"].items()]
    if "seeded" in report:
        seeded = report["seeded"]
        lines += [
            "",
            "## Seeded sensitivity analysis",
            "",
            f"- Seeded cases: {seeded['cases']}",
            f"- Undetected: {seeded['undetected']}",
            "",
            "| Case | Kind | Expected | Detected by |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r['name']} | {r['kind']} | {r['expected_check']} | {', '.join(r['detected_by'])} |"
            for r in seeded["rows"]
        ]
    return "\n".join(lines) + "\n"
