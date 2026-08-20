"""Table 9 (source headroom / endurance / refill) and per-state QBER table."""

from __future__ import annotations

import pandas as pd

from . import equations


def table9(
    r: float = 82.890625,
    B: float = 3_275_971,
    pairs: tuple[int, ...] = (1, 1000, 10000, 50000, 60000),
) -> pd.DataFrame:
    """Table 9: for each concurrent-pairs count, demand/headroom/endurance/refill.

    "demand" scales the base per-pair demand D() by `pairs`; "headroom" is
    r / (D * pairs); "endurance_h" is B / demand in hours; "refill_min" is the
    time (minutes) to refill after a 1-hour (3600s) interruption, or None
    when demand already meets or exceeds the source rate r.
    """
    D_base = equations.demand()
    rows = []
    for p in pairs:
        D_row = D_base * p
        headroom = r / D_row
        endurance_h = equations.endurance_s(B, D_row) / 3600
        refill = equations.refill_s(D_row, 3600, r)
        refill_min = refill / 60 if refill is not None else None
        rows.append(
            {
                "pairs": p,
                "demand": D_row,
                "headroom": headroom,
                "endurance_h": endurance_h,
                "refill_min": refill_min,
            }
        )
    return pd.DataFrame(rows, columns=["pairs", "demand", "headroom", "endurance_h", "refill_min"])


def qber_states(capture: dict) -> pd.DataFrame:
    """Per-state signal vs weak-decoy QBER, from a capture dict (Fig 12 source)."""
    signal = capture.get("signal_qber_per_state", [])
    weak = capture.get("weak_decoy_qber_per_state", [])
    n = max(len(signal), len(weak))
    states = [f"S{i + 1}" for i in range(n)]
    return pd.DataFrame({"state": states, "signal_qber": signal, "weak_decoy_qber": weak})
