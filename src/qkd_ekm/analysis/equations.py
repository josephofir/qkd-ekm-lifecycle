"""Analytics equations (3)-(8) from the paper.

D  = demand (units/s consumed by rotation + VPN refresh)
m_Q = source-feasible pairs ceiling = alpha * r / D
T_out = endurance = beta * B / D
T_dep = depletion time while D exceeds the usable source rate r_u
T_refill = time to refill the pool after an interruption of duration t_out
"""

from __future__ import annotations


def demand(
    tau_e: float = 900,
    tau_v: float = 3600,
    a_e: float = 1,
    a_v: float = 1,
    n_e: float = 1,
    n_v: float = 1,
):
    """Eq. (3): D = a_E n_E / tau_E + a_V n_V / tau_V."""
    return a_e * n_e / tau_e + a_v * n_v / tau_v


def ceiling(r, D, alpha: float = 1.0):
    """Eq. (4): m_Q = alpha * r / D — source-feasible concurrent pairs."""
    return alpha * r / D


def endurance_s(B, D, beta: float = 1.0):
    """Eq. (5): T_out = beta * B / D — seconds the buffer B lasts at demand D."""
    return beta * B / D


def depletion_s(B, D, r_u):
    """Eq. (6): T_dep = B / (D - r_u), only defined while D > r_u.

    Returns None when the usable source rate r_u keeps up with demand D
    (buffer never depletes).
    """
    net = D - r_u
    if net <= 0:
        return None
    return B / net


def refill_s(D, t_out, r_u):
    """Eq. (7)-(8): T_refill = D * t_out / (r_u - D) after an interruption t_out.

    Returns None when the usable rate r_u cannot outrun demand D (refill
    never completes).
    """
    net = r_u - D
    if net <= 0:
        return None
    return D * t_out / net


def reserve_pct(B, D, t_out_s, r_u: float = 0.0):
    """Remaining captured inventory (%) after an interruption of t_out_s seconds.

    Derived from the depletion equation: with no (or degraded) source
    replenishment during the interruption, remaining = B - (D - r_u) * t_out_s.
    Clipped to [0, 100].
    """
    t_dep = depletion_s(B, D, r_u)
    if t_dep is None:
        # usable rate keeps up with demand: buffer isn't drained
        return 100.0
    pct = 100.0 * (1 - t_out_s / t_dep)
    return max(0.0, min(100.0, pct))
