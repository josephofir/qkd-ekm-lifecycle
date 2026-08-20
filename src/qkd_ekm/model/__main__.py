"""CLI: `qkd-ekm-model enumerate|seeded|all -o results/model`."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from . import enumerate as enumeration
from . import seeded as seeded_mod


def _write_seeded(out: Path) -> dict:
    rows = seeded_mod.run_seeded()
    with (out / "seeded.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "kind", "expected_check", "detected_by"])
        for r in rows:
            w.writerow([r["name"], r["kind"], r["expected_check"], "|".join(r["detected_by"])])
    return {
        "cases": len(rows),
        "undetected": sum(1 for r in rows if r["expected_check"] not in r["detected_by"]),
        "rows": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qkd-ekm-model")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("enumerate", "exhaustive reachable-state enumeration"),
        ("seeded", "seeded invalid-state and invalid-transition detection"),
        ("all", "enumeration followed by seeded detection"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-o", "--out", default="results/model")
    # `make model` runs the bare command: default it to the full run. This has
    # to follow add_subparsers, whose own default for `command` is None.
    parser.set_defaults(command="all", out="results/model")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report: dict = {}
    if args.command in ("enumerate", "all"):
        result = enumeration.run()
        report = enumeration.write(result, out)
        print(
            f"states={report['states']} transitions={report['transitions']} "
            f"max_depth={report['max_depth']} violations={len(report['violations'])}"
        )
    if args.command in ("seeded", "all"):
        summary = _write_seeded(out)
        print(f"seeded_cases={summary['cases']} undetected={summary['undetected']}")
        if report:
            report["seeded"] = summary
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            (out / "report.md").write_text(enumeration.markdown(report))
        else:
            summary_only = {"seeded": summary}
            (out / "seeded.json").write_text(json.dumps(summary_only, indent=2) + "\n")

    failed = bool(report.get("violations")) or bool(
        report.get("seeded", {}).get("undetected")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
