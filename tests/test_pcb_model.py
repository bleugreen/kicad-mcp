"""Unit tests for the PCB model on the deterministic synthetic fixture.

The fixture (tests/fixtures/synthetic.kicad_pcb) is a hand-authored KiCad 9
board with known geometry:

- Rectangular Edge.Cuts outline from (100,100) to (150,130) -> 50 x 30 mm.
- R1 @ (110,110) rot 0 on F.Cu; pads 1->VCC, 2->SIG.
- R2 @ (130,110) rot 90 on F.Cu; pads 1->SIG, 2->GND.
- U1 @ (120,120) rot 0 on B.Cu (bottom); pads 1->GND, 2->VCC, 3->SIG.
- One F.Cu SIG track, one B.Cu GND track, one GND via, one B.Cu GND zone.
"""

import math
from pathlib import Path

import pytest

from kicad_mcp.pcb_model import PCBModel, clear_pcb_cache, load_pcb_model

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.kicad_pcb"


@pytest.fixture
def model() -> PCBModel:
    return PCBModel.from_file(FIXTURE)


def test_layers_and_stackup(model):
    assert model.copper_layers == ["F.Cu", "B.Cu"]
    assert model.board_thickness == 1.6
    # Stackup carries the two copper layers with their thicknesses.
    copper = {s.name: s for s in model.stackup if s.type == "copper"}
    assert set(copper) == {"F.Cu", "B.Cu"}
    assert copper["F.Cu"].thickness == 0.035
    dielectric = next(s for s in model.stackup if s.type == "core")
    assert dielectric.material == "FR4"


def test_nets(model):
    assert model.nets == {0: "", 1: "GND", 2: "VCC", 3: "SIG", 4: "DP_P", 5: "DP_N"}


def test_outline_bbox_and_dimensions(model):
    bbox = model.bounding_box()
    assert (bbox.min_x, bbox.min_y) == (100.0, 100.0)
    assert (bbox.max_x, bbox.max_y) == (150.0, 130.0)
    assert model.board_dimensions() == (50.0, 30.0)


def test_footprint_placement(model):
    r1 = model.footprint("R1")
    assert r1.position.as_tuple() == (110.0, 110.0)
    assert r1.rotation == 0.0
    assert r1.side == "top"
    assert r1.value == "10k"
    assert r1.lib_id == "Resistor_SMD:R_0603_1608Metric"

    r2 = model.footprint("R2")
    assert r2.rotation == 90.0
    assert r2.side == "top"

    u1 = model.footprint("U1")
    assert u1.side == "bottom"
    assert len(u1.pads) == 3

    assert model.footprint("DOES_NOT_EXIST") is None


def test_pad_absolute_positions(model):
    # R1 rot 0: local pad offsets +/-0.8 in X map straight to board X.
    r1_pads = {p.number: p for p in model.footprint("R1").pads}
    assert r1_pads["1"].net_name == "VCC"
    assert r1_pads["1"].position.x == pytest.approx(109.2)
    assert r1_pads["2"].net_name == "SIG"
    assert r1_pads["2"].position.x == pytest.approx(110.8)

    # R2 rot 90: local (-0.8, 0) rotates to board (130, 110.8) under KiCad's
    # Y-down convention; (0.8, 0) -> (130, 109.2).
    r2_pads = {p.number: p for p in model.footprint("R2").pads}
    assert r2_pads["1"].position.x == pytest.approx(130.0)
    assert r2_pads["1"].position.y == pytest.approx(110.8)
    assert r2_pads["2"].position.y == pytest.approx(109.2)


def test_pad_and_zone_render_geometry(model):
    r2_pad = next(p for p in model.footprint("R2").pads if p.number == "1")
    assert r2_pad.shape == "roundrect"
    assert r2_pad.size.x == pytest.approx(0.9)
    assert r2_pad.size.y == pytest.approx(0.95)
    assert r2_pad.rotation == pytest.approx(90.0)

    zone = model.net_copper_elements("GND")["zones"][0]
    assert zone.filled_polygon_count == 1
    assert len(zone.polygons) == 1
    assert [(p.x, p.y) for p in zone.polygons[0]] == [
        (101.0, 101.0),
        (149.0, 101.0),
        (149.0, 129.0),
        (101.0, 129.0),
    ]


def test_footprints_near(model):
    # U1 is sqrt(200) ~= 14.14 mm from R1; R2 is exactly 20 mm.
    near_15 = model.footprints_near("R1", 15.0)
    assert [fp.reference for fp, _ in near_15] == ["U1"]
    assert near_15[0][1] == pytest.approx(math.hypot(10, 10))

    near_25 = model.footprints_near("R1", 25.0)
    assert [fp.reference for fp, _ in near_25] == ["U1", "R2"]  # sorted by distance
    # The anchor itself is excluded.
    assert all(fp.reference != "R1" for fp, _ in near_25)


def test_footprints_near_point(model):
    hits = model.footprints_near_point(110.0, 110.0, 1.0)
    assert [fp.reference for fp, _ in hits] == ["R1"]


def test_footprints_near_unknown_raises(model):
    with pytest.raises(KeyError):
        model.footprints_near("NOPE", 10.0)


def test_net_copper_lookup_by_name_and_number(model):
    gnd = model.net_copper_elements("GND")
    assert len(gnd["tracks"]) == 1
    assert len(gnd["vias"]) == 1
    assert len(gnd["zones"]) == 1
    assert len(gnd["arcs"]) == 0
    # GND pads: R2 pad 2 and U1 pad 1.
    gnd_pads = {(ref, pad.number) for ref, pad in gnd["pads"]}
    assert gnd_pads == {("R2", "2"), ("U1", "1")}

    # Looking up by net number gives the same result.
    assert model.net_copper_elements(1)["pads"] and (
        {(r, p.number) for r, p in model.net_copper_elements(1)["pads"]} == gnd_pads
    )

    sig = model.net_copper_elements("SIG")
    assert len(sig["tracks"]) == 1
    assert {(r, p.number) for r, p in sig["pads"]} == {
        ("R1", "2"),
        ("R2", "1"),
        ("U1", "3"),
    }


def test_net_copper_lookup_unknown_net(model):
    empty = model.net_copper_elements("NO_SUCH_NET")
    assert all(len(v) == 0 for v in empty.values())


def test_top_nets(model):
    top = model.top_nets(5)
    # GND has the most copper elements (track + via + zone + 2 pads = 5).
    assert top[0][1] == "GND"
    assert top[0][2] == 5
    names = [name for _, name, _ in top]
    assert "SIG" in names and "VCC" in names


def test_layer_element_counts(model):
    counts = model.layer_element_counts()
    assert set(counts) == {"F.Cu", "B.Cu"}
    assert counts["F.Cu"]["footprints"] == 2  # R1, R2
    assert counts["B.Cu"]["footprints"] == 1  # U1
    assert counts["F.Cu"]["tracks"] == 4
    assert counts["B.Cu"]["tracks"] == 1
    assert counts["B.Cu"]["zones"] == 1
    # The via spans F.Cu and B.Cu, so it is counted on both.
    assert counts["F.Cu"]["vias"] == 1
    assert counts["B.Cu"]["vias"] == 1


def test_track_via_zone_details(model):
    sig_track = next(t for t in model.tracks if t.net == 3)
    assert sig_track.layer == "F.Cu"
    assert sig_track.width == 0.25

    via = model.vias[0]
    assert via.net == 1
    assert via.drill == 0.4
    assert set(via.layers) == {"F.Cu", "B.Cu"}

    zone = model.zones[0]
    assert zone.net == 1
    assert zone.net_name == "GND"
    assert zone.layers == ["B.Cu"]
    assert zone.filled_polygon_count == 1


def test_load_pcb_model_caches_by_mtime(tmp_path):
    clear_pcb_cache()
    board = tmp_path / "b.kicad_pcb"
    board.write_text(FIXTURE.read_text())
    first = load_pcb_model(board)
    second = load_pcb_model(board)
    assert first is second  # unchanged file -> cached instance

    # Bump mtime into the future and rewrite: cache must rebuild.
    import os
    import time

    board.write_text(FIXTURE.read_text())
    future = time.time() + 10
    os.utime(board, (future, future))
    third = load_pcb_model(board)
    assert third is not first
