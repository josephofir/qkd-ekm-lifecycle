"""Tests for the evidence scripts: redaction, comparison, QKD capture."""

import base64
import csv
import importlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
# scripts/ is a directory of runnable programs, not a package: import them the
# way `uv run python scripts/x.py` does, by putting the directory on the path.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
capture_qkd = importlib.import_module("capture_qkd")
compare_mod = importlib.import_module("compare")
redact = importlib.import_module("redact")

PAPER = json.loads((REPO_ROOT / "expected" / "paper_numbers.json").read_text())
EXPECTED_DIR = REPO_ROOT / "expected"

TS = "2025-04-12 18:38:53.858 "
UUID = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
PUBKEY = base64.b64encode(bytes(range(32))).decode()


# --- redact_line ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (
            "EKM: Retrieving 1 keys between QKD1 and QKD2 from pool",
            "EKM: Retrieving 1 keys between <PEER_A> and <PEER_B> from pool",
        ),
        (
            "EKM: Got new key request for instance QKD2",
            "EKM: Got new key request for instance <PEER_B>",
        ),
        (
            "VPNServer: Client alice@example.com initiated VPN connection",
            "VPNServer: Client <CLIENT> initiated VPN connection",
        ),
        (
            f"VPNServer: Got new key with keyId: {UUID}",
            "VPNServer: Got new key with keyId: <KEY_ID>",
        ),
        (
            "VPNServer: Fetching new key for client QKD: QKD2",
            "VPNServer: Fetching new key for client QKD: <PEER_B>",
        ),
        (
            f"FileUploadServer: received key with Id {UUID} for qkds: QKD2<-->QKD1",
            "FileUploadServer: received key with Id <KEY_ID> for qkds: <PEER_B><--><PEER_A>",
        ),
        (
            f"FileUploadServer: uploading file to gs://qkd-demo-bucket/{UUID}_sensitive.txt",
            "FileUploadServer: uploading file to gs://<BUCKET>/<OBJECT_ID>_sensitive.txt",
        ),
        (
            (
                "FileUploadServer: using encryption key: "
                "projects/p1/locations/us-central1/keyRings/r1/cryptoKeys/ekm-key"
            ),
            "FileUploadServer: using encryption key: <EKM_KEY_NAME>",
        ),
        (
            "EKM: Got Key Wrap request with KEKId: api/keys/v3",
            "EKM: Got Key Wrap request with KEKId: <KEK_ID>",
        ),
        (
            "VPNServer: peer address 10.8.0.2/32 via 192.168.1.4",
            "VPNServer: peer address <PRIVATE_IP> via <PRIVATE_IP>",
        ),
        (
            "VPNClient: Getting key for file encryption shared with QKD1",
            "VPNClient: Getting key for file encryption shared with <PEER_A>",
        ),
        (
            "VPNClient: Client Started",
            "VPNClient: Client Started",
        ),
    ],
)
def test_redact_line_maps_raw_log_lines_to_paper_placeholders(raw, want):
    assert redact.redact_line(TS + raw) == TS + want


def test_redact_line_replaces_wireguard_public_key():
    line = f"{TS}VPNServer: peer {PUBKEY} configured"
    assert redact.redact_line(line) == f"{TS}VPNServer: peer <PUBKEY> configured"


def test_redact_line_leaves_peer_names_inside_key_ids_alone():
    # QKD1/QKD2 substitution must not reach into an already-redacted identifier.
    assert redact.redact_line(f"EKM: key {UUID}") == "EKM: key <KEY_ID>"


# --- redact_files / CLI ---------------------------------------------------


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def test_redact_files_merges_and_sorts_by_timestamp(tmp_path):
    a = _write(
        tmp_path / "a.log",
        [
            "2025-04-12 18:38:53.858 EKM: Got new key request for instance QKD2",
            "2025-04-12 18:38:55.000 EKM: second",
        ],
    )
    b = _write(
        tmp_path / "b.log",
        [
            "2025-04-12 18:38:54.100 VPNServer: Client bob@example.com initiated VPN connection",
        ],
    )

    lines = redact.redact_files([a, b])

    assert lines == [
        "2025-04-12 18:38:53.858 EKM: Got new key request for instance <PEER_B>",
        "2025-04-12 18:38:54.100 VPNServer: Client <CLIENT> initiated VPN connection",
        "2025-04-12 18:38:55.000 EKM: second",
    ]


def test_redact_cli_writes_output_and_check_passes(tmp_path):
    src = _write(tmp_path / "raw.log", [f"{TS}VPNServer: Got new key with keyId: {UUID}"])
    out = tmp_path / "s1.txt"

    rc = redact.main([str(src), "-o", str(out), "--check"])

    assert rc == 0
    assert out.read_text() == f"{TS}VPNServer: Got new key with keyId: <KEY_ID>\n"


def test_redact_check_fails_on_leftover_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(redact, "RULES", [r for r in redact.RULES if r[1] != "<KEY_ID>"])
    src = _write(tmp_path / "raw.log", [f"{TS}VPNServer: Got new key with keyId: {UUID}"])

    assert redact.main([str(src), "-o", str(tmp_path / "out.txt"), "--check"]) == 1


# --- compare --------------------------------------------------------------


def _transcript(expected_file: Path, extra: bool) -> str:
    lines = []
    for i, event in enumerate(expected_file.read_text().splitlines()):
        lines.append(f"2025-04-12 18:38:{i:02d}.000 {event}")
        if extra and i == 0:
            lines.append("2025-04-12 18:38:00.500 VPNClient: Connected")
    return "\n".join(lines) + "\n"


def _build_results(root: Path, *, extra_lines: bool = True) -> Path:
    (root / "model").mkdir(parents=True)
    (root / "analysis").mkdir(parents=True)
    (root / "transcripts").mkdir(parents=True)

    (root / "model" / "report.json").write_text(
        json.dumps(
            {
                "states": PAPER["states"],
                "transitions": PAPER["transitions"],
                "max_depth": PAPER["max_depth"],
                "invariants": [f"I{i}" for i in range(1, PAPER["invariants"] + 1)],
                "guards": [f"G{i}" for i in range(1, PAPER["guards"] + 1)],
                "violations": [],
                "seeded": {
                    "cases": PAPER["seeded_states"] + PAPER["seeded_transitions"],
                    "undetected": 0,
                    "rows": [
                        {"name": f"s{i}", "kind": "state", "expected_check": "I1",
                         "detected_by": ["I1"]}
                        for i in range(PAPER["seeded_states"])
                    ]
                    + [
                        {"name": f"t{i}", "kind": "transition", "expected_check": "G1",
                         "detected_by": ["G1"]}
                        for i in range(PAPER["seeded_transitions"])
                    ],
                },
            }
        )
    )

    with (root / "analysis" / "table9.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["pairs", "demand", "headroom", "endurance_h",
                                           "refill_min"])
        w.writeheader()
        for row in PAPER["table9"]:
            w.writerow({k: ("" if v is None else v) for k, v in row.items()})

    (root / "analysis" / "scalars.json").write_text(
        json.dumps(
            {
                "D": PAPER["demand_units_per_s"],
                "mQ": PAPER["ceiling_pairs"],
                "reserve_24h_1000_pct": PAPER["reserve_24h_1000_pct"],
                "reserve_24h_10000_pct": PAPER["reserve_24h_10000_pct"],
                "depletion_50000_h": PAPER["depletion_50000_h"],
                "refill_1h_1000_min": PAPER["refill_1h_1000_min"],
                "refill_1h_10000_min": PAPER["refill_1h_10000_min"],
                "refill_1h_50000_h": PAPER["refill_1h_50000_h"],
            }
        )
    )

    for name in ("s1", "s2"):
        (root / "transcripts" / f"{name}.txt").write_text(
            _transcript(EXPECTED_DIR / f"{name}_events.txt", extra_lines)
        )
    return root


def test_compare_passes_on_results_built_from_paper_numbers(tmp_path):
    checks = compare_mod.compare(_build_results(tmp_path / "results"), EXPECTED_DIR)

    assert checks
    assert [c for c in checks if c.result == "FAIL"] == []
    assert any(c.name.startswith("s1") for c in checks)


def test_compare_fails_when_a_model_count_is_off_by_one(tmp_path):
    root = _build_results(tmp_path / "results")
    report = json.loads((root / "model" / "report.json").read_text())
    report["states"] -= 1
    (root / "model" / "report.json").write_text(json.dumps(report))

    checks = compare_mod.compare(root, EXPECTED_DIR)

    failed = [c for c in checks if c.result == "FAIL"]
    assert [c.name for c in failed] == ["model.states"]
    assert "model-calibration" in failed[0].note


def test_compare_fails_when_a_table9_value_is_off(tmp_path):
    root = _build_results(tmp_path / "results")
    rows = list(csv.DictReader((root / "analysis" / "table9.csv").open()))
    rows[1]["headroom"] = str(float(rows[1]["headroom"]) + 1)
    with (root / "analysis" / "table9.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    failed = [c for c in compare_mod.compare(root, EXPECTED_DIR) if c.result == "FAIL"]

    assert [c.name for c in failed] == ["table9[1000].headroom"]


def test_compare_fails_and_names_the_first_missing_transcript_line(tmp_path):
    root = _build_results(tmp_path / "results")
    path = root / "transcripts" / "s2.txt"
    kept = [ln for ln in path.read_text().splitlines() if "Uploading file" not in ln]
    path.write_text("\n".join(kept) + "\n")

    failed = [c for c in compare_mod.compare(root, EXPECTED_DIR) if c.result == "FAIL"]

    assert [c.name for c in failed] == ["s2.VPNClient", "s2.sequence"]
    assert "VPNClient: Uploading file sensitive.txt" in failed[0].note


def test_compare_tolerates_extra_transcript_lines_but_not_reordering(tmp_path):
    root = _build_results(tmp_path / "results")
    path = root / "transcripts" / "s1.txt"
    lines = path.read_text().splitlines()
    # Both lines are VPNClient's, so this reorders one component's own log.
    assert "VPNClient" in lines[2] and "VPNClient" in lines[3]
    lines[2], lines[3] = lines[3], lines[2]
    path.write_text("\n".join(lines) + "\n")

    failed = [c for c in compare_mod.compare(root, EXPECTED_DIR) if c.result == "FAIL"]

    assert [c.name for c in failed] == ["s1.VPNClient", "s1.sequence"]


def test_compare_tolerates_reordering_across_components(tmp_path):
    """Two hosts, two clocks: only each component's own order is a claim.

    The paper's Figure 8 shows exactly this -- the client's "Getting key" line
    prints after the EKM response that it causally precedes.
    """
    root = _build_results(tmp_path / "results")
    path = root / "transcripts" / "s2.txt"
    lines = path.read_text().splitlines()
    client = next(i for i, ln in enumerate(lines) if "VPNClient: Getting key" in ln)
    lines.insert(0, lines.pop(client))  # ahead of the EKM lines it triggers
    path.write_text("\n".join(lines) + "\n")

    checks = compare_mod.compare(root, EXPECTED_DIR)

    assert [c for c in checks if c.result == "FAIL"] == []
    assert ("s2.FileUploadServer", "5 of 5 matched") in [
        (c.name, c.actual) for c in checks
    ]


def test_compare_only_restricts_the_check_groups(tmp_path):
    root = _build_results(tmp_path / "results")
    for name in ("s1", "s2"):
        (root / "transcripts" / f"{name}.txt").unlink()

    assert compare_mod.main([str(root), str(EXPECTED_DIR), "-o", str(tmp_path / "all.md")]) == 1

    out = tmp_path / "subset.md"
    rc = compare_mod.main(
        [str(root), str(EXPECTED_DIR), "--only", "model,analysis", "-o", str(out)]
    )

    assert rc == 0
    assert "s1.sequence" not in out.read_text()


def test_compare_cli_writes_markdown_and_json_and_exits_nonzero_on_fail(tmp_path):
    root = _build_results(tmp_path / "results")
    (root / "model" / "report.json").unlink()
    out = tmp_path / "COMPARISON.md"

    rc = compare_mod.main([str(root), str(EXPECTED_DIR), "-o", str(out)])

    assert rc == 1
    text = out.read_text()
    assert "| check | expected | actual | result | note |" in text
    assert "FAIL" in text
    payload = json.loads(out.with_suffix(".json").read_text())
    assert any(c["result"] == "FAIL" for c in payload)


def test_compare_cli_exits_zero_when_everything_matches(tmp_path):
    root = _build_results(tmp_path / "results")

    assert compare_mod.main([str(root), str(EXPECTED_DIR), "-o", str(tmp_path / "c.md")]) == 0


# --- expected/ fixtures ---------------------------------------------------


def test_expected_event_files_match_the_paper_figures():
    s1 = (EXPECTED_DIR / "s1_events.txt").read_text().splitlines()
    s2 = (EXPECTED_DIR / "s2_events.txt").read_text().splitlines()

    assert len(s1) == 15
    assert s1[0] == "VPNClient: Client Started"
    assert s1[-1] == "VPNClient: Configuring the Wireguard VPN Interface"
    assert len(s2) == 12
    assert s2[0] == "EKM: Got new key request for instance <PEER_B>"
    assert s2[-1] == "VPNClient: File sensitive.txt uploaded successfully"


def test_paper_capture_carries_the_table4_values():
    capture = json.loads((EXPECTED_DIR / "qkd_capture_paper.json").read_text())

    assert capture["secure_bit_rate"] == 21220
    assert capture["secure_key_rate_256"] == 82
    assert capture["derived_256bit_rate"] == pytest.approx(21220 / 256)
    assert capture["signal_qber_per_state"] == [0.19, 1.04, 1.45, 0.27, 0.83, 0.69]
    assert capture["backend"] == "heqa"
    assert capture["source_label_secure_key_rate"] == PAPER["source_label_secure_key_rate"]


# --- capture_qkd against the in-process simulator -------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sim_server(monkeypatch):
    from qkd_ekm.qkdsim.app import create_app
    from qkd_ekm.qkdsim.store import KeyStore

    monkeypatch.delenv("SIM_TOKEN", raising=False)
    monkeypatch.setenv("SIM_SAES", "QKD1,QKD2")
    port = _free_port()
    config = uvicorn.Config(
        create_app(KeyStore(seed=11)), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_capture_qkd_against_simulator_writes_table4_json(sim_server, tmp_path):
    out = tmp_path / "qkd_capture.json"

    rc = capture_qkd.main(
        ["--backend", "sim", "--url", sim_server, "--user", "admin",
         "--password", "admin", "-o", str(out)]
    )

    assert rc == 0
    data = json.loads(out.read_text())
    assert data["secure_bit_rate"] == pytest.approx(21220.0)
    assert data["derived_256bit_rate"] == pytest.approx(21220 / 256)
    assert data["signal_qber"] == pytest.approx(0.80)
    assert data["available_256bit_keys"] is not None
    assert data["backend"] == "sim"
    assert data["source_label_secure_key_rate"] == PAPER["source_label_secure_key_rate"]
    assert data["captured_at"].startswith("20")


# --- source literals vs published transcripts -------------------------------
#
# The expected/*.txt fixtures are the paper's figures, transcribed by hand. These
# two tests tie them back to the strings the services actually log: every published
# line must appear, in order, among the redacted literals of its component.


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(line in it for line in needle)


def _component_lines(name: str, component: str) -> list[str]:
    return [
        line
        for line in (EXPECTED_DIR / name).read_text().splitlines()
        if line.startswith(f"{component}: ")
    ]


def test_s1_vpnclient_transcript_comes_from_the_client_cli_literals():
    from test_client import CONNECT_LINES

    logged = [
        redact.redact_line(f"VPNClient: {line.format(key_id=UUID)}") for line in CONNECT_LINES
    ]

    assert _is_subsequence(_component_lines("s1_events.txt", "VPNClient"), logged)


#: The upload-path log literals of each component, in the order the code emits them
#: (qkd_ekm.ekm.app, qkd_ekm.upload.app, qkd_ekm.client.cli), with sample values.
S2_LITERALS = {
    "EKM": [
        "Got new key request for instance QKD2",
        "Retrieving 1 keys between QKD1 and QKD2 from pool",
        "Got Key Wrap request with KEKId: v1",
    ],
    "FileUploadServer": [
        f"received key with Id {UUID} for qkds: QKD2<-->QKD1",
        f"retrieving key with id: {UUID}",
        f"uploading file to gs://qkd-ekm-data-abc123/{UUID}_sensitive.txt",
        "using encryption key: projects/p/locations/me-west1/keyRings/r/cryptoKeys/external",
        "file uploaded successfully",
    ],
    "VPNClient": [
        "Getting key for file encryption shared with QKD1",
        f"Encrypting the file with key: {UUID}",
        "Uploading file sensitive.txt",
        "File sensitive.txt uploaded successfully",
    ],
}


@pytest.mark.parametrize("component", sorted(S2_LITERALS))
def test_s2_transcript_comes_from_the_service_literals(component):
    logged = [redact.redact_line(f"{component}: {line}") for line in S2_LITERALS[component]]

    assert _is_subsequence(_component_lines("s2_events.txt", component), logged)
