"""Tests for routed-copper length and connectivity analysis."""

from pathlib import Path

import pytest

from kicad_mcp.pcb_model import PCBModel
from kicad_mcp.pcb_route import analyze_net_route, resolve_diff_pair, sorted_length_rows
from kicad_mcp.server import KiCadMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.kicad_pcb"
REAL_BOARDS = [
    ("/Users/mitch/projects/cm5-hudsp/cm5hudsp/cm5hudsp.kicad_pcb", "/CAM_CSI_D0_P"),
    ("/Users/mitch/projects/brainware/hw/boards/main_digital/main.kicad_pcb", "/D+"),
]


@pytest.fixture
def model() -> PCBModel:
    return PCBModel.from_file(FIXTURE)


def test_sig_route_length_and_single_island(model):
    route = analyze_net_route(model, "SIG")
    assert route.total_length_mm == pytest.approx(18.4, abs=1e-6)
    assert route.layer_lengths_mm == {"F.Cu": pytest.approx(18.4, abs=1e-6)}
    assert route.width_lengths_mm == {0.25: pytest.approx(18.4, abs=1e-6)}
    assert route.copper_island_count == 2
    assert not route.connected_only_through_zone
    assert route.endpoints == ["R1.2", "R2.1", "U1.3"]


def test_diff_pair_lengths_width_profile_and_resolution(model):
    assert resolve_diff_pair(model, "DP") == ("DP_P", "DP_N")
    pos = analyze_net_route(model, "DP_P")
    neg = analyze_net_route(model, "DP_N")
    assert pos.total_length_mm == pytest.approx(15.0, abs=1e-6)
    assert neg.total_length_mm == pytest.approx(15.0, abs=1e-6)
    assert pos.min_width_mm == pytest.approx(0.15)
    assert pos.max_width_mm == pytest.approx(0.2)
    assert pos.width_lengths_mm[0.2] == pytest.approx(10.0, abs=1e-6)
    assert pos.width_lengths_mm[0.15] == pytest.approx(5.0, abs=1e-6)


def test_gnd_zone_connectivity_is_distinct_from_disconnected(model):
    route = analyze_net_route(model, "GND")
    assert route.total_length_mm == pytest.approx(5.0, abs=1e-6)
    assert route.copper_island_count > 1
    assert route.connected_only_through_zone
    assert len(route.zones) == 1


def test_net_length_rows_match_glob_and_sort(model):
    rows = sorted_length_rows(model, "DP_*")
    assert [row.net_name for row in rows] == ["DP_N", "DP_P"]
    assert all(row.total_length_mm == pytest.approx(15.0, abs=1e-6) for row in rows)


@pytest.mark.asyncio
async def test_route_tools_on_fixture():
    server = KiCadMCPServer()
    result = await server.handle_call_tool(
        "pcb_net_route", {"source": str(FIXTURE), "net": "DP_P"}
    )
    text = result[0].text
    assert "# Route: DP_P" in text
    assert "15.000000 mm" in text
    assert "0.150000 mm: 5.000000 mm" in text

    result = await server.handle_call_tool(
        "pcb_diff_pair", {"source": str(FIXTURE), "net_p": "DP"}
    )
    assert "Length mismatch:** 0.000000 mm" in result[0].text

    result = await server.handle_call_tool(
        "pcb_net_lengths", {"source": str(FIXTURE), "pattern": "DP_*"}
    )
    assert "| DP_N | 15.000000" in result[0].text


@pytest.mark.parametrize("path,net", REAL_BOARDS)
def test_real_board_known_routed_net_is_connected(path, net):
    board = Path(path)
    if not board.exists():
        pytest.skip(f"private board not available: {path}")
    model = PCBModel.from_file(board)
    route = analyze_net_route(model, net)
    assert route.total_length_mm > 0
    assert route.copper_island_count == 1
