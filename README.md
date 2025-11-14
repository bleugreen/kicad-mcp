# KiCad MCP

Model Context Protocol (MCP) server for KiCad circuit analysis.

## Features

- Load and analyze KiCad schematics
- Multi-board system support
- Component and net exploration
- Connection path tracing
- Dynamic configuration management

## Installation

```bash
uv sync
```

## Usage

The server can be run via the MCP protocol. See the launcher script at `~/.config/kicad_mcp/kicad-mcp-server`.

## Configuration

Configuration files are searched in this priority order:

1. Environment variable: `$KICAD_MCP_CONFIG`
2. Local project: `.kicad_mcp.yaml` (in cwd or parent dirs)
3. Global config: `~/.config/kicad_mcp/config.yaml`
4. Default: empty config (no boards)

Example configuration:

```yaml
boards:
  main:
    path: /path/to/main.kicad_sch
    description: Main board

systems:
  full:
    boards: [main, sense]
    description: Complete system

cache:
  enabled: true
  directory: ~/.cache/kicad_mcp
  check_mtime: true
```

## License

MIT
