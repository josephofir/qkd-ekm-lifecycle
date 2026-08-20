"""Lifecycle control state X = (SQ, SE, B, M, KE, KP, KV, E, C, R)."""

from __future__ import annotations

from dataclasses import dataclass, field

from qkd_ekm.ekm.lifecycle import derive

IDS = ("k1", "k2", "k3", "k4")
VERSIONS = ("v1", "v2")
MAX_EPOCH = 3


@dataclass(frozen=True, slots=True)
class State:
    """One lifecycle control state.

    `bindings` holds persistent `(identifier, purpose, object)` triples: an
    EKM purpose binds an identifier to an external key version, a VPN purpose
    binds it to an activation epoch (as a string object).
    """

    sq: bool = True
    se: bool = True
    pool: frozenset[str] = field(default_factory=frozenset)
    bindings: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)
    ke: str | None = None
    kp: tuple[str, int] | None = None
    kv: tuple[str, int] | None = None
    epoch: int = 0
    authority: bool = True
    recovery: bool = False
    # Calibration-only extensions, all inert at their defaults (see
    # docs/model-calibration.md): identifiers retired by TTL expiry, the
    # explicit policy-suspension record, the "restoration detected" recovery
    # sub-state, and the allocation order behind a FIFO pool.
    expired: frozenset[str] = field(default_factory=frozenset)
    suspended: bool = False
    detected: bool = False
    order: tuple[str, ...] = ()


def initial() -> State:
    return State()


def consumed(s: State) -> set[str]:
    """Identifiers that carry a persistent binding and can never be re-ingested."""
    return {i for (i, _p, _o) in s.bindings}


def _derived(s: State) -> dict:
    return derive(
        s.sq,
        s.se,
        len(s.pool),
        s.ke is not None or s.kv is not None,
        s.authority,
        s.recovery or s.detected,
        policy_suspended=s.suspended,
    )


def mode(s: State) -> str:
    """L = f(X), via the same derivation the running EKM uses."""
    return _derived(s)["mode"]


def consequences(s: State) -> dict:
    """Y = g(X, gamma): managed storage, fresh allocation, established VPN."""
    d = _derived(s)
    return {
        "storage": d["storage"],
        "fresh_allocation": d["fresh_allocation"],
        "established_vpn": d["established_vpn"],
    }
