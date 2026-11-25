"""Export the FEB tree from a floor-plan image as Newick text and a PNG."""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from feb_image import NODE_NAMES, graph_node_names

@dataclass
class Branch:
    node_id: int
    name: str
    kind: str
    length: float
    children: list


COLORS = {"inward": "white", "street": "#919191"}


def make_trees(ground, vectors, threshold, graph=None):
    """Root each FEB component and measure the EB at its actual edge endpoints."""
    eb = np.clip(np.hypot(vectors[..., 0], vectors[..., 1]), 0, 1)
    if graph is None:
        from feb_image import build_eb_graph

        graph = build_eb_graph(ground, eb, threshold, vectors)
    regions, centers, paths, types = graph
    if not centers:
        raise ValueError("No node cores exist at the selected EB threshold.")

    names = graph_node_names(centers, types)
    order = sorted(range(len(centers)), key=centers.__getitem__)

    neighbors = [[] for _ in centers]
    for path in paths:
        start, end = map(tuple, path[[0, -1]])
        a, b = int(regions[start] - 1), int(regions[end] - 1)
        length = abs(float(eb[start]) - float(eb[end]))
        neighbors[a].append((b, length))
        neighbors[b].append((a, length))

    def branch(node, parent, length):
        children = [
            branch(other, node, difference)
            for other, difference in sorted(neighbors[node], key=lambda entry: centers[entry[0]])
            if other != parent
        ]
        return Branch(node, names[node], types[node], length, children)

    remaining = set(order)
    trees = []
    for first in order:
        if first not in remaining:
            continue
        members, todo = [], [first]
        remaining.remove(first)
        while todo:
            node = todo.pop()
            members.append(node)
            for other, _ in neighbors[node]:
                if other in remaining:
                    remaining.remove(other)
                    todo.append(other)
        root = min(members, key=lambda node: (eb[centers[node]], centers[node]))
        trees.append(branch(root, None, 0))
    return trees


def newick(tree):
    def serialize(node):
        children = ",".join(f"{serialize(child)}:{child.length:.6f}" for child in node.children)
        return (f"({children})" if children else "") + node.name

    return serialize(tree) + ";"


def draw_trees(trees, target, extra_links=()):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as DrawingPath
    from matplotlib.patches import PathPatch
    from matplotlib import patheffects

    layouts = []
    for tree in trees:
        rows = []

        def place(node, x=0, parent=None):
            current = len(rows)
            rows.append((node, x, parent))
            for child in node.children:
                place(child, x + child.length, current)

        place(tree)
        layouts.append(rows)

    rows_total = sum(map(len, layouts))
    last_x = max(x for rows in layouts for _, x, _ in rows)
    horizontal = max(0.01, last_x)
    height = 1.6 + 0.43 * rows_total + 0.65 * len(trees)
    figure = Figure(figsize=(10, height), dpi=150)
    FigureCanvasAgg(figure)
    figure.suptitle("FEB Newick diagram", x=.075, y=1 - .17 / height, ha="left", fontsize=21,
                    color="#202735", fontweight="bold")
    legends = [
        Line2D([], [], marker="o", markersize=7, linestyle="none", markeredgecolor="#202735",
               markerfacecolor=COLORS[kind], label=name)
        for kind, name in NODE_NAMES.items()
    ]
    loop_color = "#704cb2"
    if extra_links:
        legends.append(Line2D([], [], color=loop_color, linestyle="--",
                              label="Added loop edge"))
    figure.legend(handles=legends, loc="upper left", bbox_to_anchor=(.063, 1 - .55 / height),
                  ncol=len(legends), frameon=False)
    grid = figure.add_gridspec(len(trees), 1, height_ratios=[len(rows) + 1 for rows in layouts],
                              left=.08, right=.96, bottom=.50 / height, top=1 - 1.10 / height,
                              hspace=.3)
    for index, rows in enumerate(layouts):
        axis = figure.add_subplot(grid[index])
        axis.set_title(f"T{index + 1}  /  Root {rows[0][0].name}", loc="left",
                       color="#202735", fontsize=11, pad=8, fontweight="bold")
        text_x = horizontal * 1.08
        positions = {node.node_id: (x, row) for row, (node, x, _) in enumerate(rows)}
        panel_links = [(a, b) for a, b in extra_links if a in positions and b in positions]
        for lane, (a, b) in enumerate(panel_links, start=1):
            start, end = positions[a], positions[b]
            side = horizontal * (1 + .07 * lane / (len(panel_links) + 1))
            curve = DrawingPath([start, (side, start[1]), (side, end[1]), end],
                                [DrawingPath.MOVETO] + [DrawingPath.CURVE4] * 3)
            axis.add_patch(PathPatch(curve, facecolor="none", edgecolor=loop_color,
                                    linewidth=1.3, linestyle=(0, (3, 2)), zorder=2))
        for row, (node, x, parent) in enumerate(rows):
            if parent is not None:
                parent_x = rows[parent][1]
                axis.plot([parent_x, parent_x, x], [parent, row, row],
                          linewidth=.95, color="#576779", zorder=1)
                axis.annotate(f"{node.length:.6f}", ((parent_x + x) / 2, row),
                              xytext=(0, 5), textcoords="offset points", ha="center",
                              fontsize=8, color="#39475a", fontfamily="monospace",
                              path_effects=[patheffects.withStroke(linewidth=2, foreground="white")])
            axis.plot([x, text_x - .02 * horizontal], [row, row], color="#bdc6d3",
                      linestyle=":", linewidth=.55, zorder=0)
            axis.scatter(x, row, s=46, facecolor=COLORS[node.kind], edgecolor="#202735",
                         linewidth=.85, clip_on=False, zorder=3)
            axis.text(text_x, row, node.name, ha="left", va="center", fontsize=10,
                      color="#202735")
        axis.set(xlim=(-.04 * horizontal, 1.32 * horizontal), ylim=(len(rows) - .4, -.65))
        axis.set_xlabel("Cumulative branch length (absolute EB difference)", loc="left",
                        fontsize=8, color="#576779")
        axis.tick_params(axis="x", labelsize=8, colors="#576779", length=3)
        axis.set_yticks([])
        for side in ("top", "left", "right"):
            axis.spines[side].set_visible(False)
        axis.spines["bottom"].set_color("#bdc6d3")
    figure.savefig(target, facecolor="white")


def export_newick(ground, vectors, threshold, prefix, graph=None, diagram_format="png",
                  overlay_paths=None):
    if overlay_paths is not None and graph is None:
        from feb_image import build_eb_graph

        eb = np.clip(np.hypot(vectors[..., 0], vectors[..., 1]), 0, 1)
        graph = build_eb_graph(ground, eb, threshold, vectors)
    trees = make_trees(ground, vectors, threshold, graph)
    extra_links = []
    if overlay_paths is not None:
        regions, _, tree_paths, _ = graph
        for path in overlay_paths[len(tree_paths):]:
            start, end = map(tuple, path[[0, -1]])
            extra_links.append((int(regions[start] - 1), int(regions[end] - 1)))
    prefix.with_name(prefix.name + ".nwk").write_text(
        "\n".join(newick(tree) for tree in trees) + "\n", encoding="utf-8"
    )
    draw_trees(trees, prefix.with_name(f"{prefix.name}.{diagram_format}"), extra_links)


def main():
    from feb_image import compute_eb, ground_mask, read_image

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output_prefix", type=Path)
    parser.add_argument("--threshold", type=int, default=128)
    parser.add_argument("--node-eb", type=float, default=0.18)
    args = parser.parse_args()
    if not 1 <= args.threshold <= 255 or not 0 <= args.node_eb <= 1:
        parser.error("Brightness threshold must be in 1–255 and node EB in 0–1.")
    try:
        ground = ground_mask(read_image(args.image), args.threshold)
        export_newick(ground, compute_eb(ground), args.node_eb, args.output_prefix)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
