import pickle

import pytest

from kicad_mcp.circuit_graph import CircuitGraph
from kicad_mcp.config import CACHE_VERSION, KiCadMCPConfig
from kicad_mcp.kicad_cli import find_kicad_cli


# --- Cache-layer regressions ------------------------------------------------

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


# --- resolve_pcb_source -----------------------------------------------------
# All PCB source resolution routes through this one helper, so cover the four
# distinct outcomes: configured name, direct path, missing path, unknown name.

@pytest.fixture
def config_with_pcb(tmp_path):
    """A config whose 'main' board carries an existing .kicad_pcb path, plus a
    'no_pcb' board that has none."""
    pcb = tmp_path / "main.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    cfg = KiCadMCPConfig()
    cfg.config['boards'] = {
        'main': {
            'path': str(tmp_path / 'main.kicad_sch'),
            'description': 'Main',
            'pcb': str(pcb),
        },
        'no_pcb': {
            'path': str(tmp_path / 'other.kicad_sch'),
            'description': 'No PCB',
        },
    }
    return cfg, pcb


def test_resolve_pcb_source_configured_name(config_with_pcb):
    cfg, pcb = config_with_pcb
    assert cfg.resolve_pcb_source('main') == pcb


def test_resolve_pcb_source_direct_path(config_with_pcb, tmp_path):
    cfg, _ = config_with_pcb
    direct = tmp_path / "loose.kicad_pcb"
    direct.write_text("(kicad_pcb)")
    assert cfg.resolve_pcb_source(str(direct)) == direct


def test_resolve_pcb_source_nonexistent_path(config_with_pcb, tmp_path):
    cfg, _ = config_with_pcb
    missing = tmp_path / "missing.kicad_pcb"
    with pytest.raises(ValueError):
        cfg.resolve_pcb_source(str(missing))


def test_resolve_pcb_source_unknown_name(config_with_pcb):
    cfg, _ = config_with_pcb
    with pytest.raises(ValueError) as exc:
        cfg.resolve_pcb_source('does_not_exist')
    # The error should point the caller at boards that do have a pcb path.
    assert 'main' in str(exc.value)


def test_resolve_pcb_source_board_without_pcb(config_with_pcb):
    cfg, _ = config_with_pcb
    with pytest.raises(ValueError):
        cfg.resolve_pcb_source('no_pcb')


# --- find_kicad_cli ---------------------------------------------------------

def test_find_kicad_cli_env_override(monkeypatch):
    """KICAD_CLI is honored and takes priority over probed locations.

    /bin/echo exits 0 for any arguments, standing in for a working kicad-cli.
    """
    monkeypatch.setenv("KICAD_CLI", "/bin/echo")
    assert find_kicad_cli() == "/bin/echo"


def test_find_kicad_cli_bad_override_falls_through(monkeypatch):
    """A non-working override is skipped rather than returned.

    Discovery either falls through to a real kicad-cli or raises RuntimeError,
    but it must never hand back the broken override path.
    """
    monkeypatch.setenv("KICAD_CLI", "/nonexistent/kicad-cli-xyz")
    try:
        assert find_kicad_cli() != "/nonexistent/kicad-cli-xyz"
    except RuntimeError:
        pass
