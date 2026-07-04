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
- **Live KiCad Session**: Connect to a running KiCad 9 PCB editor through the official IPC API for selection sync and reversible GUI cross-probing
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
| `pcb_current_capacity` | Pattern-first current-capacity estimates for matched nets, sorted by weakest neck, using IPC-2152 conservative chart fits with IPC-2221 fallback |
| `pcb_impedance_estimate` | Pattern-first or hypothetical trace IPC-2141 impedance estimates, including coupled differential estimates when a matched pair is resolved |
| `pcb_crop` | Inline PNG crop of a component, a net's copper bounds, or an explicit board-coordinate window; returns MCP ImageContent plus the saved path |
| `pcb_highlight_net` | Inline PNG with one net drawn bright over dimmed board copper, including lower-alpha zones; returns MCP ImageContent plus the saved path |

### Electrical estimates — IPC-2152/2221 ampacity and IPC-2141 impedance

`pcb_current_capacity` and `pcb_impedance_estimate` are estimate tools, not
thermal simulation or a field solver. They use the same pattern-first behavior as
`pcb_net_lengths`: a glob or regular expression matches nets and returns a table
across all matches. When exactly one net matches, `pcb_current_capacity` appends
per-segment detail for each `(layer, width)` geometry.

`pcb_current_capacity` estimates each routed track geometry from copper width,
stackup copper thickness, layer type, and requested temperature rise. It uses the
IPC-2152 conservative chart fit when the trace area and temperature rise are
inside the encoded chart, and names the IPC-2221 fallback when outside that
range. Rows sort ascending by estimated current so the weakest necks are first;
`min_current_a` turns the table into a pass/flag check. Via barrels are estimated
with IPC-2221 using a stated plating assumption.

`pcb_impedance_estimate` reports IPC-2141 closed-form microstrip or stripline
estimates for each matched `(layer, width)` geometry, or for a hypothetical
`width_mm` plus `layer` when iterating toward a target width. If a pattern
resolves to a differential pair by the existing `_P`/`_N` or `+`/`-` conventions,
it derives the same-layer spacing from route geometry and appends a coupled
differential estimate.

Both tools always print assumptions. Missing stackup copper thickness defaults
to 0.035 mm (1 oz), via plating defaults to 25 µm, and missing dielectric
constant defaults to εr 4.4 only when the height can still be derived or is
provided explicitly. For two-layer boards without stackup, impedance uses board
thickness minus two 35 µm copper foils; multilayer boards without dielectric
thickness refuse the affected layer with an explicit message. Nets carried by
zones are flagged because the pour cross-section is not modeled, so the track
neck may not be the true current limit.

### Live KiCad session (IPC API)

These tools talk to a running KiCad 9 PCB editor through KiCad's official
`kicad-python` (`kipy`) IPC client. They are different from the file-based PCB
model tools above: they operate on the user's visible KiCad GUI session and fail
closed when no IPC server is reachable. To use them, enable KiCad's API server in
KiCad Preferences → Plugins, then keep the target PCB open in the PCB editor. The
server attempts KiCad's default IPC socket, or `KICAD_API_SOCKET` when that
environment variable is set, and every IPC call uses a short timeout so the MCP
server does not hang.

The KiCad 9 Python IPC surface exposes open-document discovery, board item
queries, selection read/write, net queries, and item-by-net queries. It does not
expose a typed zoom or pan command in `kicad-python` 0.7.1, so live focus selects
the target footprint or item and reports that view centering is unavailable rather
than fabricating a GUI state.

| Tool | Description |
|------|-------------|
| `kicad_session` | Report whether KiCad IPC is reachable, the KiCad version, the attempted API socket, and open PCB document paths |
| `kicad_focus` | Select a footprint `reference` or a board `position` (`x_mm`, `y_mm`) in the running PCB editor; view centering is reported as unsupported when the IPC client cannot do it |
| `kicad_highlight_net` | Select all selectable copper items on a live board net so KiCad visibly highlights the routed net in the GUI |
| `kicad_get_selection` | Read the user's current GUI selection as references, nets, item types, and item summaries that compose with parsed-model tools such as `pcb_component` and `pcb_net_route` |
| `kicad_open_board` | Resolves a configured board or PCB path through `KiCadMCPConfig.resolve_pcb_source`, then fails closed because KiCad 9's Python IPC client does not expose an open-board command |

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
