"""Seeded sensitivity analysis (Table 7).

Nine deliberately inconsistent states and four invalid transitions, each
constructed by hand -- never by the valid transition generator -- so that a
detection proves the checking logic works independently of `events.enabled`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

from . import checks
from .state import State


@dataclass(frozen=True, slots=True)
class SeededState(State):
    """A state that also carries a *stored* mode / consequence vector.

    Real states derive both, so I8/I9 only exercise their totality and
    consistency clauses there; the seeded cases store a stale value so the
    derivation assertions have something to catch as well.
    """

    stored_mode: str | None = None
    stored_consequences: tuple[tuple[str, object], ...] | None = None


class Case(NamedTuple):
    """`(name, subject, expected_check)` plus the kind of subject.

    `subject` is either a seeded state or a seeded `(label, src, dst)` triple.
    """

    name: str
    subject: object
    expected_check: str
    kind: str  # "state" or "transition"


def cases() -> list[Case]:
    """The nine seeded invalid states and four seeded invalid transitions."""
    base = State()
    active_vpn = replace(
        base, bindings=frozenset({("k1", "vpn", "1")}), kv=("k1", 1), epoch=1
    )
    return [
        Case(
            "duplicate_pool_entry",
            replace(base, pool=("k1", "k1")),
            "I1",
            "state",
        ),
        Case(
            "identifier_in_two_bindings",
            replace(
                base,
                bindings=frozenset({("k1", "ekm", "v1"), ("k1", "vpn", "1")}),
                ke="v1",
                kv=("k1", 1),
                epoch=1,
            ),
            "I2",
            "state",
        ),
        Case(
            "consumed_identifier_back_in_pool",
            replace(
                base,
                pool=frozenset({"k1"}),
                bindings=frozenset({("k1", "ekm", "v1")}),
                ke="v1",
            ),
            "I3",
            "state",
        ),
        Case(
            "two_identifiers_same_purpose_object",
            replace(
                base,
                bindings=frozenset({("k1", "ekm", "v1"), ("k2", "ekm", "v1")}),
                ke="v1",
            ),
            "I4",
            "state",
        ),
        Case(
            "active_version_without_binding",
            replace(base, ke="v1"),
            "I5",
            "state",
        ),
        Case(
            "pending_activation_with_replayed_epoch",
            replace(active_vpn, kp=("k1", 1)),
            "I6",
            "state",
        ),
        Case(
            "active_vpn_epoch_conflicts_with_authoritative_epoch",
            replace(active_vpn, epoch=2),
            "I7",
            "state",
        ),
        Case(
            "stored_mode_conflicts_with_derived_mode",
            SeededState(sq=False, stored_mode="READY"),
            "I8",
            "state",
        ),
        Case(
            "stored_consequences_conflict_with_dependencies",
            SeededState(
                se=False,
                stored_consequences=(
                    ("storage", True),
                    ("fresh_allocation", True),
                    ("established_vpn", "distinct"),
                ),
            ),
            "I9",
            "state",
        ),
        Case(
            "ingest_while_source_unavailable",
            (
                "ingest:k1",
                replace(base, sq=False),
                replace(base, sq=False, pool=frozenset({"k1"})),
            ),
            "G1",
            "transition",
        ),
        Case(
            "alloc_ekm_while_ekm_unreachable",
            (
                "alloc_ekm:k1:v1",
                replace(base, se=False, pool=frozenset({"k1"})),
                replace(
                    base,
                    se=False,
                    bindings=frozenset({("k1", "ekm", "v1")}),
                    ke="v1",
                ),
            ),
            "G2",
            "transition",
        ),
        Case(
            "reject_stale_changes_authoritative_state",
            ("reject_stale", active_vpn, replace(active_vpn, epoch=2)),
            "G3",
            "transition",
        ),
        Case(
            "recover_without_restored_dependencies",
            (
                "recover",
                replace(base, sq=False, recovery=False),
                replace(base, sq=False, recovery=False),
            ),
            "G4",
            "transition",
        ),
    ]


def detect(case: Case) -> list[str]:
    """Checks that fire for one seeded case."""
    if case.kind == "state":
        return checks.invariants(case.subject)
    label, src, dst = case.subject
    return checks.guards(label, src, dst)


def run_seeded() -> list[dict]:
    """Run every seeded case; each row records which checks caught it."""
    return [
        {
            "name": c.name,
            "kind": c.kind,
            "expected_check": c.expected_check,
            "detected_by": detect(c),
        }
        for c in cases()
    ]
