"""Tests for the executable lifecycle reference model (paper section 5)."""

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qkd_ekm.model import checks, events, seeded, state
from qkd_ekm.model import enumerate as enum_mod
from qkd_ekm.model.__main__ import build_parser, main
from qkd_ekm.model.state import IDS, MAX_EPOCH, VERSIONS, State

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((REPO_ROOT / "expected" / "paper_numbers.json").read_text())


def labels(s: State) -> set[str]:
    return {label for label, _ in events.enabled(s)}


def family_labels(s: State, family: str) -> set[str]:
    return {label for label in labels(s) if label.split(":")[0] == family}


def step(s: State, label: str) -> State:
    for got, nxt in events.enabled(s):
        if got == label:
            return nxt
    raise AssertionError(f"{label} not enabled in {s}")


# --- state -------------------------------------------------------------


def test_initial_is_ready_with_empty_inventory():
    s = state.initial()
    assert (s.sq, s.se, s.authority, s.recovery) == (True, True, True, False)
    assert s.pool == frozenset() and s.bindings == frozenset()
    assert (s.ke, s.kp, s.kv, s.epoch) == (None, None, None, 0)
    assert state.mode(s) == "READY"


def test_domains_match_paper_abstraction():
    assert IDS == ("k1", "k2", "k3", "k4")
    assert VERSIONS == ("v1", "v2")
    assert MAX_EPOCH == 3


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, "READY"),
        ({"sq": False, "pool": frozenset({"k1"})}, "BUFFERED"),
        ({"sq": False, "ke": "v1", "bindings": frozenset({("k1", "ekm", "v1")})}, "BINDING_HOLDOVER"),
        ({"sq": False}, "EXHAUSTED"),
        ({"sq": False, "authority": False}, "SUSPENDED"),
        ({"recovery": True}, "RECOVERY"),
    ],
)
def test_mode_follows_shared_derivation(kwargs, expected):
    assert state.mode(replace(state.initial(), **kwargs)) == expected


def test_consequences_follow_shared_derivation():
    s = replace(state.initial(), se=False)
    c = state.consequences(s)
    assert c["storage"] is False and c["fresh_allocation"] is False


# --- events ------------------------------------------------------------


def test_ingest_enabled_for_every_unused_identifier():
    assert labels(state.initial()) >= {f"ingest:{k}" for k in IDS}


def test_ingest_adds_to_pool():
    assert step(state.initial(), "ingest:k1").pool == frozenset({"k1"})


def test_ingest_blocked_while_source_unavailable():
    s = replace(state.initial(), sq=False)
    assert not any(label.startswith("ingest") for label in labels(s))


def test_ingest_blocked_for_consumed_identifier():
    s = replace(state.initial(), bindings=frozenset({("k1", "ekm", "v1")}), ke="v1")
    assert "ingest:k1" not in labels(s)
    assert "ingest:k2" in labels(s)


def test_expire_removes_only_pooled_identifiers():
    s = replace(state.initial(), pool=frozenset({"k1"}))
    assert "expire:k1" in labels(s) and "expire:k2" not in labels(s)
    assert step(s, "expire:k1").pool == frozenset()


def test_alloc_ekm_consumes_identifier_and_binds_version():
    s = replace(state.initial(), pool=frozenset({"k1"}))
    nxt = step(s, "alloc_ekm:k1:v1")
    assert nxt.pool == frozenset()
    assert nxt.bindings == frozenset({("k1", "ekm", "v1")})
    assert nxt.ke == "v1"


def test_alloc_ekm_blocked_when_ekm_unreachable_or_pool_empty():
    assert not any(label.startswith("alloc_") for label in labels(state.initial()))
    s = replace(state.initial(), se=False, pool=frozenset({"k1"}))
    assert not any(label.startswith("alloc_") for label in labels(s))


def test_alloc_ekm_replacement_uses_the_other_version():
    s = replace(
        state.initial(),
        pool=frozenset({"k2"}),
        bindings=frozenset({("k1", "ekm", "v1")}),
        ke="v1",
    )
    assert "alloc_ekm:k2:v2" in labels(s)
    assert "alloc_ekm:k2:v1" not in labels(s)


def test_alloc_vpn_creates_pending_activation_for_the_bound_epoch():
    s = replace(state.initial(), pool=frozenset({"k1"}))
    nxt = step(s, "alloc_vpn:k1:1")
    assert nxt.kp == ("k1", 1)
    assert nxt.bindings == frozenset({("k1", "vpn", "1")})
    assert nxt.kv is None and nxt.epoch == 0


def test_alloc_vpn_offers_every_ordered_future_epoch():
    # The calibrated reading binds any not-yet-used ordered activation epoch,
    # not only the immediately next one -- see docs/model-calibration.md.
    s = replace(state.initial(), pool=frozenset({"k1"}))
    assert family_labels(s, "alloc_vpn") == {
        "alloc_vpn:k1:1",
        "alloc_vpn:k1:2",
        "alloc_vpn:k1:3",
    }
    assert step(s, "alloc_vpn:k1:3").kp == ("k1", 3)


def test_alloc_vpn_blocked_while_activation_pending():
    s = replace(
        state.initial(),
        pool=frozenset({"k2"}),
        bindings=frozenset({("k1", "vpn", "1")}),
        kp=("k1", 1),
    )
    assert family_labels(s, "alloc_vpn") == set()


def test_alloc_vpn_blocked_beyond_final_epoch():
    s = replace(
        state.initial(),
        pool=frozenset({"k4"}),
        bindings=frozenset(
            {("k1", "vpn", "1"), ("k2", "vpn", "2"), ("k3", "vpn", "3")}
        ),
        kv=("k3", 3),
        epoch=3,
    )
    assert family_labels(s, "alloc_vpn") == set()


def test_activate_vpn_promotes_pending_binding():
    s = replace(
        state.initial(), bindings=frozenset({("k1", "vpn", "1")}), kp=("k1", 1)
    )
    nxt = step(s, "activate_vpn")
    assert nxt.kv == ("k1", 1) and nxt.epoch == 1 and nxt.kp is None


def test_reject_stale_is_a_self_loop_for_every_replayable_epoch():
    s = replace(
        state.initial(),
        bindings=frozenset({("k1", "vpn", "1"), ("k2", "vpn", "2")}),
        kv=("k2", 2),
        epoch=2,
    )
    assert family_labels(s, "reject_stale") == {"reject_stale:1", "reject_stale:2"}
    assert step(s, "reject_stale:2") == s
    assert family_labels(state.initial(), "reject_stale") == set()


def test_dependency_interruption_and_restoration_sets_recovery():
    s = step(state.initial(), "src_down")
    assert s.sq is False and state.mode(s) == "EXHAUSTED"
    s2 = step(s, "src_up")
    assert s2.sq is True and s2.recovery is True and state.mode(s2) == "RECOVERY"


def test_ekm_interruption_and_restoration_sets_recovery():
    s = step(state.initial(), "ekm_down")
    assert s.se is False
    assert step(s, "ekm_up").recovery is True


def test_authority_toggles():
    s = step(state.initial(), "authority_withdraw")
    assert s.authority is False
    assert step(s, "authority_grant").authority is True


def test_recover_requires_restored_dependencies_and_pending_recovery():
    s = replace(state.initial(), recovery=True)
    assert step(s, "recover").recovery is False
    assert "recover" not in labels(state.initial())
    assert "recover" not in labels(replace(s, sq=False))
    assert "recover" not in labels(replace(s, se=False))


def test_every_event_family_from_the_paper_is_modelled():
    families = set()
    for s in (
        state.initial(),
        replace(state.initial(), pool=frozenset(IDS[:2])),
        replace(state.initial(), sq=False, se=False, recovery=True, authority=False),
        replace(
            state.initial(),
            bindings=frozenset({("k1", "vpn", "1")}),
            kv=("k1", 1),
            epoch=1,
        ),
        replace(
            state.initial(),
            bindings=frozenset({("k1", "vpn", "1")}),
            kp=("k1", 1),
            recovery=True,
        ),
    ):
        families |= {label.split(":")[0] for label, _ in events.enabled(s)}
    assert families == {
        "ingest",
        "expire",
        "alloc_ekm",
        "alloc_vpn",
        "activate_vpn",
        "reject_stale",
        "src_down",
        "src_up",
        "ekm_down",
        "ekm_up",
        "authority_withdraw",
        "authority_grant",
        "recover",
    }


# --- invariants --------------------------------------------------------


def test_invariants_hold_for_initial_state():
    assert checks.invariants(state.initial()) == []


def test_invariant_count_matches_paper():
    assert len(checks.INVARIANTS) == EXPECTED["invariants"]
    assert len(checks.GUARDS) == EXPECTED["guards"]


@pytest.mark.parametrize("case", seeded.cases(), ids=lambda c: c.name)
def test_every_seeded_case_is_detected_by_its_expected_check(case):
    detected = seeded.detect(case)
    assert case.expected_check in detected, f"{case.name}: {detected}"


def test_seeded_cases_unpack_as_name_subject_check():
    name, subject, check = seeded.cases()[0][:3]
    assert (name, check) == ("duplicate_pool_entry", "I1")
    assert subject.pool == ("k1", "k1")


def test_seeded_case_counts_match_paper():
    kinds = [c.kind for c in seeded.cases()]
    assert kinds.count("state") == EXPECTED["seeded_states"]
    assert kinds.count("transition") == EXPECTED["seeded_transitions"]


def test_run_seeded_reports_a_detection_for_every_case():
    rows = seeded.run_seeded()
    assert len(rows) == EXPECTED["seeded_states"] + EXPECTED["seeded_transitions"]
    assert all(row["detected_by"] for row in rows)


def test_guards_accept_valid_transitions():
    s = replace(state.initial(), pool=frozenset({"k1"}))
    for label, nxt in events.enabled(s):
        assert checks.guards(label, s, nxt) == []


# --- enumeration -------------------------------------------------------


@pytest.fixture(scope="module")
def result():
    return enum_mod.run()


def test_enumeration_has_no_violations(result):
    assert result.violations == []


def test_i8_is_a_totality_check_across_the_reachable_closure(result):
    # I8 must resolve every state to exactly one derived mode, not only
    # states that happen to carry a stored mode (seeded states).
    for s in result.states:
        assert state.mode(s) in checks.MODES


def test_i9_is_a_totality_and_consistency_check_across_the_reachable_closure(result):
    # I9 must hold for every state: the three consequence keys are present,
    # correctly typed, and storage tracks EKM reachability (Table 5) -- not
    # only for states that happen to carry a stored consequence vector
    # (seeded states).
    for s in result.states:
        c = state.consequences(s)
        assert set(c) == {"storage", "fresh_allocation", "established_vpn"}
        assert isinstance(c["storage"], bool)
        assert isinstance(c["fresh_allocation"], bool)
        assert c["established_vpn"] == "distinct"
        assert c["storage"] == s.se


def test_enumeration_contains_the_initial_state_at_depth_zero(result):
    assert result.states[state.initial()] == 0


def test_enumeration_transitions_are_unique_labeled_edges(result):
    assert len(result.transitions) == len(set(result.transitions))


def test_enumeration_includes_reject_stale_self_loops(result):
    assert any(
        label.startswith("reject_stale") and src == dst
        for src, label, dst in result.transitions
    )


def test_enumeration_reaches_every_lifecycle_mode(result):
    modes = {state.mode(s) for s in result.states}
    assert modes == {
        "READY",
        "BUFFERED",
        "BINDING_HOLDOVER",
        "EXHAUSTED",
        "SUSPENDED",
        "RECOVERY",
    }


def test_enumeration_max_depth_matches_paper(result):
    assert result.max_depth == EXPECTED["max_depth"]


# Achieved by the calibrated rule set, and equal to the published triple;
# asserted separately so that a semantic change to the model cannot pass
# unnoticed even if `expected/paper_numbers.json` were edited. See
# docs/model-calibration.md.
ACHIEVED = (45_824, 307_680, 16)


def test_enumeration_counts_are_stable(result):
    assert (len(result.states), len(result.transitions), result.max_depth) == ACHIEVED


def test_report_rules_reflect_the_run_not_just_the_default(tmp_path):
    # A calibration-style run with a non-default reading must have its own
    # rules echoed back by write()/summary(), not the package DEFAULT_RULES.
    custom = events.Rules(ingest_needs_ekm=True)
    custom_result = enum_mod.run(custom)
    report = enum_mod.write(custom_result, tmp_path)
    assert report["rules"]["ingest_needs_ekm"] is True
    assert report["rules"] == {f: getattr(custom, f) for f in custom.__slots__}


def test_calibrated_rules_are_the_reading_that_reproduces_the_paper(result):
    assert events.DEFAULT_RULES == events.Rules(
        alloc_vpn_any_future_epoch=True, reject_stale_per_epoch=True
    )


def test_enumeration_counts_match_paper(result):
    assert (len(result.states), len(result.transitions), result.max_depth) == (
        EXPECTED["states"],
        EXPECTED["transitions"],
        EXPECTED["max_depth"],
    )


# --- CLI ---------------------------------------------------------------


def test_cli_without_a_subcommand_runs_everything():
    # `make model` invokes the bare command.
    args = build_parser().parse_args([])
    assert (args.command, args.out) == ("all", "results/model")


def test_cli_seeded_only_writes_the_seeded_artifacts(tmp_path):
    assert main(["seeded", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "seeded.csv").is_file()
    assert not (tmp_path / "states.csv").exists()


def test_cli_all_writes_the_reproducibility_artifacts(tmp_path, capsys):
    assert main(["all", "-o", str(tmp_path)]) == 0
    for name in ("states.csv", "transitions.csv", "seeded.csv", "report.json", "report.md"):
        assert (tmp_path / name).is_file(), name
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["states"] == len(list(csv.DictReader((tmp_path / "states.csv").open())))
    assert report["violations"] == []
    assert report["seeded"]["undetected"] == 0
    assert "mode" in csv.DictReader((tmp_path / "states.csv").open()).fieldnames
