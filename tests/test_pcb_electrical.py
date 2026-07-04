"""Tests for derived PCB electrical estimates."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_mcp.pcb_electrical import (
    capacity_reports_for_pattern,
    coupled_differential,
    dielectric_height,
    impedance_reports_for_pattern,
    ipc2221_max_current,
    microstrip_z0,
    net_current_capacity,
    net_impedance,
    stripline_z0,
)
from kicad_mcp.pcb_model import PCBModel, Point, StackupLayer, Track
from kicad_mcp.pcb_route import analyze_net_route
from kicad_mcp.server import KiCadMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.kicad_pcb"
REAL_BOARDS = [
    ("/Users/mitch/projects/cm5-hudsp/cm5hudsp/cm5hudsp.kicad_pcb", "*V*"),
    ("/Users/mitch/projects/brainware/hw/boards/main_digital/main.kicad_pcb", "*V*"),
]


@pytest.fixture
def model() -> PCBModel:
    return PCBModel.from_file(FIXTURE)


def test_ipc2221_hand_computed_current() -> None:
    area = 10.0 * 1.378
    expected = 0.048 * 10.0**0.44 * area**0.725
    assert ipc2221_max_current(area, 10.0, internal=False) == pytest.approx(expected)
    assert ipc2221_max_current(area, 10.0, internal=True) == pytest.approx(
        expected / 2.0
    )
    assert expected == pytest.approx(0.89, abs=0.01)


def test_current_capacity_labels_ipc2221_curve_used() -> None:
    model = PCBModel(
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
        stackup=[
            StackupLayer("F.Cu", "copper", 0.035),
            StackupLayer("dielectric 1", "core", 0.2, epsilon_r=4.4),
            StackupLayer("In1.Cu", "copper", 0.035),
            StackupLayer("dielectric 2", "core", 0.2, epsilon_r=4.4),
            StackupLayer("B.Cu", "copper", 0.035),
        ],
        nets={1: "PWR"},
        tracks=[Track(1, "In1.Cu", 0.254, Point(0, 0), Point(10, 0))],
    )
    report = net_current_capacity(model, analyze_net_route(model, "PWR"))
    assert report.neck is not None
    assert report.neck.standard == "IPC-2221 internal"
    assert report.neck.estimated_a == pytest.approx(
        ipc2221_max_current(report.neck.area_mil2, 10.0, internal=True)
    )
    assert report.neck.estimated_a == pytest.approx(0.44, abs=0.02)


def test_impedance_formula_points() -> None:
    microstrip = (
        87.0 / math.sqrt(4.5 + 1.41) * math.log(5.98 * 1.51 / (0.8 * 0.2 + 0.035))
    )
    assert microstrip_z0(0.2, 0.035, 1.51, 4.5) == pytest.approx(microstrip)
    assert microstrip_z0(2.8, 0.035, 1.51, 4.5) == pytest.approx(50.0, abs=2.0)

    stripline = (
        60.0
        / math.sqrt(4.2)
        * math.log(4.0 * 0.2 / (0.67 * math.pi * (0.8 * 0.12 + 0.035)))
    )
    assert stripline_z0(0.12, 0.035, 0.2, 4.2) == pytest.approx(stripline)
    z0 = 90.0
    assert coupled_differential(z0, 0.15, 0.2, microstrip=True) < 2.0 * z0
    assert coupled_differential(z0, 0.15, 0.2, microstrip=False) < 2.0 * z0


def test_synthetic_neck_and_diff_pair_spacing(model: PCBModel) -> None:
    route = analyze_net_route(model, "DP_P")
    report = net_current_capacity(model, route)
    assert report.neck is not None
    assert report.neck.width_mm == pytest.approx(min(route.width_lengths_mm))

    impedance = impedance_reports_for_pattern(model, "DP_*")
    assert impedance.coupled_rows
    assert impedance.coupled_rows[0].spacing_center_mm == pytest.approx(1.0)
    assert impedance.coupled_rows[0].gap_mm == pytest.approx(0.8)

    rows = net_impedance(model, route).rows
    assert {(row.layer, row.width_mm) for row in rows} == set(
        route.layer_width_lengths_mm
    )


def test_diff_pair_impedance_degrades_when_coupled_stackup_missing() -> None:
    model = PCBModel(
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
        nets={1: "DP_P", 2: "DP_N"},
        tracks=[
            Track(1, "In1.Cu", 0.2, Point(0, 0), Point(10, 0)),
            Track(2, "In1.Cu", 0.2, Point(0, 1), Point(10, 1)),
        ],
    )

    report = impedance_reports_for_pattern(model, "DP_*")

    assert len(report.rows) == 2
    assert all(row.z0_ohm is None for row in report.rows)
    assert not report.coupled_rows
    assert any(
        "coupled differential unavailable on In1.Cu" in assumption
        for assumption in report.assumptions
    )


def test_degradation_paths_name_assumptions_and_refusals() -> None:
    two_layer = PCBModel(
        board_thickness=1.6,
        copper_layers=["F.Cu", "B.Cu"],
        nets={1: "SIG"},
    )
    assumptions: list[str] = []
    h, er = dielectric_height(two_layer, "F.Cu", assumptions)
    assert h == pytest.approx(1.53)
    assert er == pytest.approx(4.4)
    assert any("board thickness" in item for item in assumptions)
    assert any("εr assumed 4.4" in item for item in assumptions)

    multilayer = PCBModel(copper_layers=["F.Cu", "In1.Cu", "B.Cu"])
    with pytest.raises(ValueError, match="missing stackup dielectric thickness"):
        dielectric_height(multilayer, "In1.Cu", [])

    stack_model = PCBModel(
        copper_layers=["F.Cu", "B.Cu"],
        stackup=[
            StackupLayer("F.Cu", "copper"),
            StackupLayer("dielectric 1", "core", 1.5, epsilon_r=4.5),
            StackupLayer("B.Cu", "copper"),
        ],
        nets={1: "SIG"},
    )
    route = analyze_net_route(model=PCBModel.from_file(FIXTURE), net="SIG")
    assumed = net_current_capacity(stack_model, route)
    assert any("copper thickness assumed 35 µm" in item for item in assumed.assumptions)


@pytest.mark.asyncio
async def test_electrical_tools_on_fixture() -> None:
    server = KiCadMCPServer()
    result = await server.handle_call_tool(
        "pcb_current_capacity",
        {"source": str(FIXTURE), "pattern": "DP_*", "min_current_a": 1.0},
    )
    text = result[0].text
    assert "# Current Capacity Estimates: DP_*" in text
    assert "⚠ below threshold" in text
    assert "IPC-2221" in text

    result = await server.handle_call_tool(
        "pcb_impedance_estimate", {"source": str(FIXTURE), "pattern": "DP_*"}
    )
    text = result[0].text
    assert "IPC-2141 closed-form estimate" in text
    assert "Coupled differential estimate" in text
    assert "0.800 mm" in text


@pytest.mark.parametrize("path,pattern", REAL_BOARDS)
def test_real_board_electrical_invariants(path: str, pattern: str) -> None:
    board = Path(path)
    if not board.exists():
        pytest.skip(f"private board not available: {path}")
    model = PCBModel.from_file(board)
    reports = capacity_reports_for_pattern(model, pattern, limit=10)
    assert reports
    for report in reports:
        if report.neck is None:
            continue
        route = analyze_net_route(model, report.net_name)
        assert report.neck.estimated_a > 0
        assert math.isfinite(report.neck.estimated_a)
        assert report.neck.width_mm in route.width_lengths_mm
    zone_net = next((zone.net_name for zone in model.zones if zone.net_name), None)
    if zone_net is not None:
        zone_report = capacity_reports_for_pattern(model, zone_net, limit=1)[0]
        assert any("pour-carried" in flag for flag in zone_report.flags)

    impedance = impedance_reports_for_pattern(model, pattern, limit=10)
    assert impedance.rows
    for row in impedance.rows:
        if row.z0_ohm is not None:
            assert 0 < row.z0_ohm < 250
