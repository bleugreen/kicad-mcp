import pickle
import re
from pathlib import Path

import pytest

from kicad_mcp.circuit_graph import CircuitGraph
from kicad_mcp.config import CACHE_VERSION, KiCadMCPConfig
from kicad_mcp.server import KiCadMCPServer

# Real schematic used as an integration fixture. Skip cleanly when it's not
# present (e.g. CI or another machine) rather than hard-failing.
SCHEMATIC_PATH = "/Users/mitch/projects/brainware/hw/boards/sense/8ch_sense/8ch_sense.kicad_sch"

requires_schematic = pytest.mark.skipif(
    not Path(SCHEMATIC_PATH).exists(),
    reason=f"Test schematic not available at {SCHEMATIC_PATH}",
)

# Fragments that indicate an f-string whose braces were doubled (`{{...}}`) and
# therefore rendered literally instead of being interpolated. These regressed
# several tools; guard against them coming back.
UNRENDERED_TEMPLATE = re.compile(r"\{'[^']*' if |\{len\(|\{', '\.join|\{node\}|\{value\}")


# --- Cache-layer regressions (no schematic required) ------------------------

def test_circuit_graph_resolves_load_mtime_without_init():
    """An object restored via pickle bypasses __init__; the cache-staleness
    check must still resolve _load_mtime instead of raising AttributeError.

    This reproduces the wedge where the first call (loading a pre-fix pickle)
    worked but the second call hit the mtime branch and crashed.
    """
    legacy = pickle.loads(pickle.dumps(CircuitGraph()))
    legacy.__dict__.pop("_load_mtime", None)  # simulate a pre-fix cache file
    assert legacy._load_mtime is None  # class-level default, no AttributeError


def test_cache_round_trip_and_rejects_incompatible(tmp_path):
    cfg = KiCadMCPConfig()
    cache_path = tmp_path / "board.cache"

    # Versioned round-trip returns the object.
    cfg._save_cache(cache_path, CircuitGraph())
    assert isinstance(cfg._load_cache(cache_path), CircuitGraph)

    # Legacy unversioned pickle (raw object) is rejected, not loaded.
    with open(cache_path, "wb") as f:
        pickle.dump(CircuitGraph(), f)
    assert cfg._load_cache(cache_path) is None

    # Version mismatch is rejected.
    with open(cache_path, "wb") as f:
        pickle.dump({"version": CACHE_VERSION - 1, "obj": CircuitGraph()}, f)
    assert cfg._load_cache(cache_path) is None


# --- Tool integration tests (require the fixture schematic) -----------------
# Each integration test below is decorated with @requires_schematic; the
# cache-layer tests above run unconditionally.


@pytest.fixture
def server():
    return KiCadMCPServer()


@pytest.fixture
def circuit(server):
    c = server._load_circuit(SCHEMATIC_PATH)
    assert c is not None, "fixture schematic failed to load"
    return c


def _assert_clean(text: str, tool: str):
    """A tool result should not be an error or contain unrendered f-strings."""
    assert not text.startswith("Error"), f"{tool} returned an error: {text[:120]}"
    assert "no attribute" not in text, f"{tool} hit a missing attribute: {text[:120]}"
    m = UNRENDERED_TEMPLATE.search(text)
    assert m is None, f"{tool} has an unrendered f-string near: {text[m.start()-10:m.start()+40]!r}"


@requires_schematic
def test_load_circuit_is_synchronous_and_stamps_mtime(circuit):
    # _load_circuit must return a CircuitGraph, not a coroutine, and the
    # cache-validation metadata must be populated.
    assert circuit._filepath is not None
    assert circuit._load_mtime is not None
    assert hasattr(circuit, "_is_passive_component")


@requires_schematic
def test_cache_returns_same_instance_when_unchanged(server):
    first = server._load_circuit(SCHEMATIC_PATH)
    second = server._load_circuit(SCHEMATIC_PATH)
    # File unmodified between calls -> fresh cache hit, same object.
    assert first is second


@pytest.mark.asyncio
@requires_schematic
async def test_get_netlist(server):
    result = await server.handle_call_tool("get_netlist", {"source": SCHEMATIC_PATH})
    assert len(result) == 1
    content = result[0].text
    _assert_clean(content, "get_netlist")
    assert "# Full Netlist" in content
    # At least one net section should be rendered.
    assert "(signal):" in content or "(power):" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_overview", "get_info", "list_nets"])
@requires_schematic
async def test_overview_tools(server, tool):
    result = await server.handle_call_tool(tool, {"source": SCHEMATIC_PATH})
    _assert_clean(result[0].text, tool)


@pytest.mark.asyncio
@requires_schematic
async def test_list_nets_power_only(server):
    result = await server.handle_call_tool(
        "list_nets", {"source": SCHEMATIC_PATH, "power_only": True}
    )
    text = result[0].text
    _assert_clean(text, "list_nets(power_only)")
    assert "(Power only)" in text


@pytest.mark.asyncio
@requires_schematic
async def test_examine_component_and_net(server, circuit):
    refs = list(circuit.netlist.components.keys())
    ic = next((r for r in refs if r.startswith("U")), refs[0])
    passive = next((r for r in refs if r[:1] in ("R", "C")), refs[0])
    net = next(iter(circuit.netlist.nets.keys()))

    for ref in (ic, passive):
        result = await server.handle_call_tool(
            "examine_component", {"source": SCHEMATIC_PATH, "reference": ref}
        )
        text = result[0].text
        _assert_clean(text, f"examine_component({ref})")
        assert f"# Component: {ref}" in text

    result = await server.handle_call_tool(
        "examine_net", {"source": SCHEMATIC_PATH, "net_name": net}
    )
    _assert_clean(result[0].text, "examine_net")


@pytest.mark.asyncio
@requires_schematic
async def test_find_connected_components(server, circuit):
    refs = list(circuit.netlist.components.keys())
    ic = next((r for r in refs if r.startswith("U")), refs[0])
    result = await server.handle_call_tool(
        "find_connected_components", {"source": SCHEMATIC_PATH, "reference": ic}
    )
    _assert_clean(result[0].text, "find_connected_components")
