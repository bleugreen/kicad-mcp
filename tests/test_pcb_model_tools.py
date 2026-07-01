"""Integration tests for the PCB MCP tools on a deterministic fixture.

Source resolution itself (KiCadMCPConfig.resolve_pcb_source) is covered by
tests/test_server.py; here we exercise the three PCB tools end to end.

Real-board tests reference the user's private designs by absolute path and skip
cleanly (never fail) when those files are absent, e.g. on CI or another machine.
The boards must never be committed.

Named test_pcb_model_tools.py (not test_pcb_tools.py) to avoid colliding with
sibling kicad-cli tool tests.
"""

from pathlib import Path

import pytest

from kicad_mcp.pcb_model import PCBModel
from kicad_mcp.server import KiCadMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.kicad_pcb"

# Private real boards used only as integration fixtures. Facts (footprint
# counts, copper layers) come from the issue and are spot-checked here.
REAL_BOARDS = {
    "/Users/mitch/projects/cm5-hudsp/cm5hudsp/cm5hudsp.kicad_pcb": {
        "footprints": 259,
        "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    },
    "/Users/mitch/projects/brainware/hw/boards/main_digital/main.kicad_pcb": {
        "footprints": 106,
        "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    },
}


@pytest.fixture
def server():
    return KiCadMCPServer()


# --- Synthetic-fixture tool tests (no external files) -----------------------


@pytest.mark.asyncio
async def test_pcb_overview_tool_on_fixture(server):
    result = await server.handle_call_tool("pcb_overview", {"source": str(FIXTURE)})
    text = result[0].text
    assert not text.startswith("Error"), text
    assert "# PCB Overview" in text
    assert "50.00 x 30.00 mm" in text
    assert "Footprints: 4" in text
    assert "GND" in text


@pytest.mark.asyncio
async def test_pcb_component_tool_on_fixture(server):
    result = await server.handle_call_tool(
        "pcb_component", {"source": str(FIXTURE), "reference": "R2"}
    )
    text = result[0].text
    assert "# Component: R2" in text
    assert "Side:** top" in text
    assert "Rotation:** 90" in text
    assert "GND" in text and "SIG" in text


@pytest.mark.asyncio
async def test_pcb_component_tool_missing_component(server):
    result = await server.handle_call_tool(
        "pcb_component", {"source": str(FIXTURE), "reference": "ZZ9"}
    )
    assert "not found" in result[0].text


@pytest.mark.asyncio
async def test_pcb_components_near_tool_on_fixture(server):
    result = await server.handle_call_tool(
        "pcb_components_near",
        {"source": str(FIXTURE), "reference": "R1", "radius_mm": 25.0},
    )
    text = result[0].text
    assert "U1" in text and "R2" in text
    assert "mm" in text


@pytest.mark.asyncio
async def test_pcb_tool_bad_source_returns_clean_error(server):
    # Unresolvable source -> resolver raises, tool returns a clean error string.
    result = await server.handle_call_tool(
        "pcb_overview", {"source": "/nope/missing.kicad_pcb"}
    )
    assert result[0].text.startswith("Error")


# --- Real-board acceptance (skip when private boards are absent) ------------


@pytest.mark.parametrize("path,facts", list(REAL_BOARDS.items()))
def test_real_boards_parse_and_count(path, facts):
    if not Path(path).exists():
        pytest.skip(f"private board not available: {path}")
    model = PCBModel.from_file(Path(path))
    assert len(model.footprints) == facts["footprints"]
    assert model.copper_layers == facts["copper_layers"]
    # A real board has an outline and plenty of copper.
    assert model.board_dimensions() is not None
    assert len(model.tracks) > 0
    assert len(model.vias) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path,facts", list(REAL_BOARDS.items()))
async def test_real_board_overview_tool(server, path, facts):
    if not Path(path).exists():
        pytest.skip(f"private board not available: {path}")
    result = await server.handle_call_tool("pcb_overview", {"source": path})
    text = result[0].text
    assert not text.startswith("Error"), text
    assert f"Footprints: {facts['footprints']}" in text
