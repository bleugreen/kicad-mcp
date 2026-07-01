from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kicad_mcp.kicad_ipc import KiCadIPC, KiCadIPCError, _box_contains
from kicad_mcp.server import KiCadMCPServer


class FakeNet:
    def __init__(self, name: str):
        self.name = name


class FakeVector:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


class FakePad:
    def __init__(self, net: str):
        self.net = FakeNet(net)


class FakeFootprint:
    def __init__(self, reference: str, nets: list[str]):
        self.id = SimpleNamespace(value=f"id-{reference}")
        self.reference_field = SimpleNamespace(text=SimpleNamespace(value=reference))
        self.definition = SimpleNamespace(pads=[FakePad(net) for net in nets])
        self.position = FakeVector(12_500_000, 22_000_000)


class FakeTrack:
    def __init__(self, net: str):
        self.id = SimpleNamespace(value="id-track")
        self.net = FakeNet(net)


class FakeFailingIPC:
    def session(self):
        raise KiCadIPCError(
            "Could not reach a running KiCad IPC API server. Enable KiCad API in KiCad Preferences → Plugins.",
            socket_path="ipc:///tmp/kicad/api.sock",
        )


class FakeValidationIPC:
    def focus(self, *, reference=None, position=None):
        raise ValueError("Pass exactly one of reference or position")


def test_kicad_session_unreachable_error_shape():
    server = KiCadMCPServer()
    server.kicad_ipc = FakeFailingIPC()

    result = asyncio.run(server.handle_call_tool("kicad_session", {}))

    text = result[0].text
    assert text.startswith("Error:")
    assert "Attempted socket: ipc:///tmp/kicad/api.sock" in text
    assert "Enable KiCad API" in text


def test_selection_summary_extracts_references_nets_and_types():
    ipc = KiCadIPC()
    summary = ipc.selection_summary(
        [FakeFootprint("U1", ["GND", "+3V3"]), FakeTrack("GND")]
    )

    assert summary["count"] == 2
    assert summary["references"] == ["U1"]
    assert summary["nets"] == ["+3V3", "GND"]
    assert summary["type_counts"] == {"FakeFootprint": 1, "FakeTrack": 1}
    assert summary["items"][0]["position"] == {"x_mm": 12.5, "y_mm": 22.0}


def test_focus_argument_validation_returns_clean_error():
    server = KiCadMCPServer()
    server.kicad_ipc = FakeValidationIPC()

    result = asyncio.run(server.handle_call_tool("kicad_focus", {}))

    assert result[0].text == "Error: Pass exactly one of reference or position"


def test_box_contains_kipy_style_pos_size_box():
    box = SimpleNamespace(pos=FakeVector(1_000_000, 2_000_000), size=FakeVector(3_000_000, 4_000_000))

    assert _box_contains(box, 2_000_000, 3_000_000)
    assert not _box_contains(box, 5_000_000, 3_000_000)


def test_kicad_open_board_missing_source_validation():
    server = KiCadMCPServer()

    result = asyncio.run(server.handle_call_tool("kicad_open_board", {}))

    assert result[0].text == "Error: source parameter is required"


def test_live_kicad_session_skip_unless_ipc_api_reachable():
    ipc = KiCadIPC(timeout_ms=300)
    try:
        session = ipc.session()
    except KiCadIPCError as exc:
        pytest.skip(f"live KiCad IPC API not reachable: {exc}")

    assert session["reachable"] is True
    assert "version" in session
    assert "boards" in session
