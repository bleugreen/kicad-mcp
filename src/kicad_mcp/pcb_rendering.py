"""2D PCB image rendering from :mod:`kicad_mcp.pcb_model` geometry.

This module intentionally renders directly from the parsed board model instead
of post-processing KiCad SVG output. Direct rendering keeps one coordinate path
for crops and net highlighting, and it allows a selected net to be colored
without relying on KiCad's SVG DOM structure.
"""

from __future__ import annotations

import base64
import math
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .pcb_model import (
    Arc,
    BoundingBox,
    Footprint,
    Pad,
    PCBModel,
    Point,
    SilkscreenGraphic,
    SilkscreenText,
    Track,
    Via,
    Zone,
)

MAX_LONG_EDGE_PX = 1600
DEFAULT_LONG_EDGE_PX = 1200
DEFAULT_MARGIN_MM = 5.0

_LAYER_COLORS: dict[str, tuple[int, int, int, int]] = {
    "F.Cu": (196, 72, 56, 255),
    "In1.Cu": (210, 150, 45, 235),
    "In2.Cu": (70, 155, 210, 235),
    "In3.Cu": (105, 170, 95, 235),
    "In4.Cu": (160, 110, 200, 235),
    "B.Cu": (55, 115, 210, 255),
    "Edge.Cuts": (28, 28, 28, 255),
    "F.SilkS": (32, 32, 32, 255),
    "B.SilkS": (32, 42, 72, 255),
}
_DIM_COLOR = (125, 125, 125, 105)
_HIGHLIGHT_COLOR = (255, 230, 45, 255)
_ZONE_ALPHA = 72


@dataclass(frozen=True)
class PCBImageResult:
    """A saved PNG and the metadata needed for an MCP image response."""

    path: str
    image_base64: str
    width: int
    height: int
    bbox: BoundingBox
    layers: list[str]
    size_bytes: int


def render_crop(
    model: PCBModel,
    *,
    reference: str | None = None,
    net: object | None = None,
    x_mm: float | None = None,
    y_mm: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    margin_mm: float = DEFAULT_MARGIN_MM,
    layers: Sequence[str] | None = None,
    width_px: int = DEFAULT_LONG_EDGE_PX,
    output_dir: str | None = None,
) -> PCBImageResult:
    """Render a cropped PCB region selected by reference, net, or explicit bbox."""

    selector_count = sum(
        [
            bool(reference),
            net is not None,
            all(v is not None for v in (x_mm, y_mm, width_mm, height_mm)),
        ]
    )
    if selector_count != 1:
        raise ValueError(
            "Select exactly one crop target: reference, net, or "
            "x_mm/y_mm/width_mm/height_mm."
        )

    bbox: BoundingBox
    selected_reference: Footprint | None = None
    if reference:
        selected_reference = model.footprint(reference)
        if selected_reference is None:
            raise ValueError(f"Component {reference} not found on the PCB")
        bbox = _expand_bbox(_footprint_bbox(selected_reference), margin_mm)
    elif net is not None:
        net_bbox = _net_bbox(model, net)
        if net_bbox is None:
            raise ValueError(f"Net {net!r} has no rendered copper elements")
        bbox = _expand_bbox(net_bbox, margin_mm)
    else:
        assert x_mm is not None and y_mm is not None
        assert width_mm is not None and height_mm is not None
        if width_mm <= 0 or height_mm <= 0:
            raise ValueError("Explicit crop width_mm and height_mm must be positive")
        bbox = BoundingBox(x_mm, y_mm, x_mm + width_mm, y_mm + height_mm)

    render_layers = _default_layers(model, layers, selected_reference)
    return _render_to_png(
        model,
        bbox=bbox,
        layers=render_layers,
        width_px=width_px,
        output_dir=output_dir,
        filename_suffix="crop",
    )


def render_highlight_net(
    model: PCBModel,
    *,
    net: object,
    x_mm: float | None = None,
    y_mm: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    layers: Sequence[str] | None = None,
    width_px: int = DEFAULT_LONG_EDGE_PX,
    output_dir: str | None = None,
) -> PCBImageResult:
    """Render a board image with one net drawn bright over dimmed copper."""

    elements = model.net_copper_elements(net)
    if not any(elements.values()):
        raise ValueError(f"Net {net!r} has no rendered copper elements")

    if any(v is not None for v in (x_mm, y_mm, width_mm, height_mm)):
        if not all(v is not None for v in (x_mm, y_mm, width_mm, height_mm)):
            raise ValueError(
                "bbox-limited highlight requires x_mm, y_mm, width_mm, and height_mm"
            )
        assert x_mm is not None and y_mm is not None
        assert width_mm is not None and height_mm is not None
        if width_mm <= 0 or height_mm <= 0:
            raise ValueError("Highlight width_mm and height_mm must be positive")
        bbox = BoundingBox(x_mm, y_mm, x_mm + width_mm, y_mm + height_mm)
    else:
        maybe_bbox = model.bounding_box() or _all_geometry_bbox(model)
        if maybe_bbox is None:
            raise ValueError("Board has no outline or rendered geometry")
        bbox = maybe_bbox

    render_layers = _default_layers(model, layers, None)
    return _render_to_png(
        model,
        bbox=bbox,
        layers=render_layers,
        width_px=width_px,
        output_dir=output_dir,
        filename_suffix="highlight",
        highlight_net=net,
    )


def _render_to_png(
    model: PCBModel,
    *,
    bbox: BoundingBox,
    layers: Sequence[str],
    width_px: int,
    output_dir: str | None,
    filename_suffix: str,
    highlight_net: object | None = None,
) -> PCBImageResult:
    if bbox.width <= 0 or bbox.height <= 0:
        raise ValueError("Render bounding box must have positive width and height")

    long_edge = min(max(1, int(width_px)), MAX_LONG_EDGE_PX)
    scale = long_edge / max(bbox.width, bbox.height)
    width = max(1, int(math.ceil(bbox.width * scale)))
    height = max(1, int(math.ceil(bbox.height * scale)))

    antialias = 3
    canvas = Image.new(
        "RGBA", (width * antialias, height * antialias), (248, 248, 244, 255)
    )
    draw = ImageDraw.Draw(canvas, "RGBA")
    transform = _Transform(bbox=bbox, scale=scale * antialias)

    layer_set = set(layers)
    highlight_number = (
        model.net_number(highlight_net) if highlight_net is not None else None
    )

    if highlight_number is None:
        _draw_board(model, draw, transform, layer_set, highlight_net=None)
    else:
        _draw_board(
            model,
            draw,
            transform,
            layer_set,
            highlight_net=-1,
            color_override=_DIM_COLOR,
        )
        _draw_board(
            model,
            draw,
            transform,
            layer_set,
            highlight_net=highlight_number,
            color_override=_HIGHLIGHT_COLOR,
            zone_alpha=_ZONE_ALPHA,
        )

    if antialias > 1:
        canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)

    out_dir = (
        Path(output_dir)
        if output_dir
        else Path(tempfile.mkdtemp(prefix="kicad_mcp_pcb_image_"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(str(model.path)).stem if model.path else "board"
    out_path = out_dir / f"{stem}-{filename_suffix}.png"
    canvas.save(out_path, "PNG")
    data = out_path.read_bytes()
    return PCBImageResult(
        path=str(out_path),
        image_base64=base64.b64encode(data).decode(),
        width=width,
        height=height,
        bbox=bbox,
        layers=list(layers),
        size_bytes=len(data),
    )


@dataclass(frozen=True)
class _Transform:
    bbox: BoundingBox
    scale: float

    def point(self, p: Point) -> tuple[float, float]:
        return (
            (p.x - self.bbox.min_x) * self.scale,
            (p.y - self.bbox.min_y) * self.scale,
        )

    def length(self, mm: float) -> float:
        return max(1.0, mm * self.scale)


def _draw_board(
    model: PCBModel,
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    layer_set: set[str],
    *,
    highlight_net: int | None,
    color_override: tuple[int, int, int, int] | None = None,
    zone_alpha: int | None = None,
) -> None:
    for zone in model.zones:
        if not _layers_intersect(zone.layers, layer_set):
            continue
        if not _net_matches(zone.net, highlight_net):
            continue
        color = color_override or _zone_layer_color(
            _first_rendered_layer(zone.layers, layer_set)
        )
        if zone_alpha is not None:
            color = (color[0], color[1], color[2], zone_alpha)
        _draw_zone(draw, transform, zone, color)

    for track in model.tracks:
        if track.layer not in layer_set or not _net_matches(track.net, highlight_net):
            continue
        color = color_override or _layer_color(track.layer)
        draw.line(
            [transform.point(track.start), transform.point(track.end)],
            fill=color,
            width=round(transform.length(track.width)),
        )

    for arc in model.arcs:
        if arc.layer not in layer_set or not _net_matches(arc.net, highlight_net):
            continue
        color = color_override or _layer_color(arc.layer)
        points = [transform.point(p) for p in _arc_points(arc)]
        if len(points) > 1:
            draw.line(points, fill=color, width=round(transform.length(arc.width)))

    for via in model.vias:
        if not _layers_intersect(via.layers, layer_set) or not _net_matches(
            via.net, highlight_net
        ):
            continue
        color = color_override or (155, 115, 60, 255)
        _draw_via(draw, transform, via, color)

    for fp in model.footprints:
        for pad in fp.pads:
            if not _pad_on_layers(pad, layer_set) or not _net_matches(
                pad.net_number, highlight_net
            ):
                continue
            layer = _first_rendered_layer(pad.layers, layer_set)
            color = color_override or _layer_color(layer)
            _draw_pad(draw, transform, pad, color)

    if highlight_net is None:
        for fp in model.footprints:
            for graphic in fp.silkscreen_graphics:
                if graphic.layer in layer_set:
                    _draw_silkscreen_graphic(draw, transform, graphic)
            if fp.reference_text is not None and fp.reference_text.layer in layer_set:
                _draw_silkscreen_text(draw, transform, fp.reference_text)

    if "Edge.Cuts" in layer_set and highlight_net is None:
        _draw_edge_box(model, draw, transform)


def _net_matches(net: int | None, highlight_net: int | None) -> bool:
    if highlight_net is None:
        return True
    if highlight_net == -1:
        return True
    return net == highlight_net


def _draw_zone(
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    zone: Zone,
    color: tuple[int, int, int, int],
) -> None:
    for polygon in zone.polygons:
        points = [transform.point(p) for p in polygon]
        if len(points) >= 3:
            draw.polygon(points, fill=color)


def _draw_via(
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    via: Via,
    color: tuple[int, int, int, int],
) -> None:
    cx, cy = transform.point(via.position)
    r = transform.length(via.size / 2.0)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    if via.drill > 0:
        dr = transform.length(via.drill / 2.0)
        draw.ellipse((cx - dr, cy - dr, cx + dr, cy + dr), fill=(248, 248, 244, 230))


def _draw_pad(
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    pad: Pad,
    color: tuple[int, int, int, int],
) -> None:
    size = pad.size or Point(0.8, 0.8)
    cx, cy = transform.point(pad.position)
    w = transform.length(size.x)
    h = transform.length(size.y)
    shape = (pad.shape or "rect").lower()
    if shape in {"circle", "oval"} and abs(w - h) < 1.0:
        draw.ellipse((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), fill=color)
    elif shape == "circle" or (shape == "oval" and abs(pad.rotation) < 1e-6):
        draw.ellipse((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), fill=color)
    else:
        points = _rotated_rect_points(cx, cy, w, h, -pad.rotation)
        draw.polygon(points, fill=color)


def _draw_silkscreen_graphic(
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    graphic: SilkscreenGraphic,
) -> None:
    color = _layer_color(graphic.layer)
    width = round(transform.length(graphic.width))
    points = [transform.point(p) for p in graphic.points]
    if graphic.kind == "line" and len(points) == 2:
        draw.line(points, fill=color, width=width)
    elif graphic.kind == "polyline" and len(points) >= 2:
        draw.line(points, fill=color, width=width)
    elif graphic.kind == "polygon" and len(points) >= 3:
        if graphic.fill:
            draw.polygon(points, fill=color)
        draw.line([*points, points[0]], fill=color, width=width)
    elif graphic.kind == "circle" and len(graphic.points) == 2:
        center = graphic.points[0]
        edge = graphic.points[1]
        cx, cy = transform.point(center)
        radius = transform.length(center.distance_to(edge))
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        if graphic.fill:
            draw.ellipse(box, fill=color)
        draw.ellipse(box, outline=color, width=width)
    elif graphic.kind == "arc" and len(graphic.points) == 3:
        arc_points = [transform.point(p) for p in _arc_points_from_three(*graphic.points)]
        if len(arc_points) > 1:
            draw.line(arc_points, fill=color, width=width)


def _draw_silkscreen_text(
    draw: ImageDraw.ImageDraw,
    transform: _Transform,
    text: SilkscreenText,
) -> None:
    x, y = transform.point(text.position)
    color = _layer_color(text.layer)
    font = ImageFont.load_default()
    draw.text((x, y), text.text, fill=color, font=font, anchor="mm")


def _draw_edge_box(
    model: PCBModel, draw: ImageDraw.ImageDraw, transform: _Transform
) -> None:
    bbox = model.bounding_box()
    if bbox is None:
        return
    p1 = transform.point(Point(bbox.min_x, bbox.min_y))
    p2 = transform.point(Point(bbox.max_x, bbox.max_y))
    width = max(1, round(transform.length(0.12)))
    draw.rectangle((*p1, *p2), outline=_LAYER_COLORS["Edge.Cuts"], width=width)


def _arc_points(arc: Arc, steps: int = 48) -> list[Point]:
    return _arc_points_from_three(arc.start, arc.mid, arc.end, steps=steps)


def _arc_points_from_three(
    start: Point, mid: Point, end: Point, steps: int = 48
) -> list[Point]:
    circle = _circle_from_points(start, mid, end)
    if circle is None:
        return [start, mid, end]
    cx, cy, radius = circle
    a0 = math.atan2(start.y - cy, start.x - cx)
    am = math.atan2(mid.y - cy, mid.x - cx)
    a1 = math.atan2(end.y - cy, end.x - cx)
    ccw_delta = (a1 - a0) % (2 * math.pi)
    ccw_mid = (am - a0) % (2 * math.pi)
    if ccw_mid <= ccw_delta:
        delta = ccw_delta
    else:
        delta = -((a0 - a1) % (2 * math.pi))
    n = max(4, min(steps, int(abs(delta) * radius / 0.25)))
    return [
        Point(
            cx + radius * math.cos(a0 + delta * i / n),
            cy + radius * math.sin(a0 + delta * i / n),
        )
        for i in range(n + 1)
    ]


def _circle_from_points(
    a: Point, b: Point, c: Point
) -> tuple[float, float, float] | None:
    d = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y))
    if abs(d) < 1e-9:
        return None
    ux = (
        (a.x * a.x + a.y * a.y) * (b.y - c.y)
        + (b.x * b.x + b.y * b.y) * (c.y - a.y)
        + (c.x * c.x + c.y * c.y) * (a.y - b.y)
    ) / d
    uy = (
        (a.x * a.x + a.y * a.y) * (c.x - b.x)
        + (b.x * b.x + b.y * b.y) * (a.x - c.x)
        + (c.x * c.x + c.y * c.y) * (b.x - a.x)
    ) / d
    return ux, uy, math.hypot(a.x - ux, a.y - uy)


def _rotated_rect_points(
    cx: float, cy: float, width: float, height: float, angle_deg: float
) -> list[tuple[float, float]]:
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    corners = [
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ]
    return [
        (cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a) for x, y in corners
    ]


def _default_layers(
    model: PCBModel, layers: Sequence[str] | None, reference: Footprint | None
) -> list[str]:
    if layers:
        return [str(layer) for layer in layers]
    if reference is not None:
        copper = reference.layer if reference.layer in model.copper_layers else "F.Cu"
        silk = "B.SilkS" if copper.startswith("B.") else "F.SilkS"
        return [copper, silk, "Edge.Cuts"]
    return [*model.copper_layers, "Edge.Cuts"]


def _layer_color(layer: str | None) -> tuple[int, int, int, int]:
    if layer is None:
        return (90, 90, 90, 255)
    return _LAYER_COLORS.get(layer, (90, 90, 90, 220))


def _zone_layer_color(layer: str | None) -> tuple[int, int, int, int]:
    color = _layer_color(layer)
    # Normal crop rendering draws pads and tracks over zones. Lightening and
    # reducing saturation keeps same-layer copper visually related without
    # letting a pour erase pads that sit inside it.
    r, g, b, a = color
    gray = round((r + g + b) / 3)
    blend = 0.55
    desaturated = (
        round(r * (1 - blend) + gray * blend),
        round(g * (1 - blend) + gray * blend),
        round(b * (1 - blend) + gray * blend),
    )
    lightened = tuple(round(channel * 0.72 + 255 * 0.28) for channel in desaturated)
    return (lightened[0], lightened[1], lightened[2], min(a, 205))


def _layers_intersect(element_layers: Iterable[str], rendered_layers: set[str]) -> bool:
    layers = set(element_layers)
    return bool(layers & rendered_layers) or (
        "*.Cu" in layers and any(layer.endswith(".Cu") for layer in rendered_layers)
    )


def _pad_on_layers(pad: Pad, rendered_layers: set[str]) -> bool:
    return _layers_intersect(pad.layers, rendered_layers)


def _first_rendered_layer(
    element_layers: Iterable[str], rendered_layers: set[str]
) -> str | None:
    for layer in element_layers:
        if layer in rendered_layers:
            return layer
    if "*.Cu" in set(element_layers):
        for layer in rendered_layers:
            if layer.endswith(".Cu"):
                return layer
    return None


def _expand_bbox(bbox: BoundingBox, margin: float) -> BoundingBox:
    margin = max(0.0, float(margin))
    return BoundingBox(
        bbox.min_x - margin,
        bbox.min_y - margin,
        bbox.max_x + margin,
        bbox.max_y + margin,
    )


def _footprint_bbox(fp: Footprint) -> BoundingBox:
    boxes = [_pad_bbox(pad) for pad in fp.pads]
    if not boxes:
        p = fp.position
        boxes = [BoundingBox(p.x - 1.0, p.y - 1.0, p.x + 1.0, p.y + 1.0)]
    return _union_boxes(boxes)


def _net_bbox(model: PCBModel, net: object) -> BoundingBox | None:
    elements = model.net_copper_elements(net)
    boxes: list[BoundingBox] = []
    boxes.extend(_track_bbox(t) for t in elements["tracks"])
    boxes.extend(_arc_bbox(a) for a in elements["arcs"])
    boxes.extend(_via_bbox(v) for v in elements["vias"])
    for zone in elements["zones"]:
        zone_bbox = _zone_bbox(zone)
        if zone_bbox is not None:
            boxes.append(zone_bbox)
    boxes.extend(_pad_bbox(pad) for _, pad in elements["pads"])
    return _union_boxes(boxes) if boxes else None


def _all_geometry_bbox(model: PCBModel) -> BoundingBox | None:
    boxes: list[BoundingBox] = []
    boxes.extend(_footprint_bbox(fp) for fp in model.footprints)
    boxes.extend(_track_bbox(t) for t in model.tracks)
    boxes.extend(_arc_bbox(a) for a in model.arcs)
    boxes.extend(_via_bbox(v) for v in model.vias)
    for zone in model.zones:
        zone_bbox = _zone_bbox(zone)
        if zone_bbox is not None:
            boxes.append(zone_bbox)
    return _union_boxes(boxes) if boxes else None


def _track_bbox(track: Track) -> BoundingBox:
    r = track.width / 2.0
    return BoundingBox(
        min(track.start.x, track.end.x) - r,
        min(track.start.y, track.end.y) - r,
        max(track.start.x, track.end.x) + r,
        max(track.start.y, track.end.y) + r,
    )


def _arc_bbox(arc: Arc) -> BoundingBox:
    r = arc.width / 2.0
    points = _arc_points(arc)
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return BoundingBox(min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def _via_bbox(via: Via) -> BoundingBox:
    r = via.size / 2.0
    p = via.position
    return BoundingBox(p.x - r, p.y - r, p.x + r, p.y + r)


def _pad_bbox(pad: Pad) -> BoundingBox:
    size = pad.size or Point(0.8, 0.8)
    p = pad.position
    # Rotation can expand the axis-aligned bounds; using the half diagonal is
    # conservative and keeps target crops from clipping rotated pads.
    r = math.hypot(size.x, size.y) / 2.0
    return BoundingBox(p.x - r, p.y - r, p.x + r, p.y + r)


def _zone_bbox(zone: Zone) -> BoundingBox | None:
    points = [p for polygon in zone.polygons for p in polygon]
    if not points:
        return None
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def _union_boxes(boxes: Sequence[BoundingBox]) -> BoundingBox:
    return BoundingBox(
        min(b.min_x for b in boxes),
        min(b.min_y for b in boxes),
        max(b.max_x for b in boxes),
        max(b.max_y for b in boxes),
    )


def result_caption(title: str, result: PCBImageResult) -> str:
    """Format shared Markdown metadata for server image responses."""

    b = result.bbox
    bbox_text = (
        f"({b.min_x:.3f}, {b.min_y:.3f}) to "
        f"({b.max_x:.3f}, {b.max_y:.3f}) mm"
    )
    return (
        f"# {title}\n\n"
        f"**Saved to:** {result.path}\n"
        f"**Size:** {result.width}x{result.height} px, {result.size_bytes} bytes\n"
        f"**BBox:** {bbox_text}\n"
        f"**Layers:** {', '.join(result.layers)}\n"
    )


def as_dict(result: PCBImageResult) -> dict[str, Any]:
    """Expose a rendering result as a plain dict for tests and callers."""

    return {
        "path": result.path,
        "image_base64": result.image_base64,
        "width": result.width,
        "height": result.height,
        "size_bytes": result.size_bytes,
        "layers": result.layers,
        "bbox": {
            "min_x": result.bbox.min_x,
            "min_y": result.bbox.min_y,
            "max_x": result.bbox.max_x,
            "max_y": result.bbox.max_y,
        },
    }
