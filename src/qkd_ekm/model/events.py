"""Labeled lifecycle events (Figure 3 T1-T12, Algorithm 1).

`enabled(s)` returns every `(label, next_state)` pair enabled in `s`. The
semantics are parameterised by `Rules` so that `scripts/model_calibrate.py`
can sweep the plausible readings of the paper's prose against the published
enumeration counts; `DEFAULT_RULES` is the calibrated reading -- the one that
reproduces all three published numbers -- and is what the package, the CLI,
and the tests use. See docs/model-calibration.md.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .state import IDS, MAX_EPOCH, VERSIONS, State, consumed, mode


@dataclass(frozen=True, slots=True)
class Rules:
    """Semantic knobs.

    The dataclass defaults are the *base* reading -- the literal first-pass
    transcription of section 5.3 -- which `scripts/model_calibrate.py` uses as
    the origin of its sweeps. `DEFAULT_RULES` below is the calibrated reading
    that reproduces the published enumeration, and is what the package, the
    CLI, and the tests use.
    """

    # `reject_stale` is enabled once a session is active, or (alternatively)
    # whenever any authoritative epoch exists.
    reject_stale_needs_active: bool = True
    # Ingestion needs EKM reachability in addition to source availability.
    ingest_needs_ekm: bool = False
    # TTL expiry only runs while the source is interrupted.
    expire_needs_source_down: bool = False
    # Fresh EKM allocation needs source availability in addition to EKM reach.
    alloc_ekm_needs_source: bool = False
    # Restoration (`src_up`/`ekm_up`) marks recovery reconciliation pending...
    restore_sets_recovery: bool = True
    # ...only once both dependencies are back (rather than on every restore).
    restore_recovery_needs_full: bool = False
    # Continuity authority may only change outside ready operation.
    authority_only_when_degraded: bool = False
    # A fresh VPN allocation requires that no session is currently active.
    alloc_vpn_needs_no_active: bool = False
    # A pending VPN activation may be superseded by a later allocation.
    vpn_pending_replaceable: bool = False
    # Recovery reconciliation discards an in-flight pending activation.
    recover_clears_pending: bool = False
    # Recovery reconciliation requires continuity authority.
    recover_needs_authority: bool = False
    # Dependency/authority events stay enabled even when they change nothing.
    dep_toggles_unconditional: bool = False
    # Allocation activates immediately (no separate `activate_vpn` event).
    activate_merged: bool = False
    # Losing EKM reachability drops the active version (no binding holdover).
    ekm_down_clears_active: bool = False

    # --- round-2 knobs (docs/model-calibration.md, "Round 2") ---------------
    # TTL expiry retires an identifier for good: it can never be re-ingested.
    expire_marks_used: bool = False
    # Withdrawing continuity authority from holdover/exhaustion records an
    # explicit policy-suspension state (T5/T6) that only recovery clears.
    suspend_state: bool = False
    # Restoration detection (T7-T10) is its own recovery sub-state, which an
    # explicit `reconcile` event promotes to reconciliation before T11.
    restoration_detected: bool = False
    # A pending activation can be abandoned explicitly (binding persists).
    abandon_pending: bool = False
    # ...and abandoning consumes its epoch (the authoritative epoch advances).
    abandon_consumes_epoch: bool = False
    # Dependency interruption discards an in-flight pending activation.
    down_clears_pending: bool = False
    # An established session can end explicitly, retaining the epoch history.
    session_end: bool = False
    # Recovery reconciliation ends the established session (epoch retained).
    recover_clears_active: bool = False
    # The temporary pool is an ordered queue: expiry and allocation take the
    # oldest retained identifier.
    fifo_pool: bool = False
    # A VPN allocation may target any future epoch, not only the next one.
    alloc_vpn_any_future_epoch: bool = False
    # Each replayable epoch is its own labeled rejection event.
    reject_stale_per_epoch: bool = False


# The calibrated reading: with these two knobs -- and only with them -- the
# exhaustive enumeration reproduces the published 45,824 states, 307,680
# labeled transitions, and maximum shortest-path depth of sixteen exactly.
# See docs/model-calibration.md, "Round 2".
DEFAULT_RULES = Rules(alloc_vpn_any_future_epoch=True, reject_stale_per_epoch=True)


def _vpn_objects(s: State) -> set[int]:
    return {int(o) for (_i, p, o) in s.bindings if p == "vpn"}


def _ekm_objects(s: State) -> set[str]:
    return {o for (_i, p, o) in s.bindings if p == "ekm"}


def _pool_add(s: State, k: str, rules: Rules) -> dict:
    if rules.fifo_pool:
        return {"pool": s.pool | {k}, "order": (*s.order, k)}
    return {"pool": s.pool | {k}}


def _pool_take(s: State, k: str, rules: Rules) -> dict:
    if rules.fifo_pool:
        return {"pool": s.pool - {k}, "order": tuple(x for x in s.order if x != k)}
    return {"pool": s.pool - {k}}


def _retained(s: State, rules: Rules) -> list[str]:
    """Identifiers eligible for expiry or allocation, oldest first under FIFO."""
    return list(s.order[:1]) if rules.fifo_pool else sorted(s.pool)


def enabled(s: State, rules: Rules = DEFAULT_RULES) -> list[tuple[str, State]]:
    """Every labeled transition enabled in `s`, as `(label, next_state)`."""
    out: list[tuple[str, State]] = []
    pool = s.pool
    gone = consumed(s)

    # Ingestion: available source plus a previously unused identifier.
    if s.sq and (s.se or not rules.ingest_needs_ekm):
        for k in IDS:
            if k not in pool and k not in gone and k not in s.expired:
                out.append((f"ingest:{k}", replace(s, **_pool_add(s, k, rules))))

    # TTL expiry of unused inventory (consumed bindings are untouched).
    if not rules.expire_needs_source_down or not s.sq:
        for k in _retained(s, rules):
            dropped = replace(s, **_pool_take(s, k, rules))
            if rules.expire_marks_used:
                dropped = replace(dropped, expired=s.expired | {k})
            out.append((f"expire:{k}", dropped))

    # Fresh EKM allocation: EKM reachability plus retained inventory.
    if s.se and (s.sq or not rules.alloc_ekm_needs_source):
        used = _ekm_objects(s)
        for k in _retained(s, rules):
            for v in VERSIONS:
                if v != s.ke and v not in used:
                    out.append(
                        (
                            f"alloc_ekm:{k}:{v}",
                            replace(
                                s,
                                **_pool_take(s, k, rules),
                                bindings=s.bindings | {(k, "ekm", v)},
                                ke=v,
                            ),
                        )
                    )

    # Fresh VPN allocation: an unused ordered activation epoch (the next one, or
    # under the calibrated reading any later one), bounded by MAX_EPOCH.
    nxt = max(max(_vpn_objects(s), default=0), s.epoch) + 1
    pending_ok = s.kp is None or rules.vpn_pending_replaceable
    active_ok = s.kv is None or not rules.alloc_vpn_needs_no_active
    epochs = range(nxt, MAX_EPOCH + 1) if rules.alloc_vpn_any_future_epoch else (nxt,)
    if s.se and nxt <= MAX_EPOCH and pending_ok and active_ok:
        for k in _retained(s, rules):
            taken = _pool_take(s, k, rules)
            for e in epochs:
                bound = s.bindings | {(k, "vpn", str(e))}
                if rules.activate_merged:
                    nxt_state = replace(
                        s, **taken, bindings=bound, kp=None, kv=(k, e), epoch=e
                    )
                else:
                    nxt_state = replace(s, **taken, bindings=bound, kp=(k, e))
                epoch_tag = f":{e}" if rules.alloc_vpn_any_future_epoch else ""
                out.append((f"alloc_vpn:{k}{epoch_tag}", nxt_state))

    # Coordinated activation of a pending binding with an increasing epoch.
    if not rules.activate_merged and s.kp is not None and s.kp[1] > s.epoch:
        out.append(
            ("activate_vpn", replace(s, kv=s.kp, epoch=s.kp[1], kp=None))
        )

    # A pending activation is abandoned; its purpose binding persists.
    if rules.abandon_pending and s.kp is not None:
        left = replace(s, kp=None)
        if rules.abandon_consumes_epoch:
            left = replace(left, epoch=s.kp[1], kv=None)
        out.append(("abandon_pending", left))

    # An established session ends; the authoritative epoch is retained.
    if rules.session_end and s.kv is not None:
        out.append(("session_end", replace(s, kv=None)))

    # Stale/replayed activation rejection preserves authoritative state (G3).
    stale = s.kv is not None if rules.reject_stale_needs_active else s.epoch > 0
    if stale and rules.reject_stale_per_epoch:
        out += [(f"reject_stale:{e}", s) for e in range(1, s.epoch + 1)]
    elif stale:
        out.append(("reject_stale", s))

    # Dependency interruption and restoration.
    if s.sq or rules.dep_toggles_unconditional:
        out.append(("src_down", _interrupt(s, rules, sq=False)))
    if not s.sq or rules.dep_toggles_unconditional:
        out.append(("src_up", _restore(s, rules, sq=True)))
    if s.se or rules.dep_toggles_unconditional:
        down = _interrupt(s, rules, se=False)
        if rules.ekm_down_clears_active:
            down = replace(down, ke=None)
        out.append(("ekm_down", down))
    if not s.se or rules.dep_toggles_unconditional:
        out.append(("ekm_up", _restore(s, rules, se=True)))

    # Continuity authority changes.
    policy_ok = not rules.authority_only_when_degraded or mode(s) != "READY"
    if policy_ok:
        if s.authority or rules.dep_toggles_unconditional:
            withdrawn = replace(s, authority=False)
            # T5/T6: withdrawal from holdover or exhaustion records suspension.
            if rules.suspend_state and mode(s) in ("BINDING_HOLDOVER", "EXHAUSTED"):
                withdrawn = replace(withdrawn, suspended=True)
            out.append(("authority_withdraw", withdrawn))
        if not s.authority or rules.dep_toggles_unconditional:
            out.append(("authority_grant", replace(s, authority=True)))

    # T7-T10 detection promoted to reconciliation once both dependencies hold.
    if rules.restoration_detected and s.detected and s.sq and s.se:
        out.append(("reconcile", replace(s, detected=False, recovery=True)))

    # Recovery reconciliation (G4).
    if s.sq and s.se and s.recovery and (s.authority or not rules.recover_needs_authority):
        done = replace(s, recovery=False, suspended=False)
        if rules.recover_clears_pending:
            done = replace(done, kp=None)
        if rules.recover_clears_active:
            done = replace(done, kv=None)
        out.append(("recover", done))

    return out


def _interrupt(s: State, rules: Rules, *, sq: bool | None = None, se: bool | None = None) -> State:
    down = replace(s, sq=s.sq if sq is None else sq, se=s.se if se is None else se)
    if rules.down_clears_pending:
        down = replace(down, kp=None)
    return down


def _restore(s: State, rules: Rules, *, sq: bool | None = None, se: bool | None = None) -> State:
    up = replace(s, sq=s.sq if sq is None else sq, se=s.se if se is None else se)
    if rules.restore_sets_recovery and (
        not rules.restore_recovery_needs_full or (up.sq and up.se)
    ):
        up = replace(up, detected=True) if rules.restoration_detected else replace(up, recovery=True)
    return up
