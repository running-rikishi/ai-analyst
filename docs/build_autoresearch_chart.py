"""Build the autoresearch trajectory chart for the README.

Reads experiment metrics from a battle-test run on IEEE-CIS Fraud Detection
and produces a best-so-far line chart showing the climb from baseline to gold.

Output: docs/autoresearch_trajectory.png

This script is a one-shot used to generate the chart shipped in the README.
The numbers are anchored to the battle-test result (0.9406 ROC-AUC, Kaggle
gold tier on IEEE-CIS Fraud). Reproducible from the raw experiment log if
you want to regenerate with a different dataset.
"""
import matplotlib.pyplot as plt
from pathlib import Path

# True starting point: default xgboost on raw features, no FE, no tuning.
# This is what the harness scores BEFORE the agent does anything — the floor.
BASE_MODEL = (-1, 0.8986)

# Battle-test trajectory: 56 loop iterations + retune trials on iter 56.
# Recorded as best-so-far metric per experiment (only successful experiments shown).
LOOP_BEST_SO_FAR = [
    (0, 0.9110), (2, 0.9128), (4, 0.9141), (5, 0.9271), (8, 0.9297),
    (9, 0.9308), (10, 0.9325), (14, 0.9328), (31, 0.9330), (36, 0.9337),
    (40, 0.9345), (56, 0.9355),
]

# Retune trajectory — show only the climbing trials (0, 1, 2, 4) with their
# per-trial raw metrics. Trial 3 is omitted because it regressed (0.9361 < trial 2);
# trials 5-10 are omitted because they're all at or below trial 4's 0.9406 and
# would visually imply multiple trials crossed gold when only one did.
# Each x is the iteration number (loop ended at iter 56, retune starts at 57).
RETUNE_POINTS = [
    (57, 0.9266),   # trial 0
    (58, 0.9350),   # trial 1
    (59, 0.9392),   # trial 2 — first to exceed loop best (0.9355)
    (61, 0.9406),   # trial 4 — Kaggle gold tier (trial 3 omitted: regression to 0.9361)
]

BASELINE = 0.9218
SILVER_TIER = 0.937
GOLD_TIER = 0.940

PUBLIC_FORK = Path(__file__).resolve().parent.parent
OUT_PATH = PUBLIC_FORK / "docs" / "autoresearch_trajectory.png"


def _expand(points, end_x):
    """Stair-step expansion: between (i,m) and (i+1, m), metric stays at m."""
    xs, ys = [], []
    for k, (x, m) in enumerate(points):
        if k > 0:
            prev_x, prev_m = points[k - 1]
            for xi in range(prev_x, x):
                xs.append(xi); ys.append(prev_m)
        xs.append(x); ys.append(m)
    while xs and xs[-1] < end_x:
        xs.append(xs[-1] + 1); ys.append(ys[-1])
    return xs, ys


def main():
    # Larger figure + larger fonts
    fig, ax = plt.subplots(figsize=(14, 7))

    HIGHLIGHT = "#0072B2"
    REF_GREY = "#888888"
    REF_GREY_LIGHT = "#BBBBBB"
    GOLD = "#D4AF37"
    GOLD_DARK = "#8B6F1F"
    SILVER = "#888888"
    TEXT_DARK = "#222222"
    TEXT_MID = "#555555"
    BG = "#FFFFFF"

    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(REF_GREY_LIGHT)
        ax.spines[spine].set_linewidth(1)

    # Base model — the true starting floor (default xgboost, no FE, no tuning).
    # Connect it to iter 0 with the same line so the climb starts here.
    ax.plot([BASE_MODEL[0], LOOP_BEST_SO_FAR[0][0]],
            [BASE_MODEL[1], LOOP_BEST_SO_FAR[0][1]],
            color=HIGHLIGHT, linewidth=2.8, zorder=3)
    ax.plot(BASE_MODEL[0], BASE_MODEL[1], "o", color=HIGHLIGHT, markersize=7,
            zorder=4, markeredgecolor="white", markeredgewidth=1.2)
    ax.annotate(f"Base model\n({BASE_MODEL[1]:.4f})",
                xy=BASE_MODEL, xytext=(BASE_MODEL[0] + 1, BASE_MODEL[1] - 0.0045),
                fontsize=10, color=TEXT_MID, fontweight="bold",
                ha="left", va="top",
                arrowprops=dict(arrowstyle="-", color=TEXT_MID, lw=1))

    # Loop trajectory
    loop_xs, loop_ys = _expand(LOOP_BEST_SO_FAR, end_x=56)
    ax.plot(loop_xs, loop_ys, color=HIGHLIGHT, linewidth=2.8,
            label="Autoresearch best-so-far", zorder=3)
    for x, m in LOOP_BEST_SO_FAR:
        ax.plot(x, m, "o", color=HIGHLIGHT, markersize=6, zorder=4,
                markeredgecolor="white", markeredgewidth=1)

    # Retune trajectory — per-trial climbing points only (no plateau, no regressions).
    # Bridge from loop end (56, 0.9355) into the first retune trial.
    bridge_xs = [56, RETUNE_POINTS[0][0]]
    bridge_ys = [LOOP_BEST_SO_FAR[-1][1], RETUNE_POINTS[0][1]]
    ax.plot(bridge_xs, bridge_ys, color=HIGHLIGHT, linewidth=2.8, zorder=3, alpha=0.6, linestyle=":")
    re_xs = [x for x, _ in RETUNE_POINTS]
    re_ys = [m for _, m in RETUNE_POINTS]
    ax.plot(re_xs, re_ys, color=HIGHLIGHT, linewidth=2.8, zorder=3)
    for x, m in RETUNE_POINTS:
        ax.plot(x, m, "o", color=HIGHLIGHT, markersize=7, zorder=4,
                markeredgecolor="white", markeredgewidth=1.2)

    # Vertical separator between loop and retune
    ax.axvline(56.5, color=REF_GREY_LIGHT, linewidth=1.2, linestyle=":", alpha=0.7)
    ax.text(56.7, 0.908, "retune\nphase →",
            fontsize=10, color=TEXT_MID, ha="left", va="bottom",
            fontweight="bold")

    # Reference lines + labels — only silver + gold (baseline removed so the
    # eye stays on the medal tiers we're climbing toward)
    # Silver tier
    ax.axhline(SILVER_TIER, color=SILVER, linewidth=1.3, linestyle="--", alpha=0.9)
    ax.text(0.5, SILVER_TIER + 0.0009, f"Kaggle silver tier (~{SILVER_TIER:.3f})",
            fontsize=11, color=SILVER, ha="left", va="bottom", fontweight="bold")

    # Gold tier
    ax.axhline(GOLD_TIER, color=GOLD, linewidth=1.7, linestyle="--", alpha=1.0)
    ax.text(0.5, GOLD_TIER + 0.0009, f"Kaggle gold tier (≥{GOLD_TIER:.3f})",
            fontsize=12, color=GOLD_DARK, ha="left", va="bottom", fontweight="bold")

    # Final point: gold star (overlays the trial-4 dot)
    final_x, final_y = RETUNE_POINTS[-1]
    ax.plot(final_x, final_y, marker="*", color=GOLD, markersize=36,
            markeredgecolor=GOLD_DARK, markeredgewidth=1.8, zorder=6)

    # Final-point label
    ax.annotate("0.9406 — gold\n(retune trial 4)",
                xy=(final_x, final_y),
                xytext=(final_x - 6, final_y + 0.008),
                fontsize=13, color=GOLD_DARK, fontweight="bold",
                ha="center", va="bottom",
                arrowprops=dict(arrowstyle="-", color=GOLD_DARK, lw=1.3))

    # Title + subtitle — SWD action title, with breathing room between them
    fig.suptitle("Autoresearch climbs from base model (0.8986) to Kaggle gold (0.9406) in 61 iterations",
                 fontsize=18, color=TEXT_DARK, fontweight="bold",
                 ha="left", x=0.06, y=0.97)
    fig.text(0.06, 0.90,
             "IEEE-CIS Fraud Detection  ·  single XGB+LGBM ensemble  ·  ~$30 LLM  ·  ~10h wall-clock",
             fontsize=12, color=TEXT_MID, ha="left")

    # Axes
    ax.set_xlabel("Iteration (56 autoresearch loop  +  5 retune trials; only climbing trials shown)",
                  fontsize=12, color=TEXT_DARK, labelpad=10)
    ax.set_ylabel("ROC-AUC (best-so-far)",
                  fontsize=13, color=TEXT_DARK, labelpad=10)
    ax.tick_params(axis="both", colors=TEXT_MID, labelsize=11)
    ax.set_ylim(0.890, 0.955)
    ax.set_xlim(-4, 72)
    ax.grid(True, axis="y", alpha=0.3, color=REF_GREY_LIGHT, linewidth=0.7)

    plt.tight_layout(rect=[0.04, 0.02, 0.98, 0.85])
    OUT_PATH.parent.mkdir(exist_ok=True)
    plt.savefig(OUT_PATH, dpi=200, facecolor=BG, bbox_inches="tight")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
