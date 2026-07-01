"""Copper route analysis for parsed KiCad PCB models.

The graph uses physical copper attachment points as nodes: track and arc ends,
via positions on each spanned layer, and pad centres on their copper layers.
Straight tracks and arcs are measured as routed copper length. Vias are treated
as zero-length layer-transition edges: this keeps reported route length equal to
planar copper length and avoids pretending board thickness is a routed trace
length. Zones are deliberately excluded from length math because their current
model records presence and layers, not pour polygon topology.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .pcb_model import Arc, Pad, PCBModel, Point, Track, Via, Zone

JOIN_TOLERANCE_MM = 0.15


@dataclass
class RouteIsland:
    """One connected measured-copper island for a net."""

    node_ids: set[int] = field(default_factory=set)
    pads: list[str] = field(default_factory=list)
    tracks: int = 0
    arcs: int = 0
    vias: int = 0
    layers: set[str] = field(default_factory=set)

    def summary(self) -> str:
        parts: list[str] = []
        if self.pads:
            parts.append("pads " + ", ".join(sorted(self.pads)[:6]))
            if len(self.pads) > 6:
                parts[-1] += f", +{len(self.pads) - 6} more"
        counts = []
        if self.tracks:
            counts.append(f"{self.tracks} track(s)")
        if self.arcs:
            counts.append(f"{self.arcs} arc(s)")
        if self.vias:
            counts.append(f"{self.vias} via(s)")
        if counts:
            parts.append(", ".join(counts))
        if self.layers:
            parts.append("layers " + ", ".join(sorted(self.layers)))
        return "; ".join(parts) if parts else "empty"


@dataclass
class RouteAnalysis:
    """Computed routing facts for one net."""

    net_number: int
    net_name: str
    total_length_mm: float
    layer_lengths_mm: dict[str, float]
    track_count: int
    arc_count: int
    via_count: int
    via_spans: dict[str, int]
    width_lengths_mm: dict[float, float]
    min_width_mm: float | None
    max_width_mm: float | None
    layers_used: list[str]
    endpoints: list[str]
    copper_island_count: int
    connected_only_through_zone: bool
    zones: list[Zone]
    islands: list[RouteIsland]
    tolerance_mm: float = JOIN_TOLERANCE_MM


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: int) -> int:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, a: int, b: int) -> None:
        self.add(a)
        self.add(b)
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass(frozen=True)
class _Node:
    point: Point
    layer: str
    kind: str
    label: str


def analyze_net_route(
    model: PCBModel, net: object, tolerance_mm: float = JOIN_TOLERANCE_MM
) -> RouteAnalysis:
    """Build a route graph for ``net`` and return measured routing facts."""

    net_number = model.net_number(net)
    if net_number is None or net_number not in model.nets:
        raise ValueError(f"Net not found: {net}")

    elems = model.net_copper_elements(net_number)
    tracks: list[Track] = elems["tracks"]
    arcs: list[Arc] = elems["arcs"]
    vias: list[Via] = elems["vias"]
    zones: list[Zone] = elems["zones"]
    pads: list[tuple[str, Pad]] = elems["pads"]

    nodes: list[_Node] = []
    uf = _UnionFind()
    element_roots: dict[str, list[int]] = defaultdict(list)

    def add_node(point: Point, layer: str, kind: str, label: str) -> int:
        node_id = len(nodes)
        nodes.append(_Node(point=point, layer=layer, kind=kind, label=label))
        uf.add(node_id)
        return node_id

    layer_lengths: dict[str, float] = defaultdict(float)
    width_lengths: dict[float, float] = defaultdict(float)
    widths: list[float] = []

    for idx, track in enumerate(tracks, 1):
        a = add_node(track.start, track.layer, "track", f"track {idx}")
        b = add_node(track.end, track.layer, "track", f"track {idx}")
        uf.union(a, b)
        element_roots[f"track:{idx}"].extend([a, b])
        length = track.start.distance_to(track.end)
        layer_lengths[track.layer] += length
        width_lengths[track.width] += length
        widths.append(track.width)

    for idx, arc in enumerate(arcs, 1):
        a = add_node(arc.start, arc.layer, "arc", f"arc {idx}")
        b = add_node(arc.end, arc.layer, "arc", f"arc {idx}")
        uf.union(a, b)
        element_roots[f"arc:{idx}"].extend([a, b])
        length = arc_length(arc)
        layer_lengths[arc.layer] += length
        width_lengths[arc.width] += length
        widths.append(arc.width)

    via_spans: dict[str, int] = defaultdict(int)
    for idx, via in enumerate(vias, 1):
        via_node_ids = [
            add_node(via.position, layer, "via", f"via {idx}") for layer in via.layers
        ]
        for first, other in zip(via_node_ids, via_node_ids[1:], strict=False):
            uf.union(first, other)
        element_roots[f"via:{idx}"].extend(via_node_ids)
        via_spans[" ↔ ".join(via.layers)] += 1

    endpoints: list[str] = []
    for ref, pad in pads:
        label = f"{ref}.{pad.number}"
        endpoints.append(label)
        pad_layers = copper_pad_layers(model, pad)
        ids = [add_node(pad.position, layer, "pad", label) for layer in pad_layers]
        for first, other in zip(ids, ids[1:], strict=False):
            uf.union(first, other)
        element_roots[f"pad:{label}"].extend(ids)

    # Coincident endpoints join only when they are on the same copper layer.
    by_layer: dict[str, list[int]] = defaultdict(list)
    for node_id, node in enumerate(nodes):
        by_layer[node.layer].append(node_id)

    for layer_node_ids in by_layer.values():
        for i, a in enumerate(layer_node_ids):
            pa = nodes[a].point
            for b in layer_node_ids[i + 1 :]:
                if pa.distance_to(nodes[b].point) <= tolerance_mm:
                    uf.union(a, b)

    islands_by_root: dict[int, RouteIsland] = {}
    for node_id, node in enumerate(nodes):
        root = uf.find(node_id)
        island = islands_by_root.setdefault(root, RouteIsland())
        island.node_ids.add(node_id)
        island.layers.add(node.layer)
        if node.kind == "pad" and node.label not in island.pads:
            island.pads.append(node.label)

    def bump(kind: str, count_attr: str) -> None:
        for key, roots in element_roots.items():
            if not key.startswith(kind + ":") or not roots:
                continue
            root = uf.find(roots[0])
            current = getattr(islands_by_root[root], count_attr)
            setattr(islands_by_root[root], count_attr, current + 1)

    bump("track", "tracks")
    bump("arc", "arcs")
    bump("via", "vias")

    islands = sorted(
        islands_by_root.values(),
        key=lambda island: (not island.pads, sorted(island.pads), -island.tracks),
    )
    island_layers = (
        set().union(*(island.layers for island in islands)) if islands else set()
    )
    via_layers = {layer for via in vias for layer in via.layers}
    layers_used = sorted(set(layer_lengths) | via_layers | island_layers)
    copper_islands = len(islands)
    connected_only_through_zone = copper_islands > 1 and bool(zones)

    return RouteAnalysis(
        net_number=net_number,
        net_name=model.nets.get(net_number, ""),
        total_length_mm=sum(layer_lengths.values()),
        layer_lengths_mm=dict(sorted(layer_lengths.items())),
        track_count=len(tracks),
        arc_count=len(arcs),
        via_count=len(vias),
        via_spans=dict(sorted(via_spans.items())),
        width_lengths_mm=dict(sorted(width_lengths.items())),
        min_width_mm=min(widths) if widths else None,
        max_width_mm=max(widths) if widths else None,
        layers_used=layers_used,
        endpoints=sorted(endpoints),
        copper_island_count=copper_islands,
        connected_only_through_zone=connected_only_through_zone,
        zones=zones,
        islands=islands,
        tolerance_mm=tolerance_mm,
    )


def copper_pad_layers(model: PCBModel, pad: Pad) -> list[str]:
    """Return the copper layers occupied by a pad."""

    layers = [layer for layer in pad.layers if layer.endswith(".Cu")]
    if layers:
        return layers
    if "*.Cu" in pad.layers or pad.pad_type in {"thru_hole", "np_thru_hole"}:
        return list(model.copper_layers)
    return []


def arc_length(arc: Arc) -> float:
    """Return true circular-arc length for a KiCad start/mid/end arc."""

    center = _circle_center(arc.start, arc.mid, arc.end)
    if center is None:
        return arc.start.distance_to(arc.end)
    cx, cy = center
    radius = math.hypot(arc.start.x - cx, arc.start.y - cy)
    if radius == 0:
        return 0.0
    a0 = math.atan2(arc.start.y - cy, arc.start.x - cx)
    am = math.atan2(arc.mid.y - cy, arc.mid.x - cx)
    a1 = math.atan2(arc.end.y - cy, arc.end.x - cx)
    ccw = _angle_contains(a0, a1, am, ccw=True)
    delta = (a1 - a0) % (2 * math.pi) if ccw else (a0 - a1) % (2 * math.pi)
    return radius * delta


def _circle_center(a: Point, b: Point, c: Point) -> tuple[float, float] | None:
    det = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y))
    if abs(det) < 1e-12:
        return None
    a2 = a.x * a.x + a.y * a.y
    b2 = b.x * b.x + b.y * b.y
    c2 = c.x * c.x + c.y * c.y
    ux = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / det
    uy = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / det
    return (ux, uy)


def _angle_contains(start: float, end: float, middle: float, *, ccw: bool) -> bool:
    if ccw:
        return ((middle - start) % (2 * math.pi)) <= ((end - start) % (2 * math.pi))
    return ((start - middle) % (2 * math.pi)) <= ((start - end) % (2 * math.pi))


def matching_net_names(model: PCBModel, pattern: str, limit: int = 50) -> list[str]:
    """Return net names matching a glob or regular expression."""

    names = sorted(name for name in model.nets.values() if name)
    regex = None
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = None
    matches = [
        name
        for name in names
        if fnmatch.fnmatchcase(name, pattern)
        or (regex is not None and regex.search(name))
    ]
    return matches[:limit]


def resolve_diff_pair(
    model: PCBModel, net_p: str, net_n: str | None = None
) -> tuple[str, str]:
    """Resolve explicit or base-name differential-pair arguments."""

    if net_n:
        _require_net(model, net_p)
        _require_net(model, net_n)
        return net_p, net_n

    candidates: list[tuple[str, str]] = []
    bases = [net_p]
    if net_p.endswith("_P"):
        bases.append(net_p[:-2])
    if net_p.endswith("_N"):
        bases.append(net_p[:-2])
    if net_p.endswith("+") or net_p.endswith("-"):
        bases.append(net_p[:-1])

    for base in dict.fromkeys(bases):
        candidates.extend(
            [
                (base + "_P", base + "_N"),
                (base + "+", base + "-"),
                (base + "P", base + "N"),
            ]
        )

    for p_name, n_name in candidates:
        p_exists = model.net_number(p_name) is not None
        n_exists = model.net_number(n_name) is not None
        if p_exists and n_exists:
            return p_name, n_name

    raise ValueError(
        f"Could not resolve differential pair from {net_p!r}; "
        "pass explicit net_p and net_n"
    )


def _require_net(model: PCBModel, name: str) -> None:
    if model.net_number(name) is None:
        raise ValueError(f"Net not found: {name}")


def sorted_length_rows(
    model: PCBModel, pattern: str, limit: int = 50
) -> list[RouteAnalysis]:
    """Analyze all matching nets and return them sorted by length descending."""

    analyses = [
        analyze_net_route(model, name)
        for name in matching_net_names(model, pattern, limit)
    ]
    return sorted(analyses, key=lambda item: item.total_length_mm, reverse=True)
