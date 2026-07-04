"""Typed parsed model of a KiCad ``.kicad_pcb`` board.

The board file is an s-expression document. Parsing is delegated to the
pure-Python `kiutils` library, which reads the KiCad 9 board format
(``version 20241229`` / ``generator_version 9.0``) used by the project's real
test boards. This module flattens kiutils' nested objects into small, stable
dataclasses and layers placement/geometry queries on top of them, so the rest
of the codebase (and the MCP tools) never depends on kiutils internals.

One kiutils asymmetry to remember when extending the parse: a footprint pad exposes
``.net`` as a ``Net(number, name)`` object, whereas tracks, vias, and zones expose
``.net`` as a plain integer net number. This module normalises both into its own
``nets`` number->name mapping so downstream code never has to special-case it.

Parsed models are cached in memory keyed by ``(absolute path, mtime)`` via
:func:`load_pcb_model`, so repeated tool calls against an unchanged file do not
reparse. This is intentionally separate from the pickle-on-disk cache used for
schematics in :mod:`kicad_mcp.config`; a board parse takes only a few seconds
and an in-process dict is sufficient.

Coordinate conventions match KiCad: millimetres, with the Y axis pointing down.
Pad positions are stored in absolute board coordinates. A footprint-local pad
offset ``(px, py)`` at footprint orientation ``a`` (degrees) maps to board
coordinates as::

    board_x = fx + px*cos(a) + py*sin(a)
    board_y = fy - px*sin(a) + py*cos(a)

This sign convention was verified empirically against both real boards: track
and via endpoints on a net coincide with the transformed pad centres of that
net (mean nearest-neighbour distance ~0.1 mm, versus ~1.4 mm for the opposite
sign).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sexpdata
from kiutils.board import Board  # type: ignore[import-untyped]


@dataclass(frozen=True)
class Point:
    """A 2D point in board millimetres (Y axis points down, KiCad-style)."""

    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Pad:
    """A footprint pad in absolute board coordinates."""

    number: str
    net_number: int | None
    net_name: str | None
    position: Point
    pad_type: str  # smd | thru_hole | np_thru_hole | connect
    shape: str | None = None
    size: Point | None = None
    rotation: float = 0.0
    drill: float | None = None
    layers: list[str] = field(default_factory=list)


@dataclass
class SilkscreenGraphic:
    """A footprint-local silkscreen graphic transformed to board coordinates."""

    kind: str  # line | arc | circle | polygon | polyline
    layer: str
    points: list[Point] = field(default_factory=list)
    width: float = 0.12
    fill: bool = False


@dataclass
class SilkscreenText:
    """A reference-designator label transformed to board coordinates."""

    text: str
    layer: str
    position: Point
    rotation: float = 0.0


@dataclass
class Footprint:
    """A placed component footprint."""

    reference: str
    value: str
    lib_id: str
    layer: str  # placement copper layer, e.g. "F.Cu" / "B.Cu"
    position: Point
    rotation: float  # degrees
    pads: list[Pad] = field(default_factory=list)
    silkscreen_graphics: list[SilkscreenGraphic] = field(default_factory=list)
    reference_text: SilkscreenText | None = None

    @property
    def side(self) -> str:
        """ "top" for front-side placement, "bottom" for back-side."""
        return "bottom" if self.layer.startswith("B.") else "top"


@dataclass
class Track:
    """A straight copper trace segment."""

    net: int | None
    layer: str
    width: float
    start: Point
    end: Point


@dataclass
class Arc:
    """A curved copper trace, defined by start/mid/end points."""

    net: int | None
    layer: str
    width: float
    start: Point
    mid: Point
    end: Point


@dataclass
class Via:
    """A plated via connecting a span of copper layers."""

    net: int | None
    position: Point
    size: float
    drill: float
    layers: list[str] = field(default_factory=list)  # layer span, e.g. [F.Cu, B.Cu]


@dataclass
class Zone:
    """A copper pour / filled zone."""

    net: int | None
    net_name: str
    layers: list[str] = field(default_factory=list)
    filled_polygon_count: int = 0
    polygons: list[list[Point]] = field(default_factory=list)


@dataclass
class StackupLayer:
    """One physical layer in the board stackup (copper, dielectric, mask, ...)."""

    name: str
    type: str | None = None
    thickness: float | None = None
    material: str | None = None
    epsilon_r: float | None = None


@dataclass(frozen=True)
class BoundingBox:
    """An axis-aligned bounding box in board millimetres."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


def _pt(position: Any) -> Point:
    """Convert a kiutils Position (with .X/.Y) into a :class:`Point`."""
    return Point(float(position.X), float(position.Y))


def _rotate_pad(px: float, py: float, angle_deg: float) -> tuple[float, float]:
    """Rotate a footprint-local pad offset by the footprint orientation.

    Uses KiCad's Y-down rotation convention (see module docstring).
    """
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return (px * c + py * s, -px * s + py * c)


def _position_relative_to_footprint(
    fx: float, fy: float, frot: float, position: Any
) -> Point:
    """Transform a footprint-local kiutils position into board coordinates."""

    dx, dy = _rotate_pad(float(position.X), float(position.Y), frot)
    return Point(fx + dx, fy + dy)


def _stroke_width(item: Any, default: float = 0.12) -> float:
    stroke = getattr(item, "stroke", None)
    width = getattr(stroke, "width", None) if stroke is not None else None
    if width is None:
        width = getattr(item, "width", None)
    return float(width if width is not None else default)


def _resolve_layer_tokens(tokens: list[str], board_layers: list[str]) -> list[str]:
    """Expand KiCad wildcard layer tokens into concrete board layer names."""

    resolved: list[str] = []

    def add(layer: str) -> None:
        if layer in board_layers and layer not in resolved:
            resolved.append(layer)

    for token in tokens:
        if token.startswith("*."):
            suffix = token[1:]
            for layer in board_layers:
                if layer.endswith(suffix):
                    add(layer)
        elif token.startswith("F&B."):
            suffix = token[3:]
            add(f"F{suffix}")
            add(f"B{suffix}")
        else:
            add(token)
    return resolved


def _pad_drill_diameter(drill: Any) -> float | None:
    """Return the visible drill diameter from kiutils pad drill data."""

    if isinstance(drill, int | float):
        return float(drill)
    diameter = getattr(drill, "diameter", None)
    if isinstance(diameter, int | float):
        return float(diameter)
    return None


def _footprint_silkscreen_graphics(
    fp: Any, fx: float, fy: float, frot: float
) -> list[SilkscreenGraphic]:
    """Collect footprint graphic items on silkscreen layers in board coordinates."""

    graphics: list[SilkscreenGraphic] = []

    def pt(position: Any) -> Point:
        return _position_relative_to_footprint(fx, fy, frot, position)

    for item in getattr(fp, "graphicItems", []) or []:
        layer = getattr(item, "layer", "")
        if layer not in {"F.SilkS", "B.SilkS"}:
            continue
        kind = type(item).__name__
        width = _stroke_width(item)
        fill = getattr(item, "fill", None) in {"yes", "solid"}
        if kind == "FpLine":
            graphics.append(
                SilkscreenGraphic(
                    kind="line",
                    layer=layer,
                    points=[pt(item.start), pt(item.end)],
                    width=width,
                )
            )
        elif kind == "FpArc":
            graphics.append(
                SilkscreenGraphic(
                    kind="arc",
                    layer=layer,
                    points=[pt(item.start), pt(item.mid), pt(item.end)],
                    width=width,
                )
            )
        elif kind == "FpCircle":
            graphics.append(
                SilkscreenGraphic(
                    kind="circle",
                    layer=layer,
                    points=[pt(item.center), pt(item.end)],
                    width=width,
                    fill=fill,
                )
            )
        elif kind == "FpRect":
            start = item.start
            end = item.end
            corners = [
                Point(float(start.X), float(start.Y)),
                Point(float(end.X), float(start.Y)),
                Point(float(end.X), float(end.Y)),
                Point(float(start.X), float(end.Y)),
            ]
            graphics.append(
                SilkscreenGraphic(
                    kind="polygon",
                    layer=layer,
                    points=[
                        Point(fx + dx, fy + dy)
                        for dx, dy in (_rotate_pad(p.x, p.y, frot) for p in corners)
                    ],
                    width=width,
                    fill=fill,
                )
            )
        elif kind == "FpPoly":
            graphics.append(
                SilkscreenGraphic(
                    kind="polygon",
                    layer=layer,
                    points=[pt(p) for p in getattr(item, "coordinates", []) or []],
                    width=width,
                    fill=fill,
                )
            )
        elif kind == "FpCurve":
            graphics.append(
                SilkscreenGraphic(
                    kind="polyline",
                    layer=layer,
                    points=[pt(p) for p in getattr(item, "coordinates", []) or []],
                    width=width,
                )
            )
    return graphics


def _sexpr_symbol_name(value: object) -> str | None:
    if isinstance(value, sexpdata.Symbol):
        return value.value()
    return None


def _sexpr_is(value: object, name: str) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and _sexpr_symbol_name(value[0]) == name
    )


def _sexpr_child(items: list[object], name: str) -> list[object] | None:
    for item in items:
        if _sexpr_is(item, name):
            return item
    return None


def _sexpr_float(items: list[object], index: int, default: float) -> float:
    try:
        return float(items[index])
    except (IndexError, TypeError, ValueError):
        return default


class PCBModel:
    """Parsed, queryable model of a single ``.kicad_pcb`` file."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        board_thickness: float | None = None,
        layers: list[str] | None = None,
        copper_layers: list[str] | None = None,
        stackup: list[StackupLayer] | None = None,
        footprints: list[Footprint] | None = None,
        tracks: list[Track] | None = None,
        arcs: list[Arc] | None = None,
        vias: list[Via] | None = None,
        zones: list[Zone] | None = None,
        nets: dict[int, str] | None = None,
        edge_points: list[tuple[float, float]] | None = None,
    ) -> None:
        self.path = path
        self.board_thickness = board_thickness
        self.layers = layers or []
        self.copper_layers = copper_layers or []
        self.stackup = stackup or []
        self.footprints = footprints or []
        self.tracks = tracks or []
        self.arcs = arcs or []
        self.vias = vias or []
        self.zones = zones or []
        self.nets = nets or {}
        self._edge_points = edge_points or []
        self._by_ref = {fp.reference: fp for fp in self.footprints}
        self._name_to_number = {name: num for num, name in self.nets.items() if name}

    # -- construction --------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path) -> PCBModel:
        """Parse a ``.kicad_pcb`` file into a :class:`PCBModel`."""
        path = Path(path)
        board = Board.from_file(str(path))
        return cls.from_board(board, path=path)

    @classmethod
    def from_board(cls, board: Board, path: Path | None = None) -> PCBModel:
        """Build a model from an already-parsed kiutils :class:`Board`."""
        # Layers & copper layers (copper layer names end in ".Cu").
        layers = [layer.name for layer in board.layers]
        copper_layers = [name for name in layers if name.endswith(".Cu")]

        # Stackup (present only when the file carries a (stackup ...) block).
        stackup: list[StackupLayer] = []
        setup = getattr(board, "setup", None)
        raw_stackup = getattr(setup, "stackup", None) if setup else None
        if raw_stackup is not None:
            for sl in raw_stackup.layers:
                stackup.append(
                    StackupLayer(
                        name=sl.name,
                        type=getattr(sl, "type", None),
                        thickness=getattr(sl, "thickness", None),
                        material=getattr(sl, "material", None),
                        epsilon_r=getattr(sl, "epsilonR", None),
                    )
                )

        board_thickness = None
        if getattr(board, "general", None) is not None:
            board_thickness = getattr(board.general, "thickness", None)

        reference_texts = cls._reference_text_specs(path) if path is not None else {}

        # Nets: number -> name.
        nets: dict[int, str] = {}
        for net in board.nets:
            nets[int(net.number)] = net.name

        # Footprints & pads (pads flattened to absolute board coordinates).
        footprints: list[Footprint] = []
        for fp in board.footprints:
            props = fp.properties or {}
            reference = props.get("Reference", "")
            value = props.get("Value", "")
            fx, fy = float(fp.position.X), float(fp.position.Y)
            frot = float(fp.position.angle or 0.0)

            pads: list[Pad] = []
            for pad in fp.pads:
                pad_position = _position_relative_to_footprint(
                    fx, fy, frot, pad.position
                )
                net_number = pad.net.number if pad.net is not None else None
                net_name = pad.net.name if pad.net is not None else None
                pad_angle = float(pad.position.angle or 0.0)
                pad_size = getattr(pad, "size", None)
                pad_drill = getattr(pad, "drill", None)
                pads.append(
                    Pad(
                        number=pad.number,
                        net_number=net_number,
                        net_name=net_name,
                        position=pad_position,
                        pad_type=pad.type,
                        shape=getattr(pad, "shape", None),
                        size=(
                            Point(float(pad_size.X), float(pad_size.Y))
                            if pad_size is not None
                            else None
                        ),
                        rotation=frot + pad_angle,
                        drill=_pad_drill_diameter(pad_drill),
                        layers=_resolve_layer_tokens(list(pad.layers or []), layers),
                    )
                )

            reference_text = reference_texts.get(reference)
            if reference_text is None and reference:
                silk_layer = "B.SilkS" if fp.layer.startswith("B.") else "F.SilkS"
                reference_text = SilkscreenText(
                    text=reference,
                    layer=silk_layer,
                    position=Point(fx, fy),
                    rotation=frot,
                )

            footprints.append(
                Footprint(
                    reference=reference,
                    value=value,
                    lib_id=fp.libId,
                    layer=fp.layer,
                    position=Point(fx, fy),
                    rotation=frot,
                    pads=pads,
                    silkscreen_graphics=_footprint_silkscreen_graphics(
                        fp, fx, fy, frot
                    ),
                    reference_text=reference_text,
                )
            )

        # Tracks / arcs / vias live together in board.traceItems.
        tracks: list[Track] = []
        arcs: list[Arc] = []
        vias: list[Via] = []
        for item in board.traceItems:
            kind = type(item).__name__
            if kind == "Segment":
                tracks.append(
                    Track(
                        net=item.net,
                        layer=item.layer,
                        width=item.width,
                        start=_pt(item.start),
                        end=_pt(item.end),
                    )
                )
            elif kind == "Arc":
                arcs.append(
                    Arc(
                        net=item.net,
                        layer=item.layer,
                        width=item.width,
                        start=_pt(item.start),
                        mid=_pt(item.mid),
                        end=_pt(item.end),
                    )
                )
            elif kind == "Via":
                vias.append(
                    Via(
                        net=item.net,
                        position=_pt(item.position),
                        size=item.size,
                        drill=item.drill,
                        layers=list(item.layers or []),
                    )
                )

        # Zones.
        zones: list[Zone] = []
        for z in board.zones:
            zlayers = list(z.layers or [])
            if not zlayers and getattr(z, "layer", None):
                zlayers = [z.layer]
            polygons: list[list[Point]] = []
            for poly in (
                getattr(z, "filledPolygons", None) or getattr(z, "polygons", []) or []
            ):
                coords = getattr(poly, "coordinates", []) or []
                points = [
                    Point(float(p.X), float(p.Y)) for p in coords if hasattr(p, "X")
                ]
                if points:
                    polygons.append(points)
            zones.append(
                Zone(
                    net=z.net,
                    net_name=z.netName or "",
                    layers=zlayers,
                    filled_polygon_count=len(z.filledPolygons or []),
                    polygons=polygons,
                )
            )

        # Board outline points from board-level Edge.Cuts graphics.
        edge_points = cls._collect_edge_points(board)

        return cls(
            path=path,
            board_thickness=board_thickness,
            layers=layers,
            copper_layers=copper_layers,
            stackup=stackup,
            footprints=footprints,
            tracks=tracks,
            arcs=arcs,
            vias=vias,
            zones=zones,
            nets=nets,
            edge_points=edge_points,
        )

    @staticmethod
    def _reference_text_specs(path: Path) -> dict[str, SilkscreenText]:
        """Read reference-designator text positions from the raw board file.

        Kiutils exposes footprint properties as a simple name/value dict, which
        is ideal for metadata but omits the `(at ...)` and `(layer ...)` fields
        needed to place labels. This narrow raw s-expression pass recovers only
        that display geometry and leaves the board model itself to kiutils.
        """

        try:
            root = sexpdata.loads(path.read_text())
        except Exception:
            return {}

        specs: dict[str, SilkscreenText] = {}
        if not isinstance(root, list):
            return specs

        for entry in root:
            if not _sexpr_is(entry, "footprint"):
                continue
            footprint_at = _sexpr_child(entry, "at")
            if footprint_at is None or len(footprint_at) < 3:
                continue
            fx = _sexpr_float(footprint_at, 1, 0.0)
            fy = _sexpr_float(footprint_at, 2, 0.0)
            frot = _sexpr_float(footprint_at, 3, 0.0)
            for child in entry:
                if not _sexpr_is(child, "property") or len(child) < 3:
                    continue
                if child[1] != "Reference" or not isinstance(child[2], str):
                    continue
                at = _sexpr_child(child, "at")
                layer_expr = _sexpr_child(child, "layer")
                if (
                    at is None
                    or len(at) < 3
                    or layer_expr is None
                    or len(layer_expr) < 2
                ):
                    continue
                dx, dy = _rotate_pad(
                    _sexpr_float(at, 1, 0.0), _sexpr_float(at, 2, 0.0), frot
                )
                specs[child[2]] = SilkscreenText(
                    text=child[2],
                    layer=str(layer_expr[1]),
                    position=Point(fx + dx, fy + dy),
                    rotation=frot + _sexpr_float(at, 3, 0.0),
                )
        return specs

    @staticmethod
    def _collect_edge_points(board: Board) -> list[tuple[float, float]]:
        """Gather board-outline vertices from board-level Edge.Cuts graphics.

        Handles the common graphic shapes (lines, arcs, rectangles, circles,
        polygons). Arc extents are approximated by their start/mid/end points,
        which is exact for the endpoints and close enough for a board-outline
        bounding box; the mid point keeps a bulging arc from being ignored.
        """
        pts: list[tuple[float, float]] = []
        for g in getattr(board, "graphicItems", []):
            if getattr(g, "layer", None) != "Edge.Cuts":
                continue
            for name in ("start", "mid", "end", "center"):
                p = getattr(g, name, None)
                if p is not None and hasattr(p, "X"):
                    pts.append((float(p.X), float(p.Y)))
            # Circle: center + end define the radius; expand to the extents.
            if type(g).__name__ == "GrCircle":
                center = getattr(g, "center", None)
                end = getattr(g, "end", None)
                if center is not None and end is not None:
                    r = math.hypot(end.X - center.X, end.Y - center.Y)
                    pts.append((center.X - r, center.Y - r))
                    pts.append((center.X + r, center.Y + r))
            # Polygon vertices, if the shape carries an explicit point list.
            for attr in ("coordinates", "points"):
                coords = getattr(g, attr, None)
                if coords:
                    for c in coords:
                        if hasattr(c, "X"):
                            pts.append((float(c.X), float(c.Y)))
        return pts

    # -- queries -------------------------------------------------------------

    def footprint(self, reference: str) -> Footprint | None:
        """Return the footprint with the given reference designator, or None."""
        return self._by_ref.get(reference)

    def net_number(self, net: object) -> int | None:
        """Resolve a net identifier (number or name) to a net number."""
        if isinstance(net, int):
            return net
        if isinstance(net, str):
            if net in self._name_to_number:
                return self._name_to_number[net]
            if net.isdigit():
                return int(net)
        return None

    def bounding_box(self) -> BoundingBox | None:
        """Board outline bounding box, or None if there is no Edge.Cuts geometry."""
        if not self._edge_points:
            return None
        xs = [p[0] for p in self._edge_points]
        ys = [p[1] for p in self._edge_points]
        return BoundingBox(min(xs), min(ys), max(xs), max(ys))

    def board_dimensions(self) -> tuple[float, float] | None:
        """(width, height) of the board outline in mm, or None if unknown."""
        bbox = self.bounding_box()
        if bbox is None:
            return None
        return (bbox.width, bbox.height)

    def footprints_near_point(
        self, x: float, y: float, radius_mm: float
    ) -> list[tuple[Footprint, float]]:
        """Footprints whose origin is within ``radius_mm`` of ``(x, y)``.

        Returned as ``(footprint, distance)`` pairs sorted nearest-first.
        """
        origin = Point(x, y)
        hits: list[tuple[Footprint, float]] = []
        for fp in self.footprints:
            d = fp.position.distance_to(origin)
            if d <= radius_mm:
                hits.append((fp, d))
        hits.sort(key=lambda pair: pair[1])
        return hits

    def footprints_near(
        self, reference: str, radius_mm: float
    ) -> list[tuple[Footprint, float]]:
        """Footprints within ``radius_mm`` of ``reference`` (excluding itself)."""
        anchor = self.footprint(reference)
        if anchor is None:
            raise KeyError(f"footprint {reference!r} not found")
        hits = self.footprints_near_point(
            anchor.position.x, anchor.position.y, radius_mm
        )
        return [(fp, d) for fp, d in hits if fp.reference != reference]

    def layer_element_counts(self) -> dict[str, dict[str, int]]:
        """Per-copper-layer counts of tracks, arcs, vias, pads, zones, footprints."""
        counts: dict[str, dict[str, int]] = {
            layer: {
                "footprints": 0,
                "tracks": 0,
                "arcs": 0,
                "vias": 0,
                "pads": 0,
                "zones": 0,
            }
            for layer in self.copper_layers
        }

        def bucket(layer: str) -> dict[str, int] | None:
            return counts.get(layer)

        for fp in self.footprints:
            b = bucket(fp.layer)
            if b:
                b["footprints"] += 1
            for pad in fp.pads:
                for layer in pad.layers:
                    pb = bucket(layer)
                    if pb:
                        pb["pads"] += 1
        for t in self.tracks:
            b = bucket(t.layer)
            if b:
                b["tracks"] += 1
        for a in self.arcs:
            b = bucket(a.layer)
            if b:
                b["arcs"] += 1
        for v in self.vias:
            for layer in v.layers:
                b = bucket(layer)
                if b:
                    b["vias"] += 1
        for z in self.zones:
            for layer in z.layers:
                b = bucket(layer)
                if b:
                    b["zones"] += 1
        return counts

    def net_copper_elements(self, net: object) -> dict[str, list]:
        """All copper elements belonging to a net (by number or name).

        Returns a dict with keys ``tracks``, ``arcs``, ``vias``, ``zones`` and
        ``pads``. An unknown net yields empty lists.
        """
        number = self.net_number(net)
        result: dict[str, list] = {
            "tracks": [],
            "arcs": [],
            "vias": [],
            "zones": [],
            "pads": [],
        }
        if number is None:
            return result
        result["tracks"] = [t for t in self.tracks if t.net == number]
        result["arcs"] = [a for a in self.arcs if a.net == number]
        result["vias"] = [v for v in self.vias if v.net == number]
        result["zones"] = [z for z in self.zones if z.net == number]
        pads: list[tuple[str, Pad]] = []
        for fp in self.footprints:
            for pad in fp.pads:
                if pad.net_number == number:
                    pads.append((fp.reference, pad))
        result["pads"] = pads
        return result

    def net_element_totals(self) -> Counter[int]:
        """Total copper-element count per net number (tracks+arcs+vias+zones+pads)."""
        totals: Counter[int] = Counter()
        for t in self.tracks:
            if t.net is not None:
                totals[t.net] += 1
        for a in self.arcs:
            if a.net is not None:
                totals[a.net] += 1
        for v in self.vias:
            if v.net is not None:
                totals[v.net] += 1
        for z in self.zones:
            if z.net is not None:
                totals[z.net] += 1
        for fp in self.footprints:
            for pad in fp.pads:
                if pad.net_number:
                    totals[pad.net_number] += 1
        return totals

    def top_nets(self, limit: int = 10) -> list[tuple[int, str, int]]:
        """Top nets by copper-element count as ``(number, name, count)`` tuples."""
        totals = self.net_element_totals()
        top = totals.most_common(limit)
        return [(num, self.nets.get(num, ""), count) for num, count in top]


# -- in-memory parse cache keyed by (absolute path, mtime) -------------------

_MODEL_CACHE: dict[str, tuple[float, PCBModel]] = {}


def load_pcb_model(path: Path) -> PCBModel:
    """Parse ``path`` into a :class:`PCBModel`, caching by (path, mtime).

    A repeated call for an unchanged file returns the cached model without
    reparsing. When the file's mtime advances, the cache entry is rebuilt.
    """
    path = Path(path)
    key = str(path.resolve())
    mtime = path.stat().st_mtime
    cached = _MODEL_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    model = PCBModel.from_file(path)
    _MODEL_CACHE[key] = (mtime, model)
    return model


def clear_pcb_cache() -> None:
    """Drop all cached parsed models (used by tests and config reloads)."""
    _MODEL_CACHE.clear()
