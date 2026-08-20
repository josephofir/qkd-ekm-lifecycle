import json
from pathlib import Path

import pandas as pd
import pytest

from qkd_ekm.analysis import equations, figures, tables
from qkd_ekm.analysis.__main__ import PAPER_CAPTURE_DEFAULT, main

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((REPO_ROOT / "expected" / "paper_numbers.json").read_text())

R = EXPECTED["source_rate_units_per_s"]
B = EXPECTED["inventory_units"]


# --- equations ---------------------------------------------------------


def test_demand_default_matches_paper():
    assert equations.demand() == pytest.approx(0.001388889, abs=1e-9)


def test_ceiling_matches_paper():
    D = equations.demand()
    assert equations.ceiling(R, D) == pytest.approx(59681.25, abs=1e-6)


def test_endurance_s_matches_table9_pairs1():
    D = equations.demand()
    exp = next(r for r in EXPECTED["table9"] if r["pairs"] == 1)
    endurance_h = equations.endurance_s(B, D) / 3600
    assert endurance_h == pytest.approx(exp["endurance_h"], rel=1e-4)


def test_refill_s_none_when_demand_exceeds_rate():
    # pairs=60000 row has demand >= r -> no possible refill
    assert equations.refill_s(D=83.4, t_out=3600, r_u=R) is None


def test_depletion_s_matches_50000_pair_scenario():
    D = equations.demand() * 50000
    depletion_h = equations.depletion_s(B, D, r_u=0.0) / 3600
    assert depletion_h == pytest.approx(EXPECTED["depletion_50000_h"], abs=0.1)


def test_reserve_pct_matches_24h_scenarios():
    D1000 = equations.demand() * 1000
    D10000 = equations.demand() * 10000
    assert equations.reserve_pct(B, D1000, 24 * 3600) == pytest.approx(
        EXPECTED["reserve_24h_1000_pct"], abs=0.1
    )
    assert equations.reserve_pct(B, D10000, 24 * 3600) == pytest.approx(
        EXPECTED["reserve_24h_10000_pct"], abs=0.1
    )


def test_reserve_pct_clipped_to_0_100():
    D = equations.demand() * 60000
    assert equations.reserve_pct(B, D, 1_000_000_000) == 0.0
    assert equations.reserve_pct(B, 1e-12, 0) == pytest.approx(100.0)


# --- tables --------------------------------------------------------------


def test_table9_matches_expected_rows():
    df = tables.table9(r=R, B=B)
    assert list(df.columns) == ["pairs", "demand", "headroom", "endurance_h", "refill_min"]
    for exp in EXPECTED["table9"]:
        row = df[df["pairs"] == exp["pairs"]].iloc[0]
        assert row["demand"] == pytest.approx(exp["demand"], rel=1e-3)
        assert row["headroom"] == pytest.approx(exp["headroom"], rel=1e-3)
        assert row["endurance_h"] == pytest.approx(exp["endurance_h"], rel=1e-3)
        if exp["refill_min"] is None:
            assert pd.isna(row["refill_min"])
        else:
            assert row["refill_min"] == pytest.approx(exp["refill_min"], rel=2e-2)


def test_qber_states_table():
    capture = dict(PAPER_CAPTURE_DEFAULT)
    df = tables.qber_states(capture)
    assert len(df) == 6
    assert list(df["signal_qber"]) == capture["signal_qber_per_state"]
    assert list(df["weak_decoy_qber"]) == capture["weak_decoy_qber_per_state"]


# --- figures (deterministic, file-producing) ------------------------------


def test_fig4_capacity_creates_file(tmp_path):
    out = tmp_path / "fig4.png"
    figures.fig4_capacity(str(out))
    assert out.exists() and out.stat().st_size > 0


def test_fig5_reserve_creates_file(tmp_path):
    out = tmp_path / "fig5.png"
    figures.fig5_reserve(str(out))
    assert out.exists() and out.stat().st_size > 0


def test_fig12_qber_creates_file(tmp_path):
    out = tmp_path / "fig12.png"
    figures.fig12_qber(PAPER_CAPTURE_DEFAULT, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_fig13_refill_creates_file(tmp_path):
    out = tmp_path / "fig13.png"
    figures.fig13_refill(str(out))
    assert out.exists() and out.stat().st_size > 0


# --- CLI -------------------------------------------------------------------


def test_cli_all_produces_paper_outputs_without_live_capture(tmp_path):
    out_dir = tmp_path / "analysis"
    missing_capture = tmp_path / "nope.json"
    missing_paper_capture = tmp_path / "nope_paper.json"

    main(
        [
            "all",
            "--capture",
            str(missing_capture),
            "--paper-capture",
            str(missing_paper_capture),
            "-o",
            str(out_dir),
        ]
    )

    assert (out_dir / "table9.csv").exists()
    assert (out_dir / "scalars.json").exists()
    assert (out_dir / "fig4_capacity.png").exists()
    assert (out_dir / "fig5_reserve.png").exists()
    assert (out_dir / "fig12_qber.png").exists()
    assert (out_dir / "fig13_refill.png").exists()
    # live capture missing -> no live-only artifact
    assert not (out_dir / "table9_live.csv").exists()

    scalars = json.loads((out_dir / "scalars.json").read_text())
    assert scalars["D"] == pytest.approx(0.001388889, abs=1e-6)
    assert scalars["mQ"] == pytest.approx(59681.25, abs=1e-3)
    assert scalars["reserve_24h_1000_pct"] == pytest.approx(
        EXPECTED["reserve_24h_1000_pct"], abs=0.2
    )
    assert scalars["reserve_24h_10000_pct"] == pytest.approx(
        EXPECTED["reserve_24h_10000_pct"], abs=0.2
    )
    assert scalars["depletion_50000_h"] == pytest.approx(EXPECTED["depletion_50000_h"], abs=0.2)
    assert scalars["refill_1h_1000_min"] == pytest.approx(
        EXPECTED["refill_1h_1000_min"], abs=0.1
    )
    assert scalars["refill_1h_10000_min"] == pytest.approx(
        EXPECTED["refill_1h_10000_min"], abs=0.2
    )
    assert scalars["refill_1h_50000_h"] == pytest.approx(EXPECTED["refill_1h_50000_h"], abs=0.1)


def test_cli_all_with_live_capture_writes_table9_live(tmp_path):
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(
        json.dumps({"source_rate_units_per_s": R, "inventory_units": B})
    )
    out_dir = tmp_path / "analysis2"

    main(["all", "--capture", str(capture_path), "-o", str(out_dir)])

    assert (out_dir / "table9_live.csv").exists()
    live_df = pd.read_csv(out_dir / "table9_live.csv")
    assert set(live_df["pairs"]) == {1, 1000, 10000, 50000, 60000}


def test_cli_all_with_a_capture_qkd_shaped_file_writes_table9_live(tmp_path):
    """The field names scripts/capture_qkd.py actually writes."""
    out_dir = tmp_path / "out"
    capture = tmp_path / "qkd_capture.json"
    capture.write_text(
        json.dumps({"derived_256bit_rate": R, "available_256bit_keys": B, "backend": "sim"})
    )

    main(["all", "--capture", str(capture), "-o", str(out_dir)])

    live_df = pd.read_csv(out_dir / "table9_live.csv")
    assert set(live_df["pairs"]) == {1, 1000, 10000, 50000, 60000}


def test_cli_all_with_capture_missing_required_keys_skips_live_table(tmp_path):
    capture_path = tmp_path / "partial_capture.json"
    capture_path.write_text(json.dumps({"signal_qber_per_state": [0.1]}))
    out_dir = tmp_path / "analysis3"

    main(["all", "--capture", str(capture_path), "-o", str(out_dir)])

    assert (out_dir / "table9.csv").exists()
    assert not (out_dir / "table9_live.csv").exists()
