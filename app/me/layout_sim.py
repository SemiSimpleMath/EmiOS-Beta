"""Pure-Python simulator for the lens layout + visibility algorithm.

Validates the concentric-rings layout and the priority-collision +
viewport-filter visibility model by drawing a synthetic graph at
multiple zoom levels. Run this to see what the algorithm produces
without React/canvas/zoom interactions getting in the way.

Usage:
    python -m app.me.layout_sim
    python -m app.me.layout_sim --output-dir /tmp/lens_sim
    python -m app.me.layout_sim --node-count 30

Produces a single PNG with a grid of panels — one per zoom level —
showing the rendered scene + claim disks. Color-codes nodes by
importance band so you can see how rings relate to bands.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# matplotlib is imported lazily inside render_grid() — it's a heavy dependency
# (~tens of MB with fonts/contourpy/kiwisolver) used ONLY for this module's optional
# dev visualization, and this module is not imported on the app boot path. Keeping
# the import lazy lets the rest of the module's layout logic be used without pulling
# matplotlib into the install. See scratch/FIX-missing-deps.md.


# ---------- algorithm constants ----------
# Geometric-progression radii: each importance level sits 1.6x farther
# out than the next-less-important. The 1.6 factor comes from:
#   "comfortable" display radius = 500 px
#   "periphery" display radius   = 800 px
# When you zoom such that ring i hits 800 px, ring (i-1) is at 500 px,
# making it the new comfortable focus. Smooth zoom traversal between
# importance levels with no missed nodes.
RING_RATIO = 1.6
COMFORTABLE_PX = 500.0
PERIPHERY_PX = 800.0
R_BASE = 300.0  # imp=1 ring radius (innermost)
GOLDEN = math.pi * (3 - math.sqrt(5))


def radius_for(imp: float) -> float:
    """Geometric progression: r_i = R_BASE × RATIO^(imp - 1).

    imp 1 at R_BASE, imp 10 at R_BASE × 1.6^9 ≈ R_BASE × 68.7.
    Less important (low imp) = closer to anchor.
    """
    return R_BASE * (RING_RATIO ** max(0.0, imp - 1.0))

# Claim radii in graph coords. Calibrated so all are smaller than R_BASE
# (=500, the innermost ring): inner-ring nodes don't get eaten by the
# seed's claim. Importance scales claim proportionally so important
# nodes reserve more space.
SEED_CLAIM = 80.0
ANCHOR_CLAIM = 70.0
ENTITY_CLAIM_BASE = 30.0
ENTITY_CLAIM_PER_IMP = 6.0
CLAIM_PADDING_PX = 6.0


@dataclass
class Node:
    id: str
    label: str
    importance: float
    x: float = 0.0
    y: float = 0.0
    is_seed: bool = False
    is_anchor: bool = False

    def claim_radius(self) -> float:
        if self.is_seed:
            return SEED_CLAIM
        if self.is_anchor:
            return ANCHOR_CLAIM
        return ENTITY_CLAIM_BASE + self.importance * ENTITY_CLAIM_PER_IMP


# ---------- layout ----------
def layout_concentric(nodes: List[Node]) -> None:
    """Place each node on the ring corresponding to its importance band."""
    seed = next((n for n in nodes if n.is_seed), None)
    if seed:
        seed.x = 0.0
        seed.y = 0.0
    by_band: dict = {}
    for n in nodes:
        if n.is_seed:
            continue
        band = max(0, min(10, int(round(n.importance))))
        by_band.setdefault(band, []).append(n)
    for band, members in by_band.items():
        r = radius_for(float(band))
        theta_start = band * GOLDEN
        slot_step = (2 * math.pi) / max(1, len(members))
        for i, n in enumerate(members):
            theta = theta_start + i * slot_step
            n.x = r * math.cos(theta)
            n.y = r * math.sin(theta)


# ---------- visibility (matches frontend claim-collision) ----------
# Number of pixels of "margin" past the collision boundary over which
# opacity ramps from 0 (just-touching) to 1 (clearly separated).
FADE_MARGIN_PX = 60.0


def visibility_at_zoom(
    nodes: List[Node],
    zoom: float,
    viewport: Optional[Tuple[float, float, float, float]] = None,
) -> List[Tuple[Node, float]]:
    """Priority-collision visibility with continuous opacity.

    Returns a list of (node, opacity) for nodes that pass the collision
    test. Opacity is 0 at the collision boundary, 1 at FADE_MARGIN_PX
    past it. So as the user zooms and the on-screen claim disks pull
    apart, nodes fade in smoothly instead of popping.

    `viewport`: (minX, maxX, minY, maxY) in graph coords. None = no filter.
    """
    if viewport is None:
        candidates = nodes
    else:
        minX, maxX, minY, maxY = viewport
        candidates = [
            n for n in nodes
            if n.is_seed or n.is_anchor
            or (minX <= n.x <= maxX and minY <= n.y <= maxY)
        ]
    candidates_sorted = sorted(
        candidates,
        key=lambda n: (
            0 if n.is_seed else 1,
            0 if n.is_anchor else 1,
            -n.importance,
        ),
    )
    visible: List[Tuple[Node, float, float]] = []  # (node, rPx, opacity)
    for n in candidates_sorted:
        rPx = n.claim_radius() * zoom + CLAIM_PADDING_PX
        # Worst (smallest) margin to any already-visible higher-priority node.
        # Seeds and anchors always render at full opacity.
        if n.is_seed or n.is_anchor:
            visible.append((n, rPx, 1.0))
            continue
        worst_margin = float("inf")
        collides = False
        for v, vRpx, _ in visible:
            dx = (n.x - v.x) * zoom
            dy = (n.y - v.y) * zoom
            d_screen = math.hypot(dx, dy)
            margin = d_screen - (rPx + vRpx)
            if margin < 0:
                collides = True
                break
            if margin < worst_margin:
                worst_margin = margin
        if collides:
            continue
        opacity = max(0.0, min(1.0, worst_margin / FADE_MARGIN_PX))
        visible.append((n, rPx, opacity))
    return [(n, op) for n, _, op in visible]


# ---------- synthetic graph ----------
def make_synthetic_graph(node_count: int = 30) -> List[Node]:
    """Generate a synthetic graph: 1 seed (Jukka) + nodes spread across
    importance bands 0..10 so we exercise every ring."""
    nodes: List[Node] = [Node(id="J", label="Jukka", importance=10.0, is_seed=True)]
    # Distribution mimics the real KG: more low-imp than high-imp nodes.
    band_counts = {
        0: 0,   # too noisy for the demo
        1: 4,
        2: 4,
        3: 3,
        4: 3,
        5: 3,
        6: 2,
        7: 2,
        8: 2,
        9: 1,
        10: 0,  # only the seed at 10
    }
    # Scale to node_count.
    total_planned = sum(band_counts.values())
    if total_planned == 0:
        total_planned = 1
    scale = max(1.0, (node_count - 1) / total_planned)
    counter = 1
    for band, count in band_counts.items():
        scaled = max(0, int(round(count * scale)))
        for _ in range(scaled):
            nodes.append(Node(
                id=f"N{counter}",
                label=f"imp{band}-{counter}",
                importance=float(band),
            ))
            counter += 1
            if counter > node_count:
                break
        if counter > node_count:
            break
    return nodes


# ---------- rendering ----------
def render_grid(
    nodes: List[Node],
    output_path: Path,
    zoom_levels: Optional[List[float]] = None,
) -> None:
    """Render a grid of subplots, one per zoom level."""
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt

    if zoom_levels is None:
        # Cover one geometric "octave" per importance step. With ratio
        # 1.6, six panels in geometric progression by 1.6 covers six
        # importance bands, each panel showing the band where one ring
        # is at "comfortable" 500px display.
        # With R_BASE=300, r_10=300×1.6^9=20614, comfortable zooms:
        # imp 10 → 500/20614 ≈ 0.024
        # imp 8 → 500/8053 ≈ 0.062
        # imp 6 → 500/3146 ≈ 0.159
        # imp 4 → 500/1229 ≈ 0.407
        # imp 2 → 500/480 ≈ 1.04
        # imp 1 → 500/300 ≈ 1.67
        zoom_levels = [0.024, 0.062, 0.159, 0.407, 1.04, 1.67]

    cols = 3
    rows = (len(zoom_levels) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(15, 5 * rows))
    axes = axes.flatten() if rows > 1 else [axes] if cols == 1 else axes

    band_colors = {
        10: "#1d4ed8",  # blue (seed)
        9: "#dc2626",   # red
        8: "#ea580c",   # orange
        7: "#ca8a04",   # yellow-ish
        6: "#65a30d",   # lime
        5: "#16a34a",   # green
        4: "#0891b2",   # cyan
        3: "#7c3aed",   # purple
        2: "#9ca3af",   # gray
        1: "#9ca3af",
        0: "#9ca3af",
    }

    for idx, zoom in enumerate(zoom_levels):
        ax = axes[idx]
        # Compute viewport at this zoom: assume canvas is 800×800 px.
        canvas_px = 800.0
        viewport_half = canvas_px / 2 / zoom
        viewport = (
            -viewport_half, viewport_half,
            -viewport_half, viewport_half,
        )

        visible = visibility_at_zoom(nodes, zoom, viewport=viewport)
        opacity_by_id = {n.id: op for n, op in visible}

        # Plot all nodes; visible ones at their fade-in opacity.
        for n in nodes:
            color = band_colors.get(int(round(n.importance)), "#000")
            opacity = opacity_by_id.get(n.id)
            if opacity is None:
                # Hidden: faint dot
                ax.scatter([n.x], [n.y], c=color, s=15, alpha=0.15, zorder=2)
            else:
                # Visible at its fade-in opacity. Note alpha applies to the
                # marker; label opacity follows separately.
                ax.scatter([n.x], [n.y], c=color, s=80,
                           edgecolors="black", linewidths=1,
                           alpha=opacity, zorder=3)
                ax.annotate(n.label, (n.x, n.y), xytext=(4, 4),
                            textcoords="offset points", fontsize=7,
                            alpha=opacity, zorder=4)
            # Always draw claim disk faintly so we see why things hide.
            claim_disk = mpatches.Circle(
                (n.x, n.y), n.claim_radius(),
                fill=False, edgecolor=color, alpha=0.15, linewidth=0.5,
                zorder=1,
            )
            ax.add_patch(claim_disk)

        # Viewport rectangle.
        view_rect = mpatches.Rectangle(
            (viewport[0], viewport[2]),
            viewport[1] - viewport[0],
            viewport[3] - viewport[2],
            fill=False, edgecolor="black", linewidth=1.5, linestyle="--",
            zorder=5,
        )
        ax.add_patch(view_rect)

        ax.set_title(f"zoom={zoom:.3f} — visible: {len(visible)}/{len(nodes)}")
        ax.set_aspect("equal")
        # Bound axes to viewport + a margin.
        margin = (viewport[1] - viewport[0]) * 0.1
        ax.set_xlim(viewport[0] - margin, viewport[1] + margin)
        ax.set_ylim(viewport[2] - margin, viewport[3] + margin)
        ax.grid(True, alpha=0.3)

    # Hide unused subplots.
    for idx in range(len(zoom_levels), len(axes)):
        axes[idx].axis("off")

    fig.suptitle(
        f"Lens layout sim — {len(nodes)} nodes, "
        f"geometric rings (ratio={RING_RATIO}, R_BASE={R_BASE:.0f})",
        fontsize=14,
    )
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="/tmp/lens_sim")
    parser.add_argument("--node-count", type=int, default=24)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "lens_sim.png"

    nodes = make_synthetic_graph(args.node_count)
    layout_concentric(nodes)
    print(f"Generated {len(nodes)} nodes; placed via concentric rings.")
    print(f"Importance distribution:")
    from collections import Counter
    bands = Counter(int(round(n.importance)) for n in nodes)
    for band in sorted(bands.keys(), reverse=True):
        r = radius_for(float(band))
        print(f"  imp={band:>2}: {bands[band]:>2} nodes, ring r={r:.0f}")

    render_grid(nodes, out_path)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
