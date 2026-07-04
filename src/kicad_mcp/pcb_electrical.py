"""Derived electrical estimates over parsed PCB route geometry.

These helpers intentionally report assumptions instead of hiding defaults. The
ampacity estimates are chart/formula fits, and impedance estimates are IPC-2141
closed-form approximations rather than thermal or field simulation.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .pcb_model import PCBModel, Point, StackupLayer, Track, Via
from .pcb_route import (
    RouteAnalysis,
    analyze_net_route,
    matching_net_names,
    resolve_diff_pair,
)

MM_TO_MIL = 39.3700787402
DEFAULT_COPPER_MM = 0.035
DEFAULT_PLATING_UM = 25.0
DEFAULT_ER = 4.4

# Digitized conservative/universal IPC-2152 chart points, reproduced in vendor
# calculator documentation such as Sierra Circuits' trace-width/current tables.
# Values are intentionally conservative relative to the IPC-2221 external curve.
_IPC2152_DELTAS: list[float] = [10.0, 20.0, 30.0, 45.0, 60.0, 75.0, 100.0]
_IPC2152_AREAS: list[float] = [10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0]
_IPC2152_GRID: dict[float, list[float]] = {
    area: [
        0.85 * ipc for ipc in [0.048 * dt**0.44 * area**0.725 for dt in _IPC2152_DELTAS]
    ]
    for area in _IPC2152_AREAS
}


@dataclass(frozen=True)
class CapacitySegment:
    layer: str
    width_mm: float
    length_mm: float
    copper_thickness_mm: float
    area_mil2: float
    ipc2152_a: float | None
    ipc2221_a: float
    estimated_a: float
    standard: str
    internal: bool


@dataclass(frozen=True)
class ViaCapacity:
    span: str
    count: int
    per_via_a: float
    total_a_upper_bound: float


@dataclass
class NetCapacityReport:
    net_name: str
    segments: list[CapacitySegment]
    neck: CapacitySegment | None
    via_limits: list[ViaCapacity]
    assumptions: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    total_length_mm: float = 0.0


@dataclass(frozen=True)
class ImpedanceRow:
    net_name: str
    layer: str
    width_mm: float
    length_mm: float
    z0_ohm: float | None
    model: str
    dielectric_h_mm: float | None
    er: float | None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiffCoupledRow:
    layer: str
    spacing_center_mm: float
    gap_mm: float
    zdiff_ohm: float
    notes: list[str] = field(default_factory=list)


@dataclass
class ImpedanceReport:
    rows: list[ImpedanceRow]
    assumptions: list[str] = field(default_factory=list)
    coupled_rows: list[DiffCoupledRow] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def ipc2221_max_current(area_mil2: float, delta_t_c: float, internal: bool) -> float:
    k = 0.024 if internal else 0.048
    return float(k * delta_t_c**0.44 * area_mil2**0.725)


def ipc2152_max_current(area_mil2: float, delta_t_c: float) -> float | None:
    if not (_IPC2152_AREAS[0] <= area_mil2 <= _IPC2152_AREAS[-1]):
        return None
    if not (_IPC2152_DELTAS[0] <= delta_t_c <= _IPC2152_DELTAS[-1]):
        return None
    lo_a, hi_a = _bracket(_IPC2152_AREAS, area_mil2)
    lo_t, hi_t = _bracket(_IPC2152_DELTAS, delta_t_c)
    if lo_a == hi_a and lo_t == hi_t:
        return _IPC2152_GRID[lo_a][_IPC2152_DELTAS.index(lo_t)]

    def log_i(area: float, dt: float) -> float:
        return math.log(_IPC2152_GRID[area][_IPC2152_DELTAS.index(dt)])

    la = math.log(area_mil2)
    lt = math.log(delta_t_c)
    la0, la1 = math.log(lo_a), math.log(hi_a)
    lt0, lt1 = math.log(lo_t), math.log(hi_t)
    wa = 0.0 if la0 == la1 else (la - la0) / (la1 - la0)
    wt = 0.0 if lt0 == lt1 else (lt - lt0) / (lt1 - lt0)
    v00 = log_i(lo_a, lo_t)
    v01 = log_i(lo_a, hi_t)
    v10 = log_i(hi_a, lo_t)
    v11 = log_i(hi_a, hi_t)
    return math.exp(
        (1 - wa) * (1 - wt) * v00
        + (1 - wa) * wt * v01
        + wa * (1 - wt) * v10
        + wa * wt * v11
    )


def copper_thickness_mm(model: PCBModel, layer: str, assumptions: list[str]) -> float:
    for stack_layer in model.stackup:
        if stack_layer.name == layer and stack_layer.thickness is not None:
            return stack_layer.thickness
    assumptions.append(
        f"copper thickness assumed 35 µm (1 oz): stackup has no thickness for {layer}"
    )
    return DEFAULT_COPPER_MM


def via_max_current(
    via: Via, delta_t_c: float, plating_um: float = DEFAULT_PLATING_UM
) -> float:
    plating_mm = plating_um / 1000.0
    area_mil2 = math.pi * via.drill * plating_mm * MM_TO_MIL * MM_TO_MIL
    return ipc2221_max_current(area_mil2, delta_t_c, internal=True)


def net_current_capacity(
    model: PCBModel,
    route: RouteAnalysis,
    temp_rise_c: float = 10.0,
    plating_um: float = DEFAULT_PLATING_UM,
) -> NetCapacityReport:
    assumptions: list[str] = [f"via plating assumed {plating_um:g} µm"]
    seen_assumptions: set[str] = set()
    segments: list[CapacitySegment] = []
    for (layer, width), length in route.layer_width_lengths_mm.items():
        local_assumptions: list[str] = []
        thickness = copper_thickness_mm(model, layer, local_assumptions)
        for assumption in local_assumptions:
            if assumption not in seen_assumptions:
                assumptions.append(assumption)
                seen_assumptions.add(assumption)
        area_mil2 = width * thickness * MM_TO_MIL * MM_TO_MIL
        internal = not _is_external_copper(layer)
        i2221 = ipc2221_max_current(area_mil2, temp_rise_c, internal=internal)
        i2152 = ipc2152_max_current(area_mil2, temp_rise_c)
        estimated = i2152 if i2152 is not None else i2221
        standard = "IPC-2152 conservative" if i2152 is not None else "IPC-2221"
        segments.append(
            CapacitySegment(
                layer,
                width,
                length,
                thickness,
                area_mil2,
                i2152,
                i2221,
                estimated,
                standard,
                internal,
            )
        )
    neck = min(segments, key=lambda item: item.estimated_a) if segments else None
    elems = model.net_copper_elements(route.net_number)
    vias = elems["vias"]
    per_span: dict[str, list[float]] = {}
    for via in vias:
        span = " ↔ ".join(via.layers)
        per_span.setdefault(span, []).append(
            via_max_current(via, temp_rise_c, plating_um)
        )
    via_limits = [
        ViaCapacity(span, len(values), min(values), min(values) * len(values))
        for span, values in sorted(per_span.items())
    ]
    flags: list[str] = []
    if route.zones or route.connected_only_through_zone:
        flags.append(
            "pour-carried: track neck may not be the true limit; "
            "pour cross-section is not modeled"
        )
    if any("copper thickness assumed" in item for item in assumptions):
        flags.append("assumed-copper")
    return NetCapacityReport(
        route.net_name,
        segments,
        neck,
        via_limits,
        assumptions,
        flags,
        route.total_length_mm,
    )


def capacity_reports_for_pattern(
    model: PCBModel,
    pattern: str,
    temp_rise_c: float = 10.0,
    plating_um: float = DEFAULT_PLATING_UM,
    limit: int = 50,
) -> list[NetCapacityReport]:
    reports = [
        net_current_capacity(
            model, analyze_net_route(model, name), temp_rise_c, plating_um
        )
        for name in matching_net_names(model, pattern, limit)
    ]
    return sorted(
        reports, key=lambda report: report.neck.estimated_a if report.neck else math.inf
    )


def microstrip_z0(w: float, t: float, h: float, er: float) -> float:
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def stripline_z0(w: float, t: float, h: float, er: float) -> float:
    return 60.0 / math.sqrt(er) * math.log(4.0 * h / (0.67 * math.pi * (0.8 * w + t)))


def coupled_differential(z0: float, s: float, h: float, *, microstrip: bool) -> float:
    if microstrip:
        return 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * s / h))
    return 2.0 * z0 * (1.0 - 0.347 * math.exp(-2.9 * s / h))


def dielectric_height(
    model: PCBModel,
    layer: str,
    assumptions: list[str],
    er_override: float | None = None,
    dielectric_h_mm: float | None = None,
) -> tuple[float, float]:
    if dielectric_h_mm is not None:
        er = er_override if er_override is not None else DEFAULT_ER
        if er_override is None:
            assumptions.append(
                "εr assumed 4.4: explicit dielectric height supplied without εr"
            )
        return dielectric_h_mm, er
    if er_override is not None:
        assumptions.append(f"εr overridden by tool parameter: {er_override:g}")
    if model.stackup:
        return _height_from_stackup(model.stackup, layer, assumptions, er_override)
    if len(model.copper_layers) == 2 and model.board_thickness is not None:
        assumptions.append(
            "dielectric height derived from 2-layer board thickness minus "
            "two 35 µm copper foils"
        )
        er = er_override if er_override is not None else DEFAULT_ER
        if er_override is None:
            assumptions.append("εr assumed 4.4: no stackup dielectric data")
        return max(model.board_thickness - 2 * DEFAULT_COPPER_MM, 0.0), er
    raise ValueError(
        f"Cannot estimate impedance on {layer}: missing stackup dielectric "
        "thickness and no 2-layer board thickness fallback"
    )


def net_impedance(
    model: PCBModel,
    route: RouteAnalysis,
    er: float | None = None,
    dielectric_h_mm: float | None = None,
) -> ImpedanceReport:
    assumptions: list[str] = []
    rows: list[ImpedanceRow] = []
    for (layer, width), length in route.layer_width_lengths_mm.items():
        notes: list[str] = []
        try:
            h, use_er = dielectric_height(
                model, layer, assumptions, er, dielectric_h_mm
            )
            t = copper_thickness_mm(model, layer, assumptions)
            is_microstrip = _is_external_copper(layer)
            z0 = (
                microstrip_z0(width, t, h, use_er)
                if is_microstrip
                else stripline_z0(width, t, h, use_er)
            )
            model_name = "microstrip" if is_microstrip else "stripline"
            ratio = width / h if h else math.inf
            if not (0.1 < ratio < 2.0) or not (1.0 < use_er < 15.0):
                notes.append("outside IPC-2141 comfort zone")
            rows.append(
                ImpedanceRow(
                    route.net_name,
                    layer,
                    width,
                    length,
                    z0,
                    model_name,
                    h,
                    use_er,
                    notes,
                )
            )
        except ValueError as exc:
            rows.append(
                ImpedanceRow(
                    route.net_name,
                    layer,
                    width,
                    length,
                    None,
                    "unavailable",
                    None,
                    None,
                    [str(exc)],
                )
            )
    return ImpedanceReport(rows, _dedupe(assumptions))


def impedance_reports_for_pattern(
    model: PCBModel,
    pattern: str,
    limit: int = 50,
    er: float | None = None,
    dielectric_h_mm: float | None = None,
) -> ImpedanceReport:
    names = matching_net_names(model, pattern, limit)
    reports = [
        net_impedance(model, analyze_net_route(model, name), er, dielectric_h_mm)
        for name in names
    ]
    merged = ImpedanceReport(
        rows=[row for report in reports for row in report.rows],
        assumptions=_dedupe([a for report in reports for a in report.assumptions]),
    )
    pair = _resolve_pair_from_matches(model, pattern, names)
    if pair is not None:
        pos_route = analyze_net_route(model, pair[0])
        neg_route = analyze_net_route(model, pair[1])
        merged.coupled_rows = diff_pair_coupled_impedance(
            model, pos_route, neg_route, er, dielectric_h_mm, merged.assumptions
        )
    return merged


def hypothetical_impedance(
    model: PCBModel,
    layer: str,
    width_mm: float,
    er: float | None = None,
    dielectric_h_mm: float | None = None,
) -> ImpedanceReport:
    route = RouteAnalysis(
        0,
        "hypothetical",
        0.0,
        {},
        0,
        0,
        0,
        {},
        {},
        {(layer, width_mm): 0.0},
        width_mm,
        width_mm,
        [layer],
        [],
        0,
        False,
        [],
        [],
    )
    return net_impedance(model, route, er, dielectric_h_mm)


def diff_pair_coupled_impedance(
    model: PCBModel,
    pos: RouteAnalysis,
    neg: RouteAnalysis,
    er: float | None,
    dielectric_h_mm: float | None,
    assumptions: list[str],
) -> list[DiffCoupledRow]:
    pos_elems = model.net_copper_elements(pos.net_number)
    neg_elems = model.net_copper_elements(neg.net_number)
    rows: list[DiffCoupledRow] = []
    for layer in sorted(set(pos.layer_lengths_mm) & set(neg.layer_lengths_mm)):
        p_tracks = [t for t in pos_elems["tracks"] if t.layer == layer]
        n_tracks = [t for t in neg_elems["tracks"] if t.layer == layer]
        distances: list[float] = []
        for track in p_tracks:
            distance = _nearest_track_center_distance(track, n_tracks)
            if distance is not None:
                distances.append(distance)
        if not distances:
            continue
        spacing = statistics.median(distances)
        p_width = _dominant_width(pos, layer)
        n_width = _dominant_width(neg, layer)
        if p_width is None or n_width is None:
            continue
        gap = max(spacing - (p_width + n_width) / 2.0, 0.0)
        h, use_er = dielectric_height(model, layer, assumptions, er, dielectric_h_mm)
        t = copper_thickness_mm(model, layer, assumptions)
        micro = _is_external_copper(layer)
        z0 = (
            microstrip_z0((p_width + n_width) / 2.0, t, h, use_er)
            if micro
            else stripline_z0((p_width + n_width) / 2.0, t, h, use_er)
        )
        notes: list[str] = []
        if len(distances) > 1 and statistics.pstdev(distances) > max(
            0.1 * spacing, 0.05
        ):
            notes.append("high spacing variance: loosely coupled or inconsistent gap")
        rows.append(
            DiffCoupledRow(
                layer,
                spacing,
                gap,
                coupled_differential(z0, gap, h, microstrip=micro),
                notes,
            )
        )
    return rows


def _bracket(values: list[float], value: float) -> tuple[float, float]:
    for idx, item in enumerate(values):
        if value == item:
            return item, item
        if value < item:
            return values[idx - 1], item
    return values[-1], values[-1]


def _is_external_copper(layer: str) -> bool:
    return layer in {"F.Cu", "B.Cu"}


def _height_from_stackup(
    stackup: list[StackupLayer],
    layer: str,
    assumptions: list[str],
    er_override: float | None,
) -> tuple[float, float]:
    copper_indexes = [i for i, sl in enumerate(stackup) if sl.name.endswith(".Cu")]
    index_by_name = {sl.name: i for i, sl in enumerate(stackup)}
    if layer not in index_by_name:
        raise ValueError(f"layer {layer} is not present in stackup")
    idx = index_by_name[layer]
    candidates: list[tuple[int, int]] = []
    for cidx in copper_indexes:
        if cidx != idx:
            candidates.append((abs(cidx - idx), cidx))
    if not candidates:
        raise ValueError(f"no reference copper layer found near {layer}")
    ref_idx = min(candidates)[1]
    lo, hi = sorted((idx, ref_idx))
    dielectrics = [sl for sl in stackup[lo + 1 : hi] if not sl.name.endswith(".Cu")]
    if not dielectrics:
        raise ValueError(
            f"no dielectric layers between {layer} and nearest copper reference"
        )
    height = sum((sl.thickness or 0.0) for sl in dielectrics)
    if height <= 0:
        raise ValueError(
            f"missing dielectric thickness between {layer} and nearest copper reference"
        )
    if er_override is not None:
        return height, er_override
    weighted = [
        (sl.epsilon_r, sl.thickness)
        for sl in dielectrics
        if sl.epsilon_r and sl.thickness
    ]
    if weighted:
        total = sum(thickness for _, thickness in weighted if thickness is not None)
        er = (
            sum(er * thickness for er, thickness in weighted if thickness is not None)
            / total
        )
    else:
        assumptions.append("εr assumed 4.4: no stackup dielectric data")
        er = DEFAULT_ER
    assumptions.append(f"nearest copper layer assumed reference plane for {layer}")
    return height, er


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _resolve_pair_from_matches(
    model: PCBModel, pattern: str, names: list[str]
) -> tuple[str, str] | None:
    try:
        if len(names) == 1:
            return resolve_diff_pair(model, names[0])
        if len(names) == 2:
            return resolve_diff_pair(model, names[0], names[1])
        return resolve_diff_pair(model, pattern)
    except ValueError:
        return None


def _dominant_width(route: RouteAnalysis, layer: str) -> float | None:
    candidates = [
        (width, length)
        for (candidate_layer, width), length in route.layer_width_lengths_mm.items()
        if candidate_layer == layer
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])[0]


def _nearest_track_center_distance(track: Track, others: list[Track]) -> float | None:
    midpoint = Point(
        (track.start.x + track.end.x) / 2.0, (track.start.y + track.end.y) / 2.0
    )
    distances = [
        _point_to_segment_distance(midpoint, other.start, other.end) for other in others
    ]
    return min(distances) if distances else None


def _point_to_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return point.distance_to(start)
    u = max(
        0.0, min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length2)
    )
    projected = Point(start.x + u * dx, start.y + u * dy)
    return point.distance_to(projected)
