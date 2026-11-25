"""Compute and color the Enclosing Balance (FEB2) of a bitmap floor plan."""

import argparse
import json
from heapq import heappop, heappush
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def read_image(path):
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def ground_mask(image, threshold, dark_ground=False):
    light = np.asarray(image.convert("L")) >= threshold
    return ~light if dark_ground else light


def despeckle_ground(ground, max_pixels):
    """Replace small isolated islands with their surrounding class."""
    from scipy.ndimage import label

    cleaned = ground.copy()
    connectivity = np.ones((3, 3), dtype=bool)
    for kind in (False, True):
        components, _ = label(ground == kind, structure=connectivity)
        remove = np.bincount(components.ravel()) <= max_pixels
        remove[0] = False
        remove[components[0, :]] = False
        remove[components[-1, :]] = False
        remove[components[:, 0]] = False
        remove[components[:, -1]] = False
        cleaned[remove[components]] = not kind
    return cleaned


def ray_steps(width, height, dx, dy):
    """Yield the distance and grid offset at each entry into a new cell."""
    step_x = np.inf if abs(dx) < 1e-14 else 1 / abs(dx)
    step_y = np.inf if abs(dy) < 1e-14 else 1 / abs(dy)
    sign_x = 1 if dx > 0 else -1
    sign_y = 1 if dy > 0 else -1
    col = row = crossed_x = crossed_y = 0
    next_x, next_y = step_x / 2, step_y / 2
    while crossed_x < width and crossed_y < height:
        distance = min(next_x, next_y)
        # At a shared corner, enter the diagonal cell. Cells touched
        # only at the corner have no angular width.
        if next_x <= distance + 1e-10:
            col += sign_x
            crossed_x += 1
            next_x = (crossed_x + 0.5) * step_x
        if next_y <= distance + 1e-10:
            row += sign_y
            crossed_y += 1
            next_y = (crossed_y + 0.5) * step_y
        yield distance, row, col


def first_wall_weights(solid, rows, cols, dx, dy):
    """Return 1/r² for the first wall, or zero for rays that leave the image."""
    height, width = solid.shape
    weights = np.zeros(rows.size, dtype=np.float64)
    active = np.arange(rows.size, dtype=np.intp)
    for distance, row_offset, col_offset in ray_steps(width, height, dx, dy):
        y = rows[active] + row_offset
        x = cols[active] + col_offset
        inside = (y >= 0) & (y < height) & (x >= 0) & (x < width)
        active = active[inside]
        if not active.size:
            break
        hit = solid[y[inside], x[inside]]
        weights[active[hit]] = 1 / distance**2
        active = active[~hit]
        if not active.size:
            break
    return weights


def compute_eb(ground, rays=256):
    """Return the EB2 vectors, with x right and y up, and NaN where undefined.

    Yoon, Plan Schematization, pp. 95–107. Approximate the angular
    integrals by equally spaced rays. Each solid pixel is a unit square.
    The ray goes from void to wall, so the wall-to-void vector is its negative.
    """
    if rays < 8 or rays % 4:
        raise ValueError("The ray count must be a multiple of four and at least eight.")
    ground = np.asarray(ground, dtype=bool)
    rows, cols = np.nonzero(ground)
    vectors = np.full((*ground.shape, 2), np.nan)
    solid = ~ground
    if not rows.size or not solid.any():
        return vectors

    total = np.zeros(rows.size)
    vx = np.zeros_like(total)
    vy = np.zeros_like(total)
    for theta in np.arange(rays // 2) * (2 * np.pi / rays):
        dx, dy = np.cos(theta), np.sin(theta)
        forward = first_wall_weights(solid, rows, cols, dx, dy)
        backward = first_wall_weights(solid, rows, cols, -dx, -dy)
        total += forward + backward
        difference = backward - forward
        vx += dx * difference
        vy -= dy * difference

    defined = total > 0
    y, x = rows[defined], cols[defined]
    vectors[y, x, 0] = vx[defined] / total[defined]
    vectors[y, x, 1] = vy[defined] / total[defined]
    return vectors


def render_values(image, ground, values, fixed_scale=False):
    from matplotlib import colormaps

    if values.shape != ground.shape:
        raise ValueError(f"Expected EB array shape {ground.shape}, got {values.shape}.")
    colored = ground & np.isfinite(values)
    if not fixed_scale and not np.array_equal(colored, ground):
        raise ValueError("Every ground pixel must have a finite EB value.")
    samples = values[colored]
    if not samples.size:
        return image.copy(), np.nan, np.nan
    low, high = float(samples.min()), float(samples.max())
    if fixed_scale:
        colors = np.clip(samples, 0, 1)
    elif high > low:
        colors = (samples - low) / (high - low)
    else:
        colors = np.full(samples.shape, 0.5)
    rgb = np.array(image)
    rgb[colored] = colormaps["viridis"](colors, bytes=True)[:, :3]
    return Image.fromarray(rgb), low, high


def has_clear_segment(valid, start, end):
    """Check that a straight center-to-center segment stays on ground."""
    height, width = valid.shape
    dy, dx = end - start
    length = np.hypot(dx, dy)
    row, col = start
    previous_row, previous_col = row, col
    for distance, row_offset, col_offset in ray_steps(width, height, dx / length, dy / length):
        if distance >= length:
            break
        y, x = row + row_offset, col + col_offset
        if not valid[y, x] or not (valid[y, previous_col] and valid[previous_row, x]):
            return False
        previous_row, previous_col = y, x
    return True


def route_cost(length, eb_a, eb_b):
    """Penalize high-EB segments while accounting for their physical length."""
    return length * (1 + 4 * (eb_a + eb_b))


def build_eb_graph(ground, values, threshold=0.18, vectors=None, close_loops=False):
    """Return the regions, EB minima, edge paths and optional node types."""
    from scipy.ndimage import find_objects, label

    valid = ground & np.isfinite(values)
    regions, count = label(valid & (values <= threshold))
    centers = []
    for node, bounds in enumerate(find_objects(regions), start=1):
        core = regions[bounds] == node
        rows, cols = np.nonzero(core)
        samples = values[bounds][rows, cols]
        candidates = np.flatnonzero(samples == samples.min())
        # Resolve equal minima by choosing the one closest to the region centroid.
        distance = (rows[candidates] - rows.mean())**2 + (cols[candidates] - cols.mean())**2
        index = candidates[np.argmin(distance)]
        centers.append((rows[index] + bounds[0].start, cols[index] + bounds[1].start))

    node_types = None if vectors is None else classify_nodes(regions, centers, vectors)
    if count < 2:
        return regions, centers, [], node_types

    stride = valid.shape[1] + 2
    traversable = np.pad(valid, 1).ravel()
    streets = {node for node, kind in enumerate(node_types or []) if kind == "street"}
    sources = {
        node: [int((row + 1) * stride + col + 1)]
        for node, (row, col) in enumerate(centers)
    }
    if streets:
        labels = np.pad(regions, 1).ravel()
        for node in streets:
            sources[node] = np.flatnonzero(labels == node + 1)

    # Select connection order by saddle level, then route through low EB.
    levels = np.pad(values.astype(float, copy=False), 1, constant_values=np.inf).ravel()
    center_seeds = [
        (node, int((row + 1) * stride + col + 1))
        for node, (row, col) in enumerate(centers)
    ]
    _, contact_levels = propagate_eb_paths(traversable, stride, center_seeds, levels=levels)
    tree = select_eb_tree(count, contact_levels)
    paths = {}
    targets_by_source = {}
    for sequence, (first, second) in enumerate(tree):
        if second in streets and first not in streets:
            first, second = second, first
        targets_by_source.setdefault(first, {})[second] = sequence

    for source, neighbors in targets_by_source.items():
        targets = {index: node for node in neighbors for index in sources[node]}
        seeds = [(source, index) for index in sources[source]]
        parent, hits = propagate_eb_paths(traversable, stride, seeds, targets=targets,
                                          route_eb=levels)
        for node, index in hits.items():
            path = np.asarray(trace_eb_path(parent, stride, index))
            paths[neighbors[node]] = path
    paths = [paths[order] for order in range(len(tree))]
    if close_loops and tree:
        paths.extend(find_leaf_loops(valid, regions, centers, sources, tree, paths, values))
    return regions, centers, paths, node_types


def select_eb_tree(count, contact_levels):
    """Connect different trees in ascending EB order (Kruskal's algorithm)."""
    components = list(range(count))

    def root(node):
        while components[node] != node:
            components[node] = components[components[node]]
            node = components[node]
        return node

    edges = []
    for (first, second), _ in sorted(contact_levels.items(), key=lambda item: (item[1], item[0])):
        a, b = root(first), root(second)
        if a == b:
            continue
        components[b] = a
        edges.append((first, second))
    return edges


def ray_crossings(start, end, obstacle):
    """Test segments against a rightward ray from inside a wall pixel."""
    y, x = obstacle
    ay, ax = np.moveaxis(start, -1, 0)
    by, bx = np.moveaxis(end, -1, 0)
    crosses_row = (ay > y) != (by > y)
    dy = by - ay
    fraction = (y - ay) / np.where(dy == 0, 1, dy)
    return crosses_row & (ax + fraction * (bx - ax) > x)


def find_leaf_loops(valid, regions, centers, sources, tree, paths, values):
    """Find independent, wall-separated alternate routes from tree leaves."""
    from collections import deque
    from scipy.ndimage import find_objects, label
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra

    walls, _ = label(~valid, structure=np.ones((3, 3)))
    outer = set(np.r_[walls[0], walls[-1], walls[:, 0], walls[:, -1]])
    obstacles = []
    for wall, bounds in enumerate(find_objects(walls), start=1):
        if wall not in outer:
            y, x = np.argwhere(walls[bounds] == wall)[0]
            obstacles.append((y + bounds[0].start + 0.25, x + bounds[1].start))
    if not obstacles:
        return []

    adjacency = [[] for _ in centers]
    for edge, (a, b) in enumerate(tree):
        adjacency[a].append((b, edge))
        adjacency[b].append((a, edge))
    degree = [len(neighbors) for neighbors in adjacency]
    leaves = [node for node, size in enumerate(degree) if size == 1]

    stride = valid.shape[1] + 2
    ids = np.flatnonzero(np.pad(valid, 1).ravel())
    points = np.argwhere(valid)
    size = len(points)
    compact = np.full((valid.shape[0] + 2) * stride, -1, dtype=np.intp)
    compact[ids] = np.arange(size)
    ports = [compact[sources[node]] for node in range(len(centers))]
    all_ports = np.concatenate(ports)
    port_index = np.full(size, -1, dtype=np.intp)
    port_index[all_ports] = np.arange(len(all_ports))
    core_links = []
    for node, pixels in enumerate(ports):
        if len(pixels) < 2:
            continue
        y, x = centers[node]
        root = compact[(y + 1) * stride + x + 1]
        remaining = set(pixels) - {root}
        queue = deque([root])
        while queue:
            a = queue.popleft()
            for shift in (-stride, stride, -1, 1):
                b = compact[ids[a] + shift]
                if b in remaining:
                    remaining.remove(b)
                    core_links.append((a, b))
                    queue.append(b)

    # Build the weighted pixel grid once and lift it into two crossing states
    # for each wall. Diagonal moves use both adjacent ground pixels.
    start, end, weight = [], [], []
    for dy, dx in ((0, 1), (stride, -1), (stride, 0), (stride, 1)):
        other = compact[ids + dy + dx]
        allowed = (other >= 0) & (compact[ids + dy] >= 0) & (compact[ids + dx] >= 0)
        a, b = np.flatnonzero(allowed), other[allowed]
        start.extend((a, b))
        end.extend((b, a))
        weight.extend((np.full(len(a), 2**0.5 if dy and dx else 1.0),) * 2)
    a, b, weights = map(np.concatenate, (start, end, weight))
    eb = values[valid]
    weights = route_cost(weights, eb[a], eb[b])
    flat_labels = np.pad(regions, 1).ravel()[ids] - 1
    candidates = []
    wall_states = []
    for obstacle in obstacles:
        offsets = np.zeros(size, dtype=np.int8)
        if core_links:
            links = np.asarray(core_links)
            cross = ray_crossings(points[links[:, 0]], points[links[:, 1]], obstacle)
            for (u, v), bit in zip(links, cross):
                offsets[v] = offsets[u] ^ bit
        edge_bits = []
        for path in paths:
            endpoints = compact[(path[[0, -1], 0] + 1) * stride + path[[0, -1], 1] + 1]
            edge_bits.append(
                (np.count_nonzero(ray_crossings(path[:-1], path[1:], obstacle)) % 2)
                ^ offsets[endpoints[0]] ^ offsets[endpoints[1]]
            )
        parity = np.zeros(len(centers), dtype=np.int8)
        components = np.full(len(centers), -1, dtype=np.intp)
        for root in range(len(centers)):
            if components[root] >= 0:
                continue
            components[root] = root
            todo = [root]
            while todo:
                u = todo.pop()
                for v, edge in adjacency[u]:
                    if components[v] < 0:
                        components[v] = root
                        parity[v] = parity[u] ^ edge_bits[edge]
                        todo.append(v)

        wall_states.append((offsets[all_ports], parity))
        crossing = ray_crossings(points[a], points[b], obstacle).astype(np.intp)
        lifted = csr_matrix((np.tile(weights, 2),
                             (np.r_[a, a + size],
                              np.r_[b + crossing * size, b + (1 - crossing) * size])),
                            shape=(2 * size, 2 * size))
        for leaf in leaves:
            entry = ports[leaf]
            distance, parent, _ = dijkstra(
                lifted, indices=entry + offsets[entry].astype(np.intp) * size,
                min_only=True, return_predecessors=True,
            )
            goals = []
            for target, exits in enumerate(ports):
                if target == leaf or components[target] != components[leaf]:
                    continue
                required = (offsets[exits] ^ parity[leaf] ^ parity[target] ^ 1).astype(np.intp)
                states = exits + required * size
                hit = states[np.argmin(distance[states])]
                if np.isfinite(distance[hit]):
                    goals.append((distance[hit], target, hit))
            for length, target, hit in sorted(goals):
                route = []
                while hit >= 0:
                    route.append(hit % size)
                    hit = parent[hit]
                if len(set(route)) == len(route):
                    candidates.append((length, leaf, target, np.asarray(route[::-1])))
                    break

    added, basis = [], {}
    for _, leaf, target, route in sorted(candidates, key=lambda item: item[:3]):
        if degree[leaf] != 1 and degree[target] != 1:
            continue
        path = points[route]
        signature = 0
        for bit, (obstacle, (offsets, parity)) in enumerate(zip(obstacles, wall_states)):
            crossing = np.count_nonzero(ray_crossings(path[:-1], path[1:], obstacle)) % 2
            a, b = route[0], route[-1]
            reference = (offsets[port_index[a]] ^ offsets[port_index[b]]
                         ^ parity[flat_labels[a]] ^ parity[flat_labels[b]])
            signature |= int(crossing ^ reference) << bit
        while signature and signature.bit_length() in basis:
            signature ^= basis[signature.bit_length()]
        if signature:
            basis[signature.bit_length()] = signature
            added.append(path)
            degree[leaf] += 1
            degree[target] += 1
    return added


def propagate_eb_paths(traversable, stride, seeds, targets=None, levels=None, route_eb=None):
    """Find EB contact levels or shortest routes to the given targets."""
    owner = np.full(traversable.size, -1, dtype=np.intp)
    distance = np.full(traversable.size, np.inf)
    parent = np.full_like(owner, -1)
    settled = np.zeros_like(traversable)
    queue = []
    for node, index in seeds:
        owner[index] = node
        distance[index] = 0 if levels is None else levels[index]
        heappush(queue, (distance[index], index))

    contacts, hits = {}, {}
    remaining = set(targets.values()) if targets is not None else set()
    moves = [(offset, 1, 0, 0) for offset in (-stride, -1, 1, stride)]
    if levels is None:
        moves += [(dy + dx, 2**0.5, dy, dx) for dy in (-stride, stride) for dx in (-1, 1)]
    while queue:
        cost, index = heappop(queue)
        if settled[index]:
            continue
        settled[index] = True
        if targets is not None and index in targets and targets[index] in remaining:
            target = targets[index]
            hits[target] = index
            remaining.remove(target)
            if not remaining:
                break
        node = owner[index]
        for offset, step, row_step, col_step in moves:
            neighbor = index + offset
            if not traversable[neighbor]:
                continue
            if not (traversable[index + row_step] and traversable[index + col_step]):
                continue
            if levels is not None:
                candidate = max(cost, levels[neighbor])
            else:
                length = step if route_eb is None else route_cost(step, route_eb[index],
                                                                 route_eb[neighbor])
                candidate = cost + length
            if candidate < distance[neighbor]:
                owner[neighbor] = node
                distance[neighbor] = candidate
                parent[neighbor] = index
                heappush(queue, (candidate, neighbor))
            elif levels is not None and settled[neighbor] and node != owner[neighbor]:
                other = owner[neighbor]
                key = (min(node, other), max(node, other))
                level = max(cost, distance[neighbor])
                if key not in contacts or level < contacts[key]:
                    contacts[key] = level
    return parent, contacts if levels is not None else hits


def trace_eb_path(parent, stride, index):
    path = []
    while index >= 0:
        row, col = divmod(index, stride)
        path.append((row - 1, col - 1))
        index = parent[index]
    return path


def rounded_edge(valid, path, preserve_route=False):
    """Return a drawing path with ground-only shortcuts and rounded corners."""
    from matplotlib.path import Path as DrawingPath

    if len(path) > 2:
        direction = np.diff(path, axis=0)
        turns = path[np.r_[True, np.any(direction[1:] != direction[:-1], axis=1), True]]
        path = turns
        if not preserve_route:
            points = [turns[0]]
            index = 0
            while index < len(turns) - 1:
                next_index = min(index + 12, len(turns) - 1)
                while next_index > index + 1 and not has_clear_segment(
                    valid, turns[index], turns[next_index]
                ):
                    next_index -= 1
                points.append(turns[next_index])
                index = next_index
            path = np.asarray(points)

    vertices, codes = [path[0]], [DrawingPath.MOVETO]
    for previous, corner, following in zip(path, path[1:], path[2:]):
        incoming, outgoing = previous - corner, following - corner
        left, right = np.linalg.norm(incoming), np.linalg.norm(outgoing)
        radius = min(8, 0.4 * left, 0.4 * right)
        while True:
            start = corner + incoming * (radius / left)
            end = corner + outgoing * (radius / right)
            # The Bézier curve stays within its control points' bounding box.
            controls = np.array([start, corner, end])
            y0, x0 = np.floor(controls.min(axis=0) + 0.5).astype(int)
            y1, x1 = np.floor(controls.max(axis=0) + 0.5).astype(int)
            if valid[y0:y1 + 1, x0:x1 + 1].all():
                break
            radius *= 0.5
        vertices.extend((start, corner, end))
        codes.extend((DrawingPath.LINETO, DrawingPath.CURVE3, DrawingPath.CURVE3))
    vertices.append(path[-1])
    codes.append(DrawingPath.LINETO)
    return DrawingPath(np.asarray(vertices)[:, ::-1], codes)


def classify_nodes(regions, centers, vectors):
    """Classify cores by the signs of their inward vector projections."""
    rows, cols = np.nonzero(regions)
    labels = regions[rows, cols]
    center_rows, center_cols = np.asarray(centers, dtype=np.intp).reshape(-1, 2)[labels - 1].T
    inward_x = center_cols - cols
    inward_y = rows - center_rows
    vx, vy = vectors[rows, cols].T
    magnitude = np.hypot(vx, vy)
    radius = np.hypot(inward_x, inward_y)
    usable = (radius > 0) & np.isfinite(magnitude) & (magnitude > 1e-12)
    projection = vx * inward_x + vy * inward_y
    tolerance = 1e-12 * magnitude * radius
    inward = usable & (projection > tolerance)
    total = np.bincount(labels[usable], minlength=len(centers) + 1)[1:]
    count = np.bincount(labels[inward], minlength=len(centers) + 1)[1:]
    return ["inward" if size and hit == size else "street"
            for hit, size in zip(count, total)]


NODE_COLORS = {
    "inward": (255, 255, 255),
    "street": (145, 145, 145),
}

NODE_NAMES = {"inward": "Space", "street": "Street"}


def graph_node_names(centers, types):
    counts = dict.fromkeys(NODE_NAMES, 0)
    names = {}
    for node in sorted(range(len(centers)), key=centers.__getitem__):
        kind = types[node]
        counts[kind] += 1
        names[node] = f"{NODE_NAMES[kind]}_{counts[kind]}"
    return names


def draw_overlays(image, ground, values, show_white, highlight, vectors,
                  vector_step, vector_scale, graph=None, node_types=None, node_eb=None,
                  preserve_routes=False, *, layer=None):
    """Draw arrows, isolines and an EB graph at the original image resolution."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patheffects import Stroke, Normal
    from matplotlib.patches import PathPatch
    from matplotlib.collections import LineCollection

    valid = ground & np.isfinite(values)
    samples = values[valid]
    width, height = image.size
    blank = Image.new("RGBA", image.size) if layer is not None else image
    if not samples.size:
        return blank, False

    low, high = samples.min(), samples.max()
    can_contour = min(width, height) >= 2
    yellow_levels = sorted({
        level for level in (highlight, node_eb)
        if can_contour and level is not None and low <= level <= high
    })
    white_levels = [
        level for level in np.arange(1, 10) / 10
        if can_contour and show_white and low < level < high
        and not any(np.isclose(level, yellow, rtol=0, atol=1e-12) for yellow in yellow_levels)
    ]
    if not white_levels and not yellow_levels and vectors is None and graph is None:
        return blank, False

    dpi = 96
    figure = Figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    figure.patch.set_alpha(0)
    canvas = FigureCanvasAgg(figure)
    # Set the dimensions in pixels to avoid rounding in the Agg canvas.
    figure.set_size_inches(np.nextafter(width / dpi, np.inf),
                           np.nextafter(height / dpi, np.inf))
    axes = figure.add_axes((0, 0, 1, 1), frameon=False)
    axes.set_axis_off()
    axes.set_xlim(-0.5, width - 0.5)
    axes.set_ylim(height - 0.5, -0.5)
    if vectors is not None and layer in (None, "vectors"):
        default_step = max(8, round(max(width, height) / 18))
        step = vector_step or default_step
        # Center the sampling grid in the image, including narrow images.
        rows = np.arange((height - 1) % step // 2, height, step)
        cols = np.arange((width - 1) % step // 2, width, step)
        x, y = np.meshgrid(cols, rows)
        vx, vy = np.moveaxis(vectors[y, x], -1, 0)
        visible = valid[y, x] & (np.hypot(vx, vy) > 1e-12)
        if visible.any():
            magnitude = np.hypot(vx[visible], vy[visible])
            direction = np.column_stack((vx[visible], -vy[visible])) / magnitude[:, None]
            perpendicular = np.column_stack((-direction[:, 1], direction[:, 0]))
            length = np.maximum(1.8, 0.8 * default_step * vector_scale * magnitude)
            tip = np.column_stack((x[visible], y[visible]))
            back = tip - direction * length[:, None]
            spread = perpendicular * (length * np.tan(np.deg2rad(15)))[:, None]
            heads = np.stack((back + spread, tip, back - spread), axis=1)
            arrows = LineCollection(heads, colors="#67f3ff", linewidths=0.65, zorder=1)
            axes.add_collection(arrows)
    field = np.ma.array(values, mask=~valid)
    if white_levels and layer in (None, "white"):
        axes.contour(field, levels=white_levels, colors="white", linewidths=0.75)
    highlighted = False
    if yellow_levels and layer in (None, "yellow"):
        contour = axes.contour(field, levels=yellow_levels, colors="#ffeb00", linewidths=1.125)
        if highlight in yellow_levels:
            highlighted = any(len(segment) > 1 for segment in
                              contour.allsegs[yellow_levels.index(highlight)])

    if graph is not None:
        regions, centers, paths = graph
        colors = [
            NODE_COLORS[kind] for kind in node_types
        ] if node_types is not None else [(255, 242, 219)] * len(centers)
        if layer in (None, "tint"):
            tint_colors = [(0, 0, 0, 0)] + [(*color, 100) for color in colors]
            tint = np.asarray(tint_colors, dtype=np.uint8)[regions]
            axes.imshow(tint, interpolation="nearest", aspect="auto", zorder=0)
        if layer in (None, "edges"):
            for path in paths:
                edge = PathPatch(rounded_edge(valid, path, preserve_routes), facecolor="none",
                                 edgecolor="#ff984c", linewidth=0.75,
                                 capstyle="round", joinstyle="round", zorder=4)
                axes.add_patch(edge)
        markers = [
            (center, color) for i, (center, color) in enumerate(zip(centers, colors))
        ]
        if markers and layer in (None, "nodes"):
            points, fills = zip(*markers)
            rows, cols = np.asarray(points).T
            axes.scatter(cols, rows, s=18, facecolor=np.asarray(fills) / 255,
                         edgecolor="#eee8df", linewidth=0.45, zorder=5)
        if node_types is not None and layer == "names":
            for node, name in graph_node_names(centers, node_types).items():
                row, col = centers[node]
                right = col < width * .68
                text = axes.annotate(name, (col, row), xytext=(4 if right else -4, 3),
                                     textcoords="offset points", ha="left" if right else "right",
                                     va="top" if row < 10 else "bottom", fontsize=6.5,
                                     color="white", annotation_clip=False)
                text.set_path_effects([Stroke(linewidth=1.8, foreground="#172336"), Normal()])

    canvas.draw()
    overlay = np.asarray(canvas.buffer_rgba()).copy()
    if layer != "names":
        overlay[~valid, 3] = 0
    if layer is not None:
        return Image.fromarray(overlay), highlighted
    result = Image.alpha_composite(image.convert("RGBA"), Image.fromarray(overlay))
    return result.convert("RGB"), highlighted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path, help="Output PNG file")
    parser.add_argument("--overlay-dir", type=Path, help=argparse.SUPPRESS)
    cache_mode = parser.add_mutually_exclusive_group()
    cache_mode.add_argument("--write-field", type=Path, help=argparse.SUPPRESS)
    cache_mode.add_argument("--reuse-field", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--threshold", type=int, default=128)
    parser.add_argument(
        "--despeckle", type=int, default=0, metavar="PIXELS",
        help="Sharpen the binary image and remove isolated regions up to this area."
    )
    parser.add_argument("--dark-ground", action="store_true")
    parser.add_argument("--ground-mask", type=Path, help="Override with a white-ground mask")
    parser.add_argument("--rays", type=int, default=256, help="Number of angular samples")
    parser.add_argument("--save-values", type=Path, help="Save FEB magnitudes as .npy")
    parser.add_argument(
        "--contours", action="store_true", help="Draw white isolines at 0.1, 0.2, …, 0.9."
    )
    parser.add_argument(
        "--highlight-eb", type=float, metavar="VALUE",
        help="Draw a thick yellow isoline at this FEB value (0–1)."
    )
    parser.add_argument(
        "--vectors", action="store_true", help="Draw EB vectors as arrows."
    )
    parser.add_argument(
        "--vector-step", type=int, metavar="PIXELS",
        help="Spacing between arrows. Default: based on image size."
    )
    parser.add_argument(
        "--vector-scale", type=float, default=1.0, metavar="FACTOR",
        help="Multiplier for arrow lengths. Default: 1."
    )
    parser.add_argument(
        "--graph", action="store_true", help="Draw EB nodes and their ground paths."
    )
    parser.add_argument(
        "--close-loops", action="store_true",
        help="Add wall-separated alternate paths from graph endpoints."
    )
    parser.add_argument(
        "--newick-prefix", type=Path, metavar="PATH",
        help="Also save the FEB tree as a diagram and PATH.nwk."
    )
    parser.add_argument(
        "--newick-format", choices=("png", "pdf"), default="png",
        help="File format for the Newick diagram."
    )
    parser.add_argument(
        "--node-eb", type=float, default=0.18, metavar="VALUE",
        help="Maximum EB in each node region. Default: 0.18."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mask-only", action="store_true")
    mode.add_argument("--eb-values", type=Path, help="Precomputed EB values as a .npy file")
    args = parser.parse_args()
    if not 1 <= args.threshold <= 255:
        parser.error("--threshold must be in 1–255.")
    if args.despeckle < 0:
        parser.error("--despeckle must be nonnegative.")
    if args.output.suffix.lower() != ".png":
        parser.error("Output filename must end in .png.")
    if args.highlight_eb is not None and not 0 <= args.highlight_eb <= 1:
        parser.error("--highlight-eb must be between 0 and 1.")
    if args.mask_only and (args.contours or args.highlight_eb is not None):
        parser.error("Contour options require an FEB field.")
    if args.mask_only and args.graph:
        parser.error("--graph requires an FEB field.")
    if args.close_loops and not args.graph:
        parser.error("--close-loops requires --graph.")
    if args.newick_prefix and (not args.graph or args.eb_values):
        parser.error("--newick-prefix requires --graph and computed EB vectors.")
    if not 0 <= args.node_eb <= 1:
        parser.error("--node-eb must be between 0 and 1.")
    if args.vectors and (args.mask_only or args.eb_values):
        parser.error("--vectors requires computing EB from the input image.")
    if args.vector_step is not None and args.vector_step < 1:
        parser.error("--vector-step must be positive.")
    if not np.isfinite(args.vector_scale) or args.vector_scale <= 0:
        parser.error("--vector-scale must be finite and positive.")
    if args.save_values and (args.mask_only or args.eb_values):
        parser.error("--save-values is available when computing FEB.")
    if args.save_values and args.save_values.suffix.lower() != ".npy":
        parser.error("--save-values must end in .npy.")
    inputs = {
        path.resolve() for path in (args.image, args.ground_mask, args.eb_values) if path
    }
    if args.output.resolve() in inputs or (
        args.save_values and args.save_values.resolve() in inputs | {args.output.resolve()}
    ):
        parser.error("Output must be different from the input files.")
    if args.newick_prefix:
        prefix = args.newick_prefix
        reports = {
            prefix.with_name(f"{prefix.name}.{ext}").resolve()
            for ext in (args.newick_format, "nwk")
        }
        occupied = inputs | {args.output.resolve()}
        if args.save_values:
            occupied.add(args.save_values.resolve())
        if reports & occupied:
            parser.error("Newick outputs must have different filenames from the other files.")

    try:
        image = read_image(args.image)
        if args.ground_mask:
            mask_image = read_image(args.ground_mask)
            if mask_image.size != image.size:
                raise ValueError("Ground mask must match the oriented input image size.")
            ground = ground_mask(mask_image, 128)
        else:
            ground = ground_mask(image, args.threshold, args.dark_ground)
        if args.despeckle:
            ground = despeckle_ground(ground, args.despeckle)
            light = ~ground if args.dark_ground else ground
            image = Image.fromarray(light.astype(np.uint8) * 255).convert("RGB")

        if args.mask_only:
            result = Image.fromarray(ground.astype(np.uint8) * 255)
        elif args.eb_values:
            values = np.load(args.eb_values, allow_pickle=False)
            if not isinstance(values, np.ndarray) or values.dtype.kind not in "fiu":
                raise ValueError("--eb-values must be a real numeric .npy array.")
            result, low, high = render_values(image, ground, values.astype(np.float64))
            print(f"Ground pixels: {ground.sum()} | EB range: {low:g} to {high:g}")
        else:
            if not ground.any():
                raise ValueError("The ground mask is empty. Check the threshold or mask.")
            if args.reuse_field:
                with np.load(args.reuse_field, allow_pickle=False) as cache:
                    if not np.array_equal(ground, cache["ground"]):
                        raise ValueError("The preprocessed image changed. Run FEB again.")
                    vectors = cache["vectors"]
                print("Cached EB field reused.")
            else:
                vectors = compute_eb(ground, args.rays)
                if args.write_field:
                    np.savez(args.write_field, ground=ground, vectors=vectors)
            values = np.clip(np.hypot(vectors[..., 0], vectors[..., 1]), 0, 1)
            result, low, high = render_values(image, ground, values, fixed_scale=True)
            if args.save_values:
                np.save(args.save_values, values)
            count = np.isfinite(values).sum()
            print(f"Defined ground pixels: {count}/{ground.sum()}")
            if count:
                print(f"FEB range: {low:.6g} to {high:.6g}")
        graph = node_types = None
        if args.graph:
            regions, centers, paths, node_types = build_eb_graph(
                ground, values, args.node_eb, None if args.eb_values else vectors,
                args.close_loops,
            )
            graph = regions, centers, paths
            print(f"EB <= {args.node_eb:g}: {len(graph[1])} nodes, {len(graph[2])} edges")
            if args.eb_values:
                print("Node types require EB vectors; the input array contains magnitudes.")
            else:
                print(f"Streets: {node_types.count('street')}")
                print("Node types: " + ", ".join(
                    f"{kind}={node_types.count(kind)}" for kind in NODE_COLORS
                ))
        if args.contours or args.highlight_eb is not None or args.vectors or args.graph:
            overlay_args = (
                result, ground, values, args.contours, args.highlight_eb,
                vectors if args.vectors else None, args.vector_step, args.vector_scale, graph,
                node_types, args.node_eb if args.graph or args.overlay_dir else None, True,
            )
            if args.overlay_dir:
                args.overlay_dir.mkdir(exist_ok=True)
                node_rows = []
                if graph is not None and node_types is not None:
                    names = graph_node_names(centers, node_types)
                    for node, name in names.items():
                        node_rows.append({"name": name, "eb": float(values[centers[node]]),
                                          "connection": "", "contact": None,
                                          "other": None, "difference": None, "pixel": ""})
                    for edge, path in enumerate(paths, start=1):
                        a, b = map(tuple, path[[0, -1]])
                        for here, there in ((a, b), (b, a)):
                            node = int(regions[here] - 1)
                            other_node = int(regions[there] - 1)
                            node_rows.append({
                                "name": names[node], "eb": float(values[centers[node]]),
                                "connection": f"E{edge} / {names[other_node]}",
                                "contact": float(values[here]), "other": float(values[there]),
                                "difference": abs(float(values[here]) - float(values[there])),
                                "pixel": f"{here[1]}, {here[0]}",
                            })
                (args.overlay_dir / "nodes.json").write_text(
                    json.dumps(node_rows, allow_nan=False), encoding="utf-8"
                )
                result.save(args.overlay_dir / "base.png")
                highlighted = False
                for layer in ("tint", "vectors", "white", "yellow", "edges", "nodes", "names"):
                    overlay, marked = draw_overlays(*overlay_args, layer=layer)
                    overlay.save(args.overlay_dir / f"{layer}.png")
                    highlighted |= marked
            else:
                result, highlighted = draw_overlays(*overlay_args)
                if node_types is not None:
                    names, _ = draw_overlays(*overlay_args, layer="names")
                    result = Image.alpha_composite(result.convert("RGBA"), names).convert("RGB")
            if args.highlight_eb is not None and not highlighted:
                print(f"No drawable contour at EB {args.highlight_eb:g}.")
        result.save(args.output)
        if args.newick_prefix:
            from feb_newick import export_newick

            # The Newick report represents the base tree when loop overlays are enabled.
            base_tree = None if args.close_loops else (regions, centers, paths, node_types)
            try:
                export_newick(ground, vectors, args.node_eb, args.newick_prefix,
                              base_tree, diagram_format=args.newick_format,
                              overlay_paths=paths if args.close_loops else None)
                print("Newick saved.")
            except ValueError as error:
                print(f"Newick unavailable: {error}")
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
