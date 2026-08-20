"""CLI: `qkd-ekm-analysis all` -> table9.csv, scalars.json, PNGs (+ live table9 if captured)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import equations, figures, tables

R = 82.890625
B = 3_275_971

# Table 4 published per-state QBER, used when no --paper-capture file is given
# (expected/qkd_capture_paper.json is produced by a later capture task).
PAPER_CAPTURE_DEFAULT = {
    "signal_qber_per_state": [0.19, 1.04, 1.45, 0.27, 0.83, 0.69],
    "weak_decoy_qber_per_state": [0.48, 1.53, 1.65, 0.69, 1.20, 0.48],
}


def _load_json(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def run_all(capture_path, paper_capture_path, out_dir) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    paper_capture = _load_json(paper_capture_path) or PAPER_CAPTURE_DEFAULT

    tables.table9(r=R, B=B).to_csv(out / "table9.csv", index=False)

    D = equations.demand()
    D1000, D10000, D50000 = D * 1000, D * 10000, D * 50000
    scalars = {
        "D": D,
        "mQ": equations.ceiling(R, D),
        "reserve_24h_1000_pct": equations.reserve_pct(B, D1000, 24 * 3600),
        "reserve_24h_10000_pct": equations.reserve_pct(B, D10000, 24 * 3600),
        "depletion_50000_h": equations.depletion_s(B, D50000, 0.0) / 3600,
        "refill_1h_1000_min": equations.refill_s(D1000, 3600, R) / 60,
        "refill_1h_10000_min": equations.refill_s(D10000, 3600, R) / 60,
        "refill_1h_50000_h": equations.refill_s(D50000, 3600, R) / 3600,
    }
    (out / "scalars.json").write_text(json.dumps(scalars, indent=2))

    figures.fig4_capacity(str(out / "fig4_capacity.png"))
    figures.fig5_reserve(str(out / "fig5_reserve.png"))
    figures.fig12_qber(paper_capture, str(out / "fig12_qber.png"))
    figures.fig13_refill(str(out / "fig13_refill.png"))

    # Live capture is optional and may be missing/partial: skip live-only
    # artifacts gracefully rather than failing the whole run.
    live_capture = _load_json(capture_path)
    if live_capture:
        # A capture written by scripts/capture_qkd.py names these two after the
        # appliance dashboard; the older explicit names still work.
        r_live = live_capture.get("derived_256bit_rate") or live_capture.get(
            "source_rate_units_per_s"
        )
        b_live = live_capture.get("available_256bit_keys") or live_capture.get("inventory_units")
        if r_live and b_live:
            tables.table9(r=r_live, B=b_live).to_csv(out / "table9_live.csv", index=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qkd-ekm-analysis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("all", help="write table9.csv, scalars.json, and figures 4/5/12/13")
    p_all.add_argument("--capture", default="results/qkd_capture.json")
    p_all.add_argument("--paper-capture", default="expected/qkd_capture_paper.json")
    p_all.add_argument("-o", "--out", default="results/analysis")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "all":
        run_all(args.capture, args.paper_capture, args.out)


if __name__ == "__main__":
    main()
