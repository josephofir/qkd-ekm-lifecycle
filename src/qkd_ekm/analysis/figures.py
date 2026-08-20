"""Figures 4, 5, 12, 13. Deterministic (Agg backend, fixed grids/seeds)."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from . import equations

R_DEFAULT = 82.890625
B_DEFAULT = 3_275_971


def fig4_capacity(out_png, r: float = R_DEFAULT):
    """Contour of source-feasible pairs over rotation x VPN-refresh intervals.

    x: rotation interval (5-120 min), y: VPN refresh interval (5-240 min).
    Annotates the configured point (15, 60) -> 59,681 source-feasible pairs.
    """
    tau_e_min = np.linspace(5, 120, 60)
    tau_v_min = np.linspace(5, 240, 60)
    X, Y = np.meshgrid(tau_e_min, tau_v_min)
    D = equations.demand(tau_e=X * 60, tau_v=Y * 60)
    Z = equations.ceiling(r, D)

    fig, ax = plt.subplots(figsize=(6, 5))
    cs = ax.contourf(X, Y, Z, levels=20, cmap="viridis")
    fig.colorbar(cs, ax=ax, label="source-feasible pairs")

    px, py = 15, 60
    pz = equations.ceiling(r, equations.demand(tau_e=px * 60, tau_v=py * 60))
    ax.plot(px, py, "ro")
    ax.annotate(
        f"{int(pz):,} source-feasible pairs",
        (px, py),
        textcoords="offset points",
        xytext=(10, 10),
        color="white",
    )
    ax.set_xlabel("rotation interval (min)")
    ax.set_ylabel("VPN refresh interval (min)")
    ax.set_title("Fig 4: source-feasible pairs")
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    plt.close(fig)


def fig5_reserve(out_png, r: float = R_DEFAULT, B: float = B_DEFAULT):
    """Remaining captured inventory (%) over interruption duration x pairs.

    x: equivalent pairs (10^0..10^4, log), y: interruption duration (0-72 h).
    Contours at 90/75/50/25/10 %, exhaustion boundary (0 %), one-hour line.
    """
    D_base = equations.demand()
    pairs = np.logspace(0, 4, 100)
    duration_h = np.linspace(0, 72, 100)
    Pairs, Dur = np.meshgrid(pairs, duration_h)
    D_row = D_base * Pairs
    t_dep_s = B / D_row  # r_u = 0 during the interruption
    Z = np.clip(100.0 * (1 - (Dur * 3600) / t_dep_s), 0, 100)

    fig, ax = plt.subplots(figsize=(6, 5))
    cs = ax.contour(Pairs, Dur, Z, levels=[10, 25, 50, 75, 90], colors="black")
    ax.clabel(cs, inline=True, fmt="%d%%")
    boundary = ax.contour(Pairs, Dur, Z, levels=[0], colors="red", linewidths=2)
    ax.clabel(boundary, inline=True, fmt={0: "exhaustion boundary"})
    ax.axhline(1, color="tab:blue", linestyle="--")
    ax.text(1.2, 1.5, "one-hour line", color="tab:blue")
    ax.set_xscale("log")
    ax.set_xlabel("equivalent pairs")
    ax.set_ylabel("interruption duration (h)")
    ax.set_title("Fig 5: remaining captured inventory (%)")
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    plt.close(fig)


def fig12_qber(capture: dict, out_png):
    """Grouped bars: signal vs weak-decoy QBER per state."""
    signal = capture.get("signal_qber_per_state", [])
    weak = capture.get("weak_decoy_qber_per_state", [])
    n = max(len(signal), len(weak))
    idx = np.arange(n)
    width = 0.35

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(idx - width / 2, signal, width, label="signal")
    ax.bar(idx + width / 2, weak, width, label="weak decoy")
    ax.set_xticks(idx)
    ax.set_xticklabels([f"S{i + 1}" for i in idx])
    ax.set_ylabel("QBER (%)")
    ax.set_title("Fig 12: signal vs weak-decoy QBER per state")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    plt.close(fig)


def fig13_refill(out_png, r: float = R_DEFAULT, B: float = B_DEFAULT):
    """Refill duration (h, log colour) over utilization x interruption duration.

    x: utilization (% of captured capacity consumed during the interruption,
    log 0.1-100), y: interruption duration (0-24 h). utilization% pins the
    equivalent demand D = utilization/100 * B / t_out, then refill duration
    follows eq. (7)-(8) with r_u = r.
    """
    util_pct = np.logspace(-1, 2, 100)
    duration_h = np.linspace(0.1, 24, 100)  # avoid t_out=0 (division by zero)
    U, Dur = np.meshgrid(util_pct, duration_h)
    t_out_s = Dur * 3600
    D = (U / 100.0) * B / t_out_s

    # NOTE: equations.refill_s isn't vectorized (branches on scalar None),
    # so the same eq. (7)-(8) formula is applied directly over the grid here.
    net = r - D
    with np.errstate(divide="ignore", invalid="ignore"):
        refill_h = np.where(net > 0, D * t_out_s / net / 3600, np.nan)
    refill_h_log = np.log10(np.clip(refill_h, 1e-6, None))

    fig, ax = plt.subplots(figsize=(6, 5))
    cs = ax.contourf(U, Dur, refill_h_log, levels=20, cmap="magma")
    fig.colorbar(cs, ax=ax, label="log10(refill duration, h)")
    ax.set_xscale("log")
    ax.set_xlabel("utilization (% of captured capacity)")
    ax.set_ylabel("interruption duration (h)")
    ax.set_title("Fig 13: refill duration")
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    plt.close(fig)
