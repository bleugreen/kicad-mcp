"""Tests for direct 2D PCB image rendering."""

from pathlib import Path

import pytest
from PIL import Image

from kicad_mcp import pcb_rendering
from kicad_mcp.pcb_model import PCBModel
from kicad_mcp.server import KiCadMCPServer

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.kicad_pcb"
BRAINWARE_BOARD = Path(
    "/Users/mitch/projects/brainware/hw/boards/main_digital/main.kicad_pcb"
)


@pytest.fixture
def model() -> PCBModel:
    return PCBModel.from_file(FIXTURE)


def _pixel_at_mm(path: str, bbox, x_mm: float, y_mm: float):
    image = Image.open(path).convert("RGBA")
    px = round((x_mm - bbox.min_x) / bbox.width * (image.width - 1))
    py = round((y_mm - bbox.min_y) / bbox.height * (image.height - 1))
    return image.getpixel((px, py))


def test_crop_reference_bbox_contains_target_footprint(model, tmp_path):
    result = pcb_rendering.render_crop(
        model,
        reference="R1",
        margin_mm=2.0,
        width_px=400,
        output_dir=str(tmp_path),
    )
    path = Path(result.path)
    assert path.exists() and path.stat().st_size > 100
    assert result.width == 400
    assert result.height <= 400

    r1 = model.footprint("R1")
    assert r1 is not None
    assert result.bbox.min_x < min(p.position.x for p in r1.pads)
    assert result.bbox.max_x > max(p.position.x for p in r1.pads)
    assert result.bbox.min_y < min(p.position.y for p in r1.pads)
    assert result.bbox.max_y > max(p.position.y for p in r1.pads)


def test_crop_rejects_ambiguous_target(model):
    with pytest.raises(ValueError, match="exactly one"):
        pcb_rendering.render_crop(model, reference="R1", net="SIG")


def test_highlight_net_pixel_differs_from_empty_region(model, tmp_path):
    result = pcb_rendering.render_highlight_net(
        model,
        net="SIG",
        x_mm=108.0,
        y_mm=108.0,
        width_mm=25.0,
        height_mm=5.0,
        layers=["F.Cu"],
        width_px=500,
        output_dir=str(tmp_path),
    )
    path = Path(result.path)
    assert path.exists() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.width == 500
    assert result.height == 100

    track_pixel = _pixel_at_mm(result.path, result.bbox, 120.0, 110.0)
    empty_pixel = _pixel_at_mm(result.path, result.bbox, 109.0, 108.5)
    assert track_pixel != empty_pixel
    assert track_pixel[0] > empty_pixel[0]


@pytest.mark.asyncio
async def test_server_pcb_crop_returns_image(tmp_path):
    server = KiCadMCPServer()
    result = await server.handle_call_tool(
        "pcb_crop",
        {
            "source": str(FIXTURE),
            "reference": "R1",
            "margin_mm": 2.0,
            "width_px": 400,
            "output_dir": str(tmp_path),
        },
    )
    assert any(getattr(c, "type", None) == "image" for c in result)
    image = next(c for c in result if getattr(c, "type", None) == "image")
    text = next(c for c in result if getattr(c, "type", None) == "text")
    assert image.mimeType == "image/png"
    assert image.data
    assert "Saved to" in text.text


@pytest.mark.asyncio
async def test_server_pcb_highlight_net_returns_image(tmp_path):
    server = KiCadMCPServer()
    result = await server.handle_call_tool(
        "pcb_highlight_net",
        {
            "source": str(FIXTURE),
            "net": "SIG",
            "layers": ["F.Cu"],
            "width_px": 500,
            "output_dir": str(tmp_path),
        },
    )
    assert any(getattr(c, "type", None) == "image" for c in result)
    image = next(c for c in result if getattr(c, "type", None) == "image")
    assert image.mimeType == "image/png"
    assert image.data


@pytest.mark.skipif(
    not BRAINWARE_BOARD.exists(), reason="private Brainware board is absent"
)
def test_integration_real_brainware_crop_and_highlight(tmp_path):
    model = PCBModel.from_file(BRAINWARE_BOARD)
    reference = model.footprints[0].reference
    net_number, net_name, _ = next(
        (num, name, count) for num, name, count in model.top_nets(10) if count > 0
    )
    net = net_name or net_number

    crop = pcb_rendering.render_crop(
        model,
        reference=reference,
        margin_mm=5.0,
        width_px=600,
        output_dir=str(tmp_path),
    )
    highlight = pcb_rendering.render_highlight_net(
        model,
        net=net,
        width_px=600,
        output_dir=str(tmp_path),
    )

    for result in (crop, highlight):
        path = Path(result.path)
        assert path.exists() and path.stat().st_size > 100
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert max(result.width, result.height) == 600
