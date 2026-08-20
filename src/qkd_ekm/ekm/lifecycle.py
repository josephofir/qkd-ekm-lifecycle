"""Lifecycle-mode derivation.

Single source of truth for the paper's six-mode external-key lifecycle.
Both the live EKM (this module, driving `KeyPool`/`Store` state) and the
reference model (`qkd_ekm.model.state`) call this same function, so the
model's exhaustive state-space search and the running system are checked
against one definition of "what mode are we in" rather than two that could
drift apart.

The six modes, and the observable condition each corresponds to:

- READY: the QKD source is answering and the EKM is reachable -- normal
  operation, keys are minted fresh from the live QKD channel.
- BUFFERED: the QKD source has stopped answering, but the EKM is
  reachable and the local pool still holds unconsumed keys pulled while
  the source was up -- service continues from that buffer.
- BINDING_HOLDOVER: the pool is empty and the source is down, but an
  object already has an authoritative (previously bound) key -- no new
  key can be minted, yet the existing binding remains valid so operation
  on that object continues.
- EXHAUSTED: the pool is empty, the source is down, and there is no
  authoritative binding to fall back on -- no key material is available
  by any path.
- SUSPENDED: continuity authority has been withdrawn (e.g. revoked or
  expired) with no fresh pool to draw from -- the system must not proceed
  even in a degraded mode; this overrides EXHAUSTED/BINDING_HOLDOVER.
- RECOVERY: a recovery procedure is in progress -- takes precedence over
  every other signal, since the system must finish reconciling state
  before resuming normal classification.
"""

from __future__ import annotations


def derive(
    source_available: bool,
    ekm_reachable: bool,
    pool_size: int,
    has_authoritative_binding: bool,
    continuity_authority: bool,
    recovery_pending: bool,
    *,
    policy_suspended: bool = False,
) -> dict:
    if recovery_pending:
        mode = "RECOVERY"
    elif policy_suspended:
        # An explicit policy-suspension record (paper Figure 3, T5/T6) outranks
        # the dependency-derived modes until reconciliation clears it. Off by
        # default: callers that do not track the record keep the old behaviour.
        mode = "SUSPENDED"
    elif source_available and ekm_reachable:
        mode = "READY"
    elif ekm_reachable and pool_size > 0:
        mode = "BUFFERED"
    elif not continuity_authority:
        mode = "SUSPENDED"
    elif has_authoritative_binding:
        mode = "BINDING_HOLDOVER"
    else:
        mode = "EXHAUSTED"
    return {
        "mode": mode,
        "storage": bool(ekm_reachable),
        "fresh_allocation": bool(
            ekm_reachable and pool_size > 0 and mode not in ("SUSPENDED", "RECOVERY")
        ),
        "established_vpn": "distinct",
    }
