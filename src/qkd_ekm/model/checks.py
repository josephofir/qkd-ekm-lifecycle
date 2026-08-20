"""State properties I1-I9 (Table 5) and transition guards G1-G4.

`invariants(s)` names every violated state property, `guards(label, s, s2)`
names every violated transition guard. Both are deliberately written against
the state fields only -- never against the transition generator -- so that the
seeded sensitivity cases exercise them independently.
"""

from __future__ import annotations

from .state import MAX_EPOCH, State, consequences, mode

INVARIANTS = {
    "I1": "Available identifiers remain unique throughout every temporary pool transition.",
    "I2": "Every consumed identifier appears within at most one persistent purpose binding.",
    "I3": "A consumed identifier never re-enters the available allocation pool.",
    "I4": "Every purpose-and-object pair resolves toward at most one consumed identifier.",
    "I5": "Every active EKM version resolves toward its matching persistent binding.",
    "I6": "Every pending VPN activation resolves toward a matching fresh purpose binding.",
    "I7": "Every active VPN state matches its binding and authoritative activation epoch.",
    "I8": "Every modeled condition resolves toward one derived lifecycle mode.",
    "I9": "Storage, fresh allocation, and established VPN indicators match dependencies.",
}

GUARDS = {
    "G1": "Ingestion requires an available QKD source.",
    "G2": "Fresh allocation requires EKM reachability and retained inventory.",
    "G3": "Stale VPN rejection preserves authoritative lifecycle state.",
    "G4": "Recovery reconciliation requires restored dependencies and pending recovery.",
}

MODES = ("READY", "BUFFERED", "BINDING_HOLDOVER", "EXHAUSTED", "RECOVERY", "SUSPENDED")


def invariants(s: State, *, retained_epoch: bool = False) -> list[str]:
    """Names of the state properties violated by `s` (empty when consistent).

    `retained_epoch` relaxes I7 to the reading in which E survives the end of
    an established session (see docs/model-calibration.md, "Round 2").
    """
    bad: list[str] = []
    pool = list(s.pool)
    bindings = list(s.bindings)
    bound_ids = [i for (i, _p, _o) in bindings]

    # I1: `pool` is a `frozenset[str]` (see state.py), so `len(pool) !=
    # len(set(pool))` can never hold for real states -- the type makes I1
    # structurally true across the reachable closure. It stays checkable via
    # a plain list/tuple `pool` (seeded states, or a future list-shaped pool)
    # and via the FIFO `order` tuple, which is a real tuple and can carry
    # duplicates or drift from `pool`.
    if len(pool) != len(set(pool)) or (s.order and sorted(s.order) != sorted(set(pool))):
        bad.append("I1")
    if len(bound_ids) != len(set(bound_ids)):
        bad.append("I2")
    if set(pool) & set(bound_ids) or set(s.expired) & (set(pool) | set(bound_ids)):
        bad.append("I3")
    pairs = [(p, o) for (_i, p, o) in bindings]
    if len(pairs) != len(set(pairs)):
        bad.append("I4")
    if s.ke is not None and not any(p == "ekm" and o == s.ke for (_i, p, o) in bindings):
        bad.append("I5")
    if s.kp is not None:
        k, e = s.kp
        if (k, "vpn", str(e)) not in s.bindings or not s.epoch < e <= MAX_EPOCH:
            bad.append("I6")
    if s.kv is not None:
        k, e = s.kv
        if (k, "vpn", str(e)) not in s.bindings or e != s.epoch:
            bad.append("I7")
    elif s.epoch != 0 and not retained_epoch:
        bad.append("I7")

    # I8: every state resolves to exactly one of the six derived modes -- a
    # totality check that runs on every state, not only seeded ones. Because
    # `mode()` routes through the shared `derive()` (see state.py), this side
    # of the check is structurally true across the reachable closure; seeded
    # states additionally carry a *stored* mode that must agree with it.
    derived_mode = mode(s)
    stored_mode = getattr(s, "stored_mode", None)
    if derived_mode not in MODES or (stored_mode is not None and stored_mode != derived_mode):
        bad.append("I8")

    # I9: storage/fresh_allocation/established_vpn are present, correctly
    # typed, and consistent with their dependencies (Table 5: storage tracks
    # EKM reachability) for every state; seeded states additionally carry a
    # *stored* consequence vector that must agree with the derived one.
    c = consequences(s)
    consistent = (
        set(c) == {"storage", "fresh_allocation", "established_vpn"}
        and isinstance(c["storage"], bool)
        and isinstance(c["fresh_allocation"], bool)
        and c["established_vpn"] == "distinct"
        and c["storage"] == s.se
    )
    stored = getattr(s, "stored_consequences", None)
    if not consistent or (stored is not None and dict(stored) != c):
        bad.append("I9")
    return bad


def guards(label: str, s: State, s2: State) -> list[str]:
    """Names of the transition guards violated by `s --label--> s2`."""
    bad: list[str] = []
    family, _, arg = label.partition(":")
    if family == "ingest" and not s.sq:
        bad.append("G1")
    if family in ("alloc_ekm", "alloc_vpn"):
        identifier = arg.split(":")[0]
        if not s.se or not s.pool or identifier not in s.pool:
            bad.append("G2")
    if family == "reject_stale" and s2 != s:
        bad.append("G3")
    if family == "recover" and not (s.sq and s.se and s.recovery):
        bad.append("G4")
    return bad
