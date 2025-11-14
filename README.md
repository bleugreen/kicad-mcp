# KiCad MCP Server

Model Context Protocol (MCP) server for analyzing KiCad schematics. Query components, trace nets, explore connections, and analyze multi-board systems through a simple tool interface.

## Features

- **Circuit Analysis**: Load and analyze KiCad `.kicad_sch` files
- **Component Queries**: Search, filter, and examine components with detailed pin information
- **Net Tracing**: Explore nets, trace signal paths, and find connection routes
- **Multi-Board Systems**: Analyze signals across multiple connected boards
- **Smart Caching**: Optional file caching for faster repeated queries
- **Dynamic Configuration**: Add/remove boards and systems without restarting

## Installation

```bash
# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

**Requirements**: Python 3.10+

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

- **boards**: Named board configurations with paths and descriptions
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
| `add_board` | Add a new board to configuration |
| `remove_board` | Remove a board from configuration |
| `add_system` | Add a new multi-board system |
| `remove_system` | Remove a system from configuration |

### Circuit Analysis (Single Board)

| Tool | Description |
|------|-------------|
| `get_overview` | High-level schematic summary (component count, nets, categories) |
| `list_components` | List all components, optionally filtered by category |
| `list_nets` | List all nets, optionally power nets only |
| `examine_component` | Detailed component info (value, pins, connected nets) |
| `examine_net` | Detailed net info (connections, component types) |
| `check_pin_connection` | Find which net a specific pin connects to |

### Connection Tracing (Single Board)

| Tool | Description |
|------|-------------|
| `trace_connection` | Find connection path between two components |
| `find_connected_components` | Find all components within N hops of a component |

### Multi-Board Analysis

| Tool | Description |
|------|-------------|
| `get_system_overview` | Overview of multi-board system |
| `trace_cross_board_signal` | Trace a signal across multiple boards |

## Usage Examples

### Analyze a Single Board

```python
# Get overview
get_overview(source="main")
# or use direct path:
get_overview(source="/path/to/board.kicad_sch")

# List all ICs
list_components(source="main", category="ICs")

# Examine a specific component
examine_component(source="main", reference="U1")

# Check what IC2 pin 5 connects to
check_pin_connection(source="main", reference="IC2", pin_number="5")
```

### Trace Connections

```python
# Find path between two components
trace_connection(source="main", start_ref="U1", end_ref="U5")

# Find everything connected to a regulator within 2 hops
find_connected_components(source="main", reference="U3", max_hops=2)

# Examine a power net
examine_net(source="main", net_name="VCC")
```

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

### Dynamic Configuration

```python
# Add a new board
add_board(
    name="power",
    path="/path/to/power.kicad_sch",
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

## Source Parameter

Most analysis tools accept a `source` parameter which can be:
- **Board name** from configuration (e.g., `"main"`)
- **Direct file path** to a `.kicad_sch` file (e.g., `"/path/to/board.kicad_sch"`)

System tools use `system_name` to reference configured multi-board systems.

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
pytest

# Format code
black src/

# Type checking
mypy src/
```

## License

MIT
