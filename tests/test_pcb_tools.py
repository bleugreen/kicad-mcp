"""Tests for the kicad-cli-backed PCB tools (DRC / render / SVG export).

Split into three groups:

* DRC JSON parsing against a committed sample report (no CLI, no board).
* Fail-closed behavior driven by tiny fake-cli stub scripts (no real KiCad).
* Integration tests that shell out to the real kicad-cli against the user's
  private boards, skipping cleanly when either is absent.
"""

import json
import stat
from pathlib import Path

import pytest

from kicad_mcp import kicad_cli
from kicad_mcp.config import KiCadMCPConfig
from kicad_mcp.kicad_cli import KiCadCLIError
from kicad_mcp.server import KiCadMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "drc_sample.json"

# Real private boards used as integration fixtures; skip cleanly when absent.
BOARDS = [
    "/Users/mitch/projects/cm5-hudsp/cm5hudsp/cm5hudsp.kicad_pcb",
    "/Users/mitch/projects/brainware/hw/boards/main_digital/main.kicad_pcb",
]


def _kicad_cli_available() -> bool:
    try:
        kicad_cli.find_kicad_cli()
        return True
    except Exception:
        return False


def _available_board():
    for b in BOARDS:
        if Path(b).exists():
            return b
    return None


requires_cli_and_board = pytest.mark.skipif(
    not (_kicad_cli_available() and _available_board()),
    reason="kicad-cli or a test board is not available",
)


# --- DRC JSON parsing -------------------------------------------------------


@pytest.fixture
def sample_report():
    return json.loads(FIXTURE.read_text())


def test_fixture_exists_and_has_expected_shape(sample_report):
    # Guards the committed fixture against accidental truncation.
    assert sample_report["violations"], "fixture should carry violations"
    assert sample_report["unconnected_items"], "fixture should carry unconnected items"
    entry = sample_report["violations"][0]
    assert {"type", "description", "severity", "items"} <= set(entry)


def test_parse_groups_by_rule_and_counts(sample_report):
    result = kicad_cli.parse_drc_report(sample_report)
    # 6 violations + 2 unconnected_items = 8 problems.
    assert result["total"] == 8
    assert result["by_severity"] == {"error": 6, "warning": 2}
    assert result["by_category"] == {"violations": 6, "unconnected_items": 2}

    # Grouped by rule type: 6 distinct violation types + one unconnected group.
    types = {g["type"] for g in result["groups"]}
    assert "unconnected_items" in types
    assert "clearance" in types
    assert sum(g["count"] for g in result["groups"]) == 8


def test_parse_carries_coordinates(sample_report):
    result = kicad_cli.parse_drc_report(sample_report)
    # Every enumerated item should carry mm coordinates and a uuid where the
    # source provided them.
    all_items = [
        it for g in result["groups"] for p in g["problems"] for it in p["items"]
    ]
    assert all_items, "expected item-level detail"
    assert any(it["x"] is not None and it["y"] is not None for it in all_items)


def test_parse_severity_filter(sample_report):
    errors = kicad_cli.parse_drc_report(sample_report, severity="error")
    assert errors["total"] == 6
    assert set(errors["by_severity"]) == {"error"}

    warnings = kicad_cli.parse_drc_report(sample_report, severity="warning")
    assert warnings["total"] == 2
    assert set(warnings["by_severity"]) == {"warning"}


def test_parse_max_violations_truncates_listing_not_totals(sample_report):
    result = kicad_cli.parse_drc_report(sample_report, max_violations=2)
    assert result["total"] == 8  # totals stay exact
    assert result["truncated"] is True
    listed = sum(len(g["problems"]) for g in result["groups"])
    assert listed == 2


def test_parse_rejects_non_object():
    # Garbage that isn't a JSON object must raise, never yield an empty pass.
    with pytest.raises(KiCadCLIError):
        kicad_cli.parse_drc_report([1, 2, 3])
    with pytest.raises(KiCadCLIError):
        kicad_cli.parse_drc_report("not a report")


def test_parse_invalid_severity_raises(sample_report):
    with pytest.raises(KiCadCLIError):
        kicad_cli.parse_drc_report(sample_report, severity="bogus")


# --- Fail-closed DRC via fake-cli stubs -------------------------------------


def _make_stub(tmp_path: Path, body: str) -> str:
    stub = tmp_path / "fake-kicad-cli"
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


@pytest.fixture
def dummy_pcb(tmp_path):
    # resolve_pcb_source only checks existence/extension, not contents.
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return str(pcb)


def test_run_drc_fails_closed_on_nonzero_exit(tmp_path, dummy_pcb):
    stub = _make_stub(tmp_path, "echo 'drc exploded' >&2\nexit 1\n")
    with pytest.raises(KiCadCLIError) as exc:
        kicad_cli.run_drc(dummy_pcb, cli=stub, output_dir=str(tmp_path / "out"))
    assert "exit 1" in str(exc.value)


def test_run_drc_fails_closed_on_garbage_json(tmp_path, dummy_pcb):
    # Stub exits 0 but writes non-JSON to the --output path.
    body = (
        'out=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "--output" ]; then shift; out="$1"; fi\n'
        '  shift\n'
        'done\n'
        'echo "this is definitely not json" > "$out"\n'
        'exit 0\n'
    )
    stub = _make_stub(tmp_path, body)
    with pytest.raises(KiCadCLIError) as exc:
        kicad_cli.run_drc(dummy_pcb, cli=stub, output_dir=str(tmp_path / "out"))
    assert "JSON" in str(exc.value) or "json" in str(exc.value)


def test_run_drc_fails_closed_on_missing_report(tmp_path, dummy_pcb):
    # Stub exits 0 but writes nothing: a silent success with no report must
    # not read as a clean pass.
    stub = _make_stub(tmp_path, "exit 0\n")
    with pytest.raises(KiCadCLIError):
        kicad_cli.run_drc(dummy_pcb, cli=stub, output_dir=str(tmp_path / "out"))


def test_run_drc_timeout_fails_closed(tmp_path, dummy_pcb):
    stub = _make_stub(tmp_path, "sleep 5\nexit 0\n")
    with pytest.raises(KiCadCLIError) as exc:
        kicad_cli.run_drc(
            dummy_pcb, cli=stub, output_dir=str(tmp_path / "out"), timeout=1
        )
    assert "timed out" in str(exc.value)


# --- Source resolution: schematic-sibling behavior --------------------------
# The configured-name / direct-path / missing / unknown-name cases live in
# tests/test_server.py against KiCadMCPConfig.resolve_pcb_source (the canonical
# resolver). Here we cover only the .kicad_sch -> sibling .kicad_pcb behavior
# this task added to that resolver.


def test_resolve_pcb_source_schematic_sibling(tmp_path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)")
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert KiCadMCPConfig().resolve_pcb_source(str(sch)) == pcb


def test_resolve_pcb_source_schematic_without_sibling_raises(tmp_path):
    sch = tmp_path / "lonely.kicad_sch"
    sch.write_text("(kicad_sch)")
    with pytest.raises(ValueError):
        KiCadMCPConfig().resolve_pcb_source(str(sch))


# --- MCP server fail-closed wiring ------------------------------------------


@pytest.mark.asyncio
async def test_server_pcb_drc_surfaces_error_not_clean_pass():
    # A bogus source must produce an explicit Error, never a clean-looking
    # empty DRC result.
    server = KiCadMCPServer()
    result = await server.handle_call_tool(
        "pcb_drc", {"source": "/tmp/does-not-exist.kicad_pcb"}
    )
    assert len(result) == 1
    assert result[0].text.startswith("Error")


# --- Integration (real CLI + real boards) -----------------------------------


@requires_cli_and_board
def test_integration_run_drc(tmp_path):
    board = _available_board()
    report = kicad_cli.run_drc(board, output_dir=str(tmp_path))
    assert "total" in report
    assert Path(report["report_path"]).exists()
    assert report["duration_s"] >= 0
    # Structure sanity: groups sum to total.
    assert sum(g["count"] for g in report["groups"]) == report["total"]


@requires_cli_and_board
def test_integration_render(tmp_path):
    board = _available_board()
    result = kicad_cli.render_pcb(
        board, width=640, height=480, output_dir=str(tmp_path)
    )
    p = Path(result["path"])
    assert p.exists() and p.stat().st_size > 0
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@requires_cli_and_board
def test_integration_export_layers(tmp_path):
    board = _available_board()
    paths = kicad_cli.export_layers_svg(
        board, ["F.Cu", "Edge.Cuts"], output_dir=str(tmp_path)
    )
    assert len(paths) == 2
    for p in paths:
        assert p.exists() and p.stat().st_size > 0
        assert p.suffix == ".svg"


@pytest.mark.asyncio
@requires_cli_and_board
async def test_integration_server_render_returns_image():
    server = KiCadMCPServer()
    board = _available_board()
    result = await server.handle_call_tool(
        "pcb_render", {"source": board, "width": 640, "height": 480}
    )
    # First content is the image, second the caption.
    assert any(getattr(c, "type", None) == "image" for c in result)
    img = next(c for c in result if getattr(c, "type", None) == "image")
    assert img.mimeType == "image/png"
    assert img.data  # non-empty base64
