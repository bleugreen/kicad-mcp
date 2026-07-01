# KiCad MCP Server

Model Context Protocol (MCP) server for analyzing KiCad printed circuit boards. It
provides a toolkit for PCB analysis, with multi-board schematic signal tracing and
component datasheet lookup, exposed through a simple tool interface.

Single-board schematic text queries (component listings, per-net dumps, single-board
connection tracing) have moved to the companion `kicad-schema` project, which renders a
schematic as structured YAML. This server focuses on the analysis that is still hard to
do in text form: signals that cross board boundaries, datasheet discovery, and the PCB
layout surface that PCB-analysis tooling builds on.

## Features

- **Multi-Board Analysis**: Trace signals across multiple connected boards
- **Datasheet Lookup**: Resolve a manufacturer and part number to a datasheet URL
- **Board & System Configuration**: Register boards and systems; add or remove them without restarting
- **PCB Sources**: Associate a `.kicad_pcb` layout with each board for PCB-analysis tooling
- **PCB Tools (kicad-cli)**: Headless design-rule checking, 3D board renders, and per-layer SVG export from `.kicad_pcb` layouts
- **Smart Caching**: Optional file caching of parsed schematics for faster repeated queries

## Installation

```bash
# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

**Requirements**: Python 3.10+, and KiCad (for `kicad-cli`). The server discovers
`kicad-cli` automatically at the macOS app-bundle path, `/usr/bin`, and on `PATH`; set
the `KICAD_CLI` environment variable to point at a specific executable.

## Quick Start

### 1. Configure MCP Client

Add to your MCP client configuration (e.g., Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/kicad-mcp",
        "run",
        "kicad-mcp"
      ]
    }
  }
}
```

### 2. Create Configuration File

Create `.kicad_mcp.yaml` in your project directory:

```yaml
boards:
  main:
    path: /path/to/main.kicad_sch
    pcb: /path/to/main.kicad_pcb        # optional: PCB layout for PCB-analysis tools
    description: Main controller board

  sensor:
    path: /path/to/sensor.kicad_sch
    description: Sensor board

systems:
  complete:
    boards: [main, sensor]
    description: Full system with all boards

cache:
  enabled: true
  directory: ~/.cache/kicad_mcp
  check_mtime: true
```

## Configuration

Configuration files are searched in priority order:

1. **Environment variable**: `$KICAD_MCP_CONFIG`
2. **Local project**: `.kicad_mcp.yaml` (current directory or parent directories)
3. **Global config**: `~/.config/kicad_mcp/config.yaml`
4. **Default**: Empty configuration (no boards pre-loaded)

### Configuration Options

- **boards**: Named board configurations. Each board has:
  - `path`: path to the `.kicad_sch` schematic file
  - `pcb` *(optional)*: path to the `.kicad_pcb` layout file, used by PCB-analysis tools
  - `description` *(optional)*: human-readable description
- **systems**: Multi-board system definitions referencing board names
- **cache.enabled**: Enable/disable pickle caching of parsed schematics
- **cache.directory**: Where to store cache files
- **cache.check_mtime**: Invalidate cache when source files change

## Available Tools

### Board Management

| Tool | Description |
|------|-------------|
| `list_configured_boards` | List all boards from configuration |
| `list_configured_systems` | List all multi-board systems |
| `load_board` | Load a board by name (with caching) |
| `load_system` | Load a multi-board system |
| `reload_config` | Reload configuration without restarting |

### Configuration Management

| Tool | Description |
|------|-------------|
| `add_board` | Add a new board to configuration (schematic `path`, optional `pcb`) |
| `remove_board` | Remove a board from configuration |
| `add_system` | Add a new multi-board system |
| `remove_system` | Remove a system from configuration |

### Multi-Board Analysis

| Tool | Description |
|------|-------------|
| `get_system_overview` | Overview of a multi-board system |
| `trace_cross_board_signal` | Trace a signal across multiple boards |

### Datasheets

| Tool | Description |
|------|-------------|
| `search_datasheet` | Resolve a manufacturer and part number to a datasheet URL |

### PCB Layout (kicad-cli)

These tools operate on the `.kicad_pcb` layout via KiCad's headless `kicad-cli`.
The `source` argument accepts a configured board name (using its `pcb` path), a
direct path to a `.kicad_pcb`, or a path to a `.kicad_sch` (resolved to its
sibling `.kicad_pcb`).

| Tool | Description |
|------|-------------|
| `pcb_drc` | Run Design Rule Check; returns violations grouped by rule with severities, mm coordinates, totals, and the JSON report path. **Fails closed** — a failed run returns an explicit error, never a false clean pass. Accepts `severity` and `max_violations` filters. |
| `pcb_render` | Render the board in 3D to a PNG, returned as an inline image plus the saved file path. Camera controls: `side`, `zoom`, `rotate`, `pan`, `pivot`, `perspective`, `floor`, `width`, `height`, `quality`, `background`. |
| `pcb_export_layers` | Export one SVG per layer (e.g. `F.Cu,B.Cu,Edge.Cuts`) and return the file paths. `fit` defaults to `board` (board-area only) for downstream cropping. |

The underlying wrappers live in `kicad_mcp.kicad_cli` and are importable as plain
functions, so non-MCP consumers (such as a crop/highlight tool that needs
board-area-fitted per-layer SVGs) can call them directly.

### PCB Layout (parsed model)

These tools read a typed, in-memory model of the `.kicad_pcb` (placement, copper,
stackup, pads, tracks, vias, zones) parsed with the pure-Python `kiutils` library
and cached per `(path, mtime)` — no `kicad-cli` process. Their `source` resolves
through `KiCadMCPConfig.resolve_pcb_source` (a configured board's `pcb` path, a
direct `.kicad_pcb` path, or a `.kicad_sch` sibling). The queryable model lives
in `kicad_mcp.pcb_model`; direct 2D PNG rendering lives in
`kicad_mcp.pcb_rendering`.

| Tool | Description |
|------|-------------|
| `pcb_overview` | Board dimensions, layer/stackup summary, footprint/track/via/zone counts, net count, and top nets by copper element count |
| `pcb_component` | A component's placement (position, side, rotation), footprint id, and pads with their nets |
| `pcb_components_near` | Footprints placed within a radius (mm) of a component, with distances |
| `pcb_net_route` | Routed copper length, layer usage, widths, vias, endpoints, and copper-island connectivity for one net |
| `pcb_diff_pair` | Length and via-count comparison for a differential pair, with pair-name inference for common `_P`/`_N` and `+`/`-` conventions |
| `pcb_net_lengths` | Sorted routed lengths for nets matching a glob or regular expression |
| `pcb_crop` | Inline PNG crop of a component, a net's copper bounds, or an explicit board-coordinate window; returns MCP ImageContent plus the saved path |
| `pcb_highlight_net` | Inline PNG with one net drawn bright over dimmed board copper, including lower-alpha zones; returns MCP ImageContent plus the saved path |

## Usage Examples

### Multi-Board Systems

```python
# Load and overview a system
load_system(system_name="complete")
get_system_overview(system_name="complete")

# Trace SPI signal across boards
trace_cross_board_signal(
    system_name="complete",
    signal_net="/MISO",
    start_component="main:U1",
    end_component="sensor:U2"
)
```

### Datasheet Lookup

```python
search_datasheet(
    manufacturer="Texas Instruments",
    part_number="ADS1299IPAGR"
)
```

### Dynamic Configuration

```python
# Add a new board, including its PCB layout
add_board(
    name="power",
    path="/path/to/power.kicad_sch",
    pcb="/path/to/power.kicad_pcb",
    description="Power supply board"
)

# Create a system with it
add_system(
    name="full_system",
    boards=["main", "sensor", "power"],
    description="Complete system with power"
)

# Reload after manual config edits
reload_config()
```

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check src/
```

## License

MIT
