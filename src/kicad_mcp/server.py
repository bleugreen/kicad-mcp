"KiCad MCP Server with circuit graph functionality."

from pathlib import Path
from typing import Dict, Optional
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .circuit_graph import CircuitGraph
from .multi_board_graph import MultiBoardGraph
from .config import KiCadMCPConfig
from .datasheet_lookup import DatasheetFinder
from . import __version__, kicad_cli, pcb_rendering
from . import pcb_electrical
from .kicad_cli import KiCadCLIError
from .kicad_ipc import KiCadIPC, KiCadIPCError, format_selection, format_session
from .pcb_model import PCBModel, load_pcb_model
from .pcb_route import (
    RouteAnalysis,
    analyze_net_route,
    resolve_diff_pair,
    sorted_length_rows,
)

# PCB tools operate on .kicad_pcb files via kicad-cli, resolving their source
# through the config, so they bypass the schematic/circuit machinery entirely.
PCB_CLI_TOOLS = {"pcb_drc", "pcb_render", "pcb_export_layers"}

# Parsed-model PCB tools query the typed board model (pcb_model) rather than
# shelling out to kicad-cli; they resolve their source through the same config.
PCB_MODEL_TOOLS = {"pcb_overview", "pcb_component", "pcb_components_near"}

# Route-analysis PCB tools build on PCBModel copper elements and report routed
# lengths/connectivity without invoking kicad-cli.
PCB_ROUTE_TOOLS = {"pcb_net_route", "pcb_diff_pair", "pcb_net_lengths"}

# Electrical estimate PCB tools derive ampacity and impedance tables from the
# parsed model and route walk without invoking kicad-cli.
PCB_ELECTRICAL_TOOLS = {"pcb_current_capacity", "pcb_impedance_estimate"}

# PCB image tools render directly from PCBModel geometry to PNG ImageContent.
PCB_IMAGE_TOOLS = {"pcb_crop", "pcb_highlight_net"}

# Live-session tools talk to a running KiCad GUI through the official IPC API.
KICAD_IPC_TOOLS = {
    "kicad_session",
    "kicad_focus",
    "kicad_highlight_net",
    "kicad_get_selection",
    "kicad_open_board",
}


class KiCadMCPServer:
    """MCP server for KiCad schematic analysis."""

    def __init__(self):
        """Initialize the MCP server."""
        self.server = Server("kicad-mcp")
        self.config = KiCadMCPConfig()  # Load configuration
        self.circuits: Dict[str, CircuitGraph] = {}  # Cache loaded circuits
        self.systems: Dict[str, MultiBoardGraph] = {}  # Cache loaded systems
        self.datasheet_finder = DatasheetFinder(self.config.cache_dir)  # Datasheet lookup
        self.kicad_ipc = KiCadIPC(self.config)
        self.setup_handlers()
        self.server.call_tool()(self.handle_call_tool)

    def setup_handlers(self) -> None:
        """Setup tool handlers."""

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """List available tools."""
            return [
                types.Tool(
                    name="search_datasheet",
                    description="Search for component datasheet URL using manufacturer and part number",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "manufacturer": {
                                "type": "string",
                                "description": "Component manufacturer (e.g., 'Texas Instruments', 'STMicroelectronics')"
                            },
                            "part_number": {
                                "type": "string",
                                "description": "Component part number (e.g., 'ADS1299IPAGR', 'STM32F4')"
                            },
                            "force_refresh": {
                                "type": "boolean",
                                "description": "Force new search even if cached result exists",
                                "default": False
                            }
                        },
                        "required": ["manufacturer", "part_number"]
                    }
                ),
                types.Tool(
                    name="list_configured_boards",
                    description="List all boards configured in .kicad_mcp.yaml",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                types.Tool(
                    name="list_configured_systems",
                    description="List all multi-board systems configured in .kicad_mcp.yaml",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                types.Tool(
                    name="load_board",
                    description="Load a board by name from configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "board_name": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense')"
                            }
                        },
                        "required": ["board_name"]
                    }
                ),
                types.Tool(
                    name="load_system",
                    description="Load a multi-board system by name from configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "system_name": {
                                "type": "string",
                                "description": "System name from config (e.g., 'full', 'main-sense')"
                            }
                        },
                        "required": ["system_name"]
                    }
                ),
                types.Tool(
                    name="trace_cross_board_signal",
                    description="Trace a signal across multiple boards in a system",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "system_name": {
                                "type": "string",
                                "description": "System name from config"
                            },
                            "signal_net": {
                                "type": "string",
                                "description": "Signal/net name to trace (e.g., '/MISO')"
                            },
                            "start_component": {
                                "type": "string",
                                "description": "Optional: Starting component reference"
                            },
                            "end_component": {
                                "type": "string",
                                "description": "Optional: Ending component reference"
                            }
                        },
                        "required": ["system_name", "signal_net"]
                    }
                ),
                types.Tool(
                    name="get_system_overview",
                    description="Get an overview of a multi-board system",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "system_name": {
                                "type": "string",
                                "description": "System name from config"
                            }
                        },
                        "required": ["system_name"]
                    }
                ),
                types.Tool(
                    name="reload_config",
                    description="Reload configuration from disk without restarting the server",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                types.Tool(
                    name="add_board",
                    description="Add a new board to the configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Board identifier (e.g., 'my_board')"
                            },
                            "path": {
                                "type": "string",
                                "description": "Path to the .kicad_sch file"
                            },
                            "description": {
                                "type": "string",
                                "description": "Board description (optional)",
                                "default": ""
                            },
                            "pcb": {
                                "type": "string",
                                "description": "Path to the .kicad_pcb layout file (optional)"
                            }
                        },
                        "required": ["name", "path"]
                    }
                ),
                types.Tool(
                    name="remove_board",
                    description="Remove a board from the configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Board identifier to remove"
                            }
                        },
                        "required": ["name"]
                    }
                ),
                types.Tool(
                    name="add_system",
                    description="Add a new multi-board system to the configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "System identifier (e.g., 'my_system')"
                            },
                            "boards": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of board names to include in the system"
                            },
                            "description": {
                                "type": "string",
                                "description": "System description (optional)",
                                "default": ""
                            }
                        },
                        "required": ["name", "boards"]
                    }
                ),
                types.Tool(
                    name="remove_system",
                    description="Remove a system from the configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "System identifier to remove"
                            }
                        },
                        "required": ["name"]
                    }
                ),
                types.Tool(
                    name="pcb_drc",
                    description=(
                        "Run headless Design Rule Check on a PCB and return "
                        "violations grouped by rule with severities, mm "
                        "coordinates, totals, and the JSON report path. "
                        "Fails closed (explicit error) if the run fails rather "
                        "than reporting a false clean pass."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to .kicad_pcb (or sibling .kicad_sch)"
                            },
                            "severity": {
                                "type": "string",
                                "description": "Filter: all, error, warning, or exclusion",
                                "enum": ["all", "error", "warning", "exclusion"],
                                "default": "all"
                            },
                            "max_violations": {
                                "type": "integer",
                                "description": "Cap the number of individual violations listed (totals stay exact)"
                            }
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="pcb_render",
                    description=(
                        "Render a PCB in 3D to a PNG and return the image plus "
                        "the saved file path. Supports camera controls: side, "
                        "zoom, rotate, pan, pivot, perspective, floor."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to .kicad_pcb (or sibling .kicad_sch)"
                            },
                            "side": {
                                "type": "string",
                                "description": "Camera side",
                                "enum": ["top", "bottom", "left", "right", "front", "back"],
                                "default": "top"
                            },
                            "width": {"type": "integer", "description": "Image width in px", "default": 1600},
                            "height": {"type": "integer", "description": "Image height in px", "default": 900},
                            "quality": {
                                "type": "string",
                                "description": "Render quality",
                                "enum": ["basic", "high", "user", "job_settings"],
                                "default": "basic"
                            },
                            "background": {
                                "type": "string",
                                "description": "Background: default, transparent, or opaque",
                                "enum": ["default", "transparent", "opaque"]
                            },
                            "zoom": {"type": "number", "description": "Camera zoom (default 1)"},
                            "rotate": {"type": "string", "description": "Rotate board 'X,Y,Z' e.g. '-45,0,45' for isometric"},
                            "pan": {"type": "string", "description": "Pan camera 'X,Y,Z'"},
                            "pivot": {"type": "string", "description": "Pivot point relative to board center in cm 'X,Y,Z'"},
                            "perspective": {"type": "boolean", "description": "Use perspective projection", "default": False},
                            "floor": {"type": "boolean", "description": "Enable floor, shadows, post-processing", "default": False}
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="pcb_export_layers",
                    description=(
                        "Export one SVG per PCB layer (e.g. F.Cu,B.Cu,Edge.Cuts) "
                        "and return the generated file paths. Defaults to "
                        "board-area fit for downstream cropping."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to .kicad_pcb (or sibling .kicad_sch)"
                            },
                            "layers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Untranslated layer names, e.g. ['F.Cu','B.Cu','Edge.Cuts']"
                            },
                            "output_dir": {
                                "type": "string",
                                "description": "Directory to write SVGs into (default: a per-run temp dir)"
                            },
                            "fit": {
                                "type": "string",
                                "description": "Page sizing: board (board area only), page (framed page), or current",
                                "enum": ["board", "page", "current"],
                                "default": "board"
                            },
                            "black_and_white": {"type": "boolean", "description": "Plot black and white only", "default": False}
                        },
                        "required": ["source", "layers"]
                    }
                ),
                types.Tool(
                    name="pcb_overview",
                    description="Get a PCB layout overview: board dimensions, layer/stackup summary, footprint/track/via/zone counts, net count, and top nets by copper element count.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to a .kicad_pcb file"
                            }
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="pcb_component",
                    description="Get a component's PCB placement (position, side, rotation), footprint id, and pads with their nets.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to a .kicad_pcb file"
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference designator (e.g., 'R1', 'U3')"
                            }
                        },
                        "required": ["source", "reference"]
                    }
                ),
                types.Tool(
                    name="pcb_components_near",
                    description="Find footprints placed within a radius (mm) of a given component, with distances.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config OR path to a .kicad_pcb file"
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference designator to search around"
                            },
                            "radius_mm": {
                                "type": "number",
                                "description": "Search radius in millimetres",
                                "default": 5.0
                            }
                        },
                        "required": ["source", "reference"]
                    }
                ),
                types.Tool(
                    name="pcb_net_route",
                    description="Analyze one PCB net's routed copper length, layer usage, widths, vias, endpoints, and copper-island connectivity.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Board name from config OR path to a .kicad_pcb file"},
                            "net": {"type": "string", "description": "Net name or net number"}
                        },
                        "required": ["source", "net"]
                    }
                ),
                types.Tool(
                    name="pcb_diff_pair",
                    description="Compare routed lengths for a differential pair. Pass explicit net_p/net_n or pass net_p as the base name using _P/_N or +/- conventions.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Board name from config OR path to a .kicad_pcb file"},
                            "net_p": {"type": "string", "description": "Positive net name, or pair base name when net_n is omitted"},
                            "net_n": {"type": "string", "description": "Negative net name (optional when net_p is a base name)"}
                        },
                        "required": ["source", "net_p"]
                    }
                ),
                types.Tool(
                    name="pcb_net_lengths",
                    description="List routed lengths for nets whose names match a glob or regular expression, sorted by length for bus matching review.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Board name from config OR path to a .kicad_pcb file"},
                            "pattern": {"type": "string", "description": "Glob or regular expression, e.g. DDR_*"},
                            "limit": {"type": "integer", "description": "Maximum number of matching nets to report", "default": 50}
                        },
                        "required": ["source", "pattern"]
                    }
                ),

                types.Tool(
                    name="pcb_current_capacity",
                    description=(
                        "Estimate current capacity for nets matching a glob or regex, "
                        "sorted weakest first, using IPC-2152 conservative chart fits "
                        "with IPC-2221 fallback."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Board name from config OR path to a .kicad_pcb file"},
                            "pattern": {"type": "string", "description": "Glob or regular expression matching net names"},
                            "temp_rise_c": {"type": "number", "description": "Allowed copper temperature rise in °C", "default": 10},
                            "min_current_a": {"type": "number", "description": "Optional pass/flag threshold in amps"},
                            "plating_um": {"type": "number", "description": "Assumed via barrel plating thickness in µm", "default": 25},
                            "limit": {"type": "integer", "description": "Maximum number of matching nets to report", "default": 50},
                        },
                        "required": ["source", "pattern"]
                    }
                ),
                types.Tool(
                    name="pcb_impedance_estimate",
                    description=(
                        "Estimate single-ended and differential impedance with IPC-2141 "
                        "closed-form formulas for matching nets, or a hypothetical width/layer."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Board name from config OR path to a .kicad_pcb file"},
                            "pattern": {"type": "string", "description": "Glob or regular expression matching net names"},
                            "width_mm": {"type": "number", "description": "Hypothetical trace width in millimetres"},
                            "layer": {"type": "string", "description": "Hypothetical trace layer, e.g. F.Cu"},
                            "er": {"type": "number", "description": "Override dielectric constant"},
                            "dielectric_h_mm": {"type": "number", "description": "Override dielectric height to reference plane in millimetres"},
                            "limit": {"type": "integer", "description": "Maximum number of matching nets to report", "default": 50},
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="pcb_crop",
                    description=(
                        "Render a 2D PNG crop of a PCB region as ImageContent. "
                        "Select exactly one target: reference plus margin_mm, "
                        "net plus margin_mm, or explicit x_mm/y_mm/width_mm/height_mm. "
                        "Layers default to the target side copper plus silkscreen "
                        "and Edge.Cuts for reference crops, or all "
                        "copper plus Edge.Cuts otherwise."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name, .kicad_pcb path, or sibling .kicad_sch",
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference designator to crop around",
                            },
                            "net": {"description": "Net name or number to crop around"},
                            "x_mm": {
                                "type": "number",
                                "description": "Explicit crop origin X in board millimetres",
                            },
                            "y_mm": {
                                "type": "number",
                                "description": "Explicit crop origin Y in board millimetres",
                            },
                            "width_mm": {
                                "type": "number",
                                "description": "Explicit crop width in millimetres",
                            },
                            "height_mm": {
                                "type": "number",
                                "description": "Explicit crop height in millimetres",
                            },
                            "margin_mm": {
                                "type": "number",
                                "description": "Margin around reference/net crop",
                                "default": 5.0,
                            },
                            "layers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Layer names, e.g. ['F.Cu','Edge.Cuts']",
                            },
                            "width_px": {
                                "type": "integer",
                                "description": "Long-edge pixel target capped at 1600",
                                "default": 1200,
                            },
                            "output_dir": {
                                "type": "string",
                                "description": "PNG output directory; defaults to a temp dir",
                            },
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="pcb_highlight_net",
                    description=(
                        "Render a 2D PNG with one net's tracks, vias, pads, and "
                        "zones drawn bright over a dimmed board. Defaults to the "
                        "whole board and all copper layers."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name, .kicad_pcb path, or sibling .kicad_sch",
                            },
                            "net": {"description": "Net name or number to highlight"},
                            "x_mm": {
                                "type": "number",
                                "description": "Optional bbox origin X in board millimetres",
                            },
                            "y_mm": {
                                "type": "number",
                                "description": "Optional bbox origin Y in board millimetres",
                            },
                            "width_mm": {
                                "type": "number",
                                "description": "Optional bbox width in millimetres",
                            },
                            "height_mm": {
                                "type": "number",
                                "description": "Optional bbox height in millimetres",
                            },
                            "layers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Layer names, e.g. ['F.Cu','B.Cu','Edge.Cuts']",
                            },
                            "width_px": {
                                "type": "integer",
                                "description": "Long-edge pixel target capped at 1600",
                                "default": 1200,
                            },
                            "output_dir": {
                                "type": "string",
                                "description": "PNG output directory; defaults to a temp dir",
                            },
                        },
                        "required": ["source", "net"]
                    }
                ),
                types.Tool(
                    name="kicad_session",
                    description="Report live KiCad IPC reachability, version, attempted socket, and open PCB documents.",
                    inputSchema={"type": "object", "properties": {}},
                ),
                types.Tool(
                    name="kicad_focus",
                    description="Select a footprint reference or board position in the running KiCad PCB editor. View zoom/pan is reported when unsupported by the IPC client.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "reference": {"type": "string", "description": "Footprint reference designator, e.g. U3"},
                            "position": {
                                "type": "object",
                                "description": "Board position in millimetres",
                                "properties": {
                                    "x_mm": {"type": "number"},
                                    "y_mm": {"type": "number"},
                                },
                                "required": ["x_mm", "y_mm"],
                            },
                        },
                    },
                ),
                types.Tool(
                    name="kicad_highlight_net",
                    description="Select all selectable copper items on a net in the running KiCad PCB editor so the GUI highlights them live.",
                    inputSchema={
                        "type": "object",
                        "properties": {"net": {"type": "string", "description": "Net name"}},
                        "required": ["net"],
                    },
                ),
                types.Tool(
                    name="kicad_get_selection",
                    description="Read the user's current live KiCad PCB selection as references, nets, item types, and item summaries.",
                    inputSchema={"type": "object", "properties": {}},
                ),
                types.Tool(
                    name="kicad_open_board",
                    description="Resolve a PCB source and fail closed if the installed KiCad IPC client cannot open documents.",
                    inputSchema={
                        "type": "object",
                        "properties": {"source": {"type": "string", "description": "Configured board name or .kicad_pcb/.kicad_sch path"}},
                        "required": ["source"],
                    },
                ),
            ]

    async def handle_call_tool(
        self, name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handle tool calls."""

        # Some tools don't require arguments
        if not arguments:
            arguments = {}

        # PCB tools run kicad-cli against a .kicad_pcb; they don't touch the
        # schematic circuit graph, so dispatch them before the standard flow.
        if name in PCB_CLI_TOOLS:
            return self._handle_pcb_tool(name, arguments)

        if name in PCB_MODEL_TOOLS:
            return self._handle_pcb_model_tool(name, arguments)

        if name in PCB_ROUTE_TOOLS:
            return self._handle_pcb_route_tool(name, arguments)

        if name in PCB_ELECTRICAL_TOOLS:
            return self._handle_pcb_electrical_tool(name, arguments)

        if name in PCB_IMAGE_TOOLS:
            return self._handle_pcb_image_tool(name, arguments)

        if name in KICAD_IPC_TOOLS:
            return self._handle_kicad_ipc_tool(name, arguments)

        try:
            if name == "search_datasheet":
                manufacturer = arguments.get("manufacturer", "")
                part_number = arguments.get("part_number", "")
                force_refresh = arguments.get("force_refresh", False)

                if not manufacturer or not part_number:
                    result = "Error: Both manufacturer and part_number are required"
                else:
                    result = f"# Datasheet Search: {part_number}\n\n"
                    result += f"**Manufacturer:** {manufacturer}\n"
                    result += f"**Part Number:** {part_number}\n\n"

                    try:
                        url = self.datasheet_finder.find_datasheet(
                            manufacturer,
                            part_number,
                            use_cache=not force_refresh
                        )

                        if url:
                            result += f"**Datasheet URL:** {url}\n"
                            if force_refresh:
                                result += "\n*(Result refreshed and cached)*"
                            else:
                                result += "\n*(Result may be from cache)*"
                        else:
                            result += "**Status:** No datasheet found\n\n"
                            result += "Try searching directly or check manufacturer/part number spelling."
                    except Exception as e:
                        result += f"**Error:** Search failed: {str(e)}"

            elif name == "list_configured_boards":
                boards = self.config.list_boards()
                result = "# Configured Boards\n\n"
                if boards:
                    for board in boards:
                        result += f"- {board}\n"
                else:
                    result += "No boards configured. Create a .kicad_mcp.yaml file."

            elif name == "list_configured_systems":
                systems = self.config.list_systems()
                result = "# Configured Systems\n\n"
                if systems:
                    for system in systems:
                        result += f"- {system}\n"
                else:
                    result += "No systems configured. Create a .kicad_mcp.yaml file."

            elif name == "load_board":
                board_name = arguments.get("board_name")
                circuit = self.config.load_board(board_name)

                if circuit:
                    # Cache it for later use
                    self.circuits[board_name] = circuit
                    result = circuit.get_info_text()
                else:
                    result = f"Board '{board_name}' not found in configuration"

            elif name == "load_system":
                system_name = arguments.get("system_name")
                system = self.config.load_system(system_name)

                if system:
                    # Cache it for later use
                    self.systems[system_name] = system
                    result = system.get_overview()
                else:
                    result = f"System '{system_name}' not found in configuration"

            elif name == "trace_cross_board_signal":
                system_name = arguments.get("system_name")
                signal_net = arguments.get("signal_net")
                start_comp = arguments.get("start_component")
                end_comp = arguments.get("end_component")

                # Load or get cached system
                if system_name in self.systems:
                    system = self.systems[system_name]
                else:
                    system = self.config.load_system(system_name)
                    if system:
                        self.systems[system_name] = system

                if not system:
                    result = f"System '{system_name}' not found"
                else:
                    path = system.trace_signal_path(signal_net, start_comp, end_comp)
                    if path:
                        result = f"# Signal Path: {signal_net}\n\n"
                        prev_board = None
                        for i, node in enumerate(path):
                            if ':' in str(node):
                                # Component node
                                board, ref = node.split(':', 1)
                                comp = system.boards[board].netlist.components.get(ref)
                                value = comp.value if comp else 'unknown'

                                # Add arrow if not first component
                                if i > 0:
                                    result += "  ↓\n"

                                # For passive components, show the "other" net (not the signal being traced)
                                other_net_str = ""
                                if system.boards[board]._is_passive_component(ref):
                                    nets = system.boards[board].get_nets_of_component(ref)
                                    # Filter out the signal net we're tracing
                                    # The signal_net might be just the net name or board:net_name
                                    base_signal_net = signal_net.split(':')[-1] if ':' in signal_net else signal_net
                                    other_nets = [n for n in nets if n != base_signal_net]
                                    if other_nets:
                                        other_net_str = f" → [{', '.join(other_nets)}]"

                                result += f"**{node}** ({value}){other_net_str}\n"
                                prev_board = board
                            else:
                                # Net node (board transition)
                                boards = system.get_connected_boards(node)
                                if len(boards) > 1:
                                    result += f"  → {node} [crosses: {' ↔ '.join(boards)}]\n"
                                else:
                                    result += f"  → {node}\n"
                    else:
                        result = f"No path found for signal {signal_net}"

            elif name == "get_system_overview":
                system_name = arguments.get("system_name")

                # Load or get cached system
                if system_name in self.systems:
                    system = self.systems[system_name]
                else:
                    system = self.config.load_system(system_name)
                    if system:
                        self.systems[system_name] = system

                if system:
                    result = system.get_overview()
                else:
                    result = f"System '{system_name}' not found"

            elif name == "reload_config":
                self.config.reload_config()
                # Clear server-level caches too
                self.circuits.clear()
                self.systems.clear()
                result = f"# Configuration Reloaded\n\n"
                result += f"Config file: {self.config.config_path}\n\n"
                boards = self.config.list_boards()
                systems = self.config.list_systems()
                result += f"**Boards:** {len(boards)}\n"
                result += f"**Systems:** {len(systems)}\n"

            elif name == "add_board":
                board_name = arguments.get("name")
                board_path = arguments.get("path")
                board_desc = arguments.get("description", "")

                # Add the board
                board_pcb = arguments.get("pcb")
                self.config.add_board(board_name, board_path, board_desc, board_pcb)

                result = f"# Board Added\n\n"
                result += f"**Name:** {board_name}\n"
                result += f"**Path:** {board_path}\n"
                result += f"**Description:** {board_desc}\n"
                if board_pcb:
                    result += f"**PCB:** {board_pcb}\n"
                result += f"\nSaved to: {self.config.config_path}\n"

            elif name == "remove_board":
                board_name = arguments.get("name")

                # Remove from server cache if present
                if board_name in self.circuits:
                    del self.circuits[board_name]

                # Remove from config
                if self.config.remove_board(board_name):
                    result = f"Board '{board_name}' removed from configuration"
                else:
                    result = f"Board '{board_name}' not found in configuration"

            elif name == "add_system":
                system_name = arguments.get("name")
                system_boards = arguments.get("boards", [])
                system_desc = arguments.get("description", "")

                # Add the system
                self.config.add_system(system_name, system_boards, system_desc)

                result = f"# System Added\n\n"
                result += f"**Name:** {system_name}\n"
                result += f"**Boards:** {', '.join(system_boards)}\n"
                result += f"**Description:** {system_desc}\n\n"
                result += f"Saved to: {self.config.config_path}\n"

            elif name == "remove_system":
                system_name = arguments.get("name")

                # Remove from server cache if present
                if system_name in self.systems:
                    del self.systems[system_name]

                # Remove from config
                if self.config.remove_system(system_name):
                    result = f"System '{system_name}' removed from configuration"
                else:
                    result = f"System '{system_name}' not found in configuration"

            else:
                result = f"Unknown tool: {name}"

            return [types.TextContent(type="text", text=result)]

        except Exception as e:
            return [types.TextContent(
                type="text",
                text=f"Error executing {name}: {str(e)}"
            )]

    def _handle_kicad_ipc_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch live KiCad IPC tools."""
        try:
            if name == "kicad_session":
                result = format_session(self.kicad_ipc.session())
            elif name == "kicad_focus":
                result = format_selection(
                    self.kicad_ipc.focus(
                        reference=arguments.get("reference"),
                        position=arguments.get("position"),
                    )["selection"],
                    title="Live KiCad Focus",
                )
            elif name == "kicad_highlight_net":
                net = arguments.get("net")
                if not net:
                    return [types.TextContent(type="text", text="Error: net parameter is required")]
                data = self.kicad_ipc.highlight_net(str(net))
                result = format_selection(
                    data["selection"],
                    title=f"Live KiCad Net Highlight: {data['net']}",
                )
            elif name == "kicad_get_selection":
                result = format_selection(self.kicad_ipc.get_selection())
            elif name == "kicad_open_board":
                result = str(self.kicad_ipc.open_board(arguments.get("source", "")))
            else:
                result = f"Unknown live KiCad IPC tool: {name}"
            return [types.TextContent(type="text", text=result)]
        except (KiCadIPCError, ValueError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error executing {name}: {e}")]

    def _handle_pcb_model_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch the parsed-model PCB tools (overview/component/near).

        These query the in-memory :class:`PCBModel` (no kicad-cli). Source
        resolution failures surface as an explicit ``Error:`` TextContent.
        """
        source = arguments.get("source")
        if not source:
            return [types.TextContent(type="text", text="Error: source parameter is required")]
        try:
            model = self._load_pcb(source)
        except (ValueError, FileNotFoundError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

        if name == "pcb_overview":
            result = self._format_pcb_overview(model)
        elif name == "pcb_component":
            result = self._format_pcb_component(model, arguments.get("reference"))
        else:  # pcb_components_near
            result = self._format_pcb_components_near(
                model, arguments.get("reference"), arguments.get("radius_mm", 5.0)
            )
        return [types.TextContent(type="text", text=result)]

    def _handle_pcb_route_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch route-analysis PCB tools."""
        source = arguments.get("source")
        if not source:
            return [types.TextContent(type="text", text="Error: source parameter is required")]
        try:
            model = self._load_pcb(source)
            if name == "pcb_net_route":
                net = arguments.get("net")
                if net is None:
                    return [types.TextContent(type="text", text="Error: net parameter is required")]
                result = self._format_pcb_net_route(analyze_net_route(model, net))
            elif name == "pcb_diff_pair":
                net_p_arg = arguments.get("net_p")
                if not net_p_arg:
                    return [types.TextContent(type="text", text="Error: net_p parameter is required")]
                net_p, net_n = resolve_diff_pair(model, net_p_arg, arguments.get("net_n"))
                result = self._format_pcb_diff_pair(
                    analyze_net_route(model, net_p), analyze_net_route(model, net_n)
                )
            else:  # pcb_net_lengths
                pattern = arguments.get("pattern")
                if not pattern:
                    return [types.TextContent(type="text", text="Error: pattern parameter is required")]
                limit = int(arguments.get("limit", 50))
                result = self._format_pcb_net_lengths(
                    pattern, sorted_length_rows(model, pattern, limit), limit
                )
            return [types.TextContent(type="text", text=result)]
        except (ValueError, FileNotFoundError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error executing {name}: {e}")]


    def _handle_pcb_electrical_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch derived electrical estimate PCB tools."""
        source = arguments.get("source")
        if not source:
            return [types.TextContent(type="text", text="Error: source parameter is required")]
        try:
            model = self._load_pcb(source)
            if name == "pcb_current_capacity":
                pattern = arguments.get("pattern")
                if not pattern:
                    return [types.TextContent(type="text", text="Error: pattern parameter is required")]
                reports = pcb_electrical.capacity_reports_for_pattern(
                    model,
                    pattern,
                    temp_rise_c=float(arguments.get("temp_rise_c", 10.0)),
                    plating_um=float(arguments.get("plating_um", 25.0)),
                    limit=int(arguments.get("limit", 50)),
                )
                result = self._format_pcb_current_capacity(
                    pattern,
                    reports,
                    int(arguments.get("limit", 50)),
                    arguments.get("min_current_a"),
                )
            else:
                pattern = arguments.get("pattern")
                width = arguments.get("width_mm")
                layer = arguments.get("layer")
                er = arguments.get("er")
                dielectric_h = arguments.get("dielectric_h_mm")
                if pattern:
                    report = pcb_electrical.impedance_reports_for_pattern(
                        model,
                        pattern,
                        limit=int(arguments.get("limit", 50)),
                        er=float(er) if er is not None else None,
                        dielectric_h_mm=float(dielectric_h) if dielectric_h is not None else None,
                    )
                    result = self._format_pcb_impedance(pattern, report)
                elif width is not None and layer:
                    report = pcb_electrical.hypothetical_impedance(
                        model,
                        str(layer),
                        float(width),
                        er=float(er) if er is not None else None,
                        dielectric_h_mm=float(dielectric_h) if dielectric_h is not None else None,
                    )
                    result = self._format_pcb_impedance("hypothetical trace", report)
                else:
                    return [
                        types.TextContent(
                            type="text",
                            text="Error: pass either pattern or width_mm plus layer",
                        )
                    ]
            return [types.TextContent(type="text", text=result)]
        except (ValueError, FileNotFoundError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error executing {name}: {e}")]

    def _handle_pcb_image_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch PCB image tools (crop/highlight) backed by direct rendering."""
        source = arguments.get("source")
        if not source:
            return [types.TextContent(type="text", text="Error: source parameter is required")]

        try:
            model = self._load_pcb(source)
            if name == "pcb_crop":
                result = pcb_rendering.render_crop(
                    model,
                    reference=arguments.get("reference"),
                    net=arguments.get("net"),
                    x_mm=arguments.get("x_mm"),
                    y_mm=arguments.get("y_mm"),
                    width_mm=arguments.get("width_mm"),
                    height_mm=arguments.get("height_mm"),
                    margin_mm=arguments.get("margin_mm", 5.0),
                    layers=arguments.get("layers"),
                    width_px=arguments.get("width_px", 1200),
                    output_dir=arguments.get("output_dir"),
                )
                title = "PCB Crop"
            elif name == "pcb_highlight_net":
                if "net" not in arguments:
                    return [
                        types.TextContent(
                            type="text", text="Error: net parameter is required"
                        )
                    ]
                result = pcb_rendering.render_highlight_net(
                    model,
                    net=arguments.get("net"),
                    x_mm=arguments.get("x_mm"),
                    y_mm=arguments.get("y_mm"),
                    width_mm=arguments.get("width_mm"),
                    height_mm=arguments.get("height_mm"),
                    layers=arguments.get("layers"),
                    width_px=arguments.get("width_px", 1200),
                    output_dir=arguments.get("output_dir"),
                )
                title = f"PCB Net Highlight: {arguments.get('net')}"
            else:
                return [
                    types.TextContent(
                        type="text", text=f"Unknown PCB image tool: {name}"
                    )
                ]

            return [
                types.ImageContent(
                    type="image",
                    data=result.image_base64,
                    mimeType="image/png",
                ),
                types.TextContent(
                    type="text", text=pcb_rendering.result_caption(title, result)
                ),
            ]
        except (ValueError, FileNotFoundError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error executing {name}: {e}")]

    def _load_pcb(self, source: str) -> PCBModel:
        """Resolve a source to a board file and return its cached parsed model.

        Resolution routes through :meth:`KiCadMCPConfig.resolve_pcb_source`;
        parsing is cached in-memory keyed by (path, mtime) by
        :func:`load_pcb_model`.
        """
        path = self.config.resolve_pcb_source(source)
        return load_pcb_model(path)

    @staticmethod
    def _format_pcb_overview(model: PCBModel) -> str:
        name = Path(str(model.path)).name if model.path else "board"
        lines = [f"# PCB Overview: {name}", ""]

        dims = model.board_dimensions()
        bbox = model.bounding_box()
        if dims and bbox:
            lines.append(
                f"**Dimensions:** {dims[0]:.2f} x {dims[1]:.2f} mm "
                f"(bbox {bbox.min_x:.2f},{bbox.min_y:.2f} to "
                f"{bbox.max_x:.2f},{bbox.max_y:.2f})"
            )
        else:
            lines.append("**Dimensions:** unknown (no Edge.Cuts outline found)")

        if model.board_thickness is not None:
            lines.append(f"**Board thickness:** {model.board_thickness} mm")
        lines.append(
            f"**Copper layers:** {len(model.copper_layers)} "
            f"({', '.join(model.copper_layers)})"
        )
        lines.append(f"**Total layers defined:** {len(model.layers)}")

        if model.stackup:
            lines.append("\n## Stackup")
            for s in model.stackup:
                thickness = f", {s.thickness} mm" if s.thickness is not None else ""
                material = f", {s.material}" if s.material else ""
                lines.append(f"- {s.name} ({s.type}){thickness}{material}")

        lines.append("\n## Counts")
        lines.append(f"- Footprints: {len(model.footprints)}")
        lines.append(f"- Tracks (segments): {len(model.tracks)}")
        lines.append(f"- Arcs: {len(model.arcs)}")
        lines.append(f"- Vias: {len(model.vias)}")
        lines.append(f"- Zones: {len(model.zones)}")
        lines.append(f"- Nets: {len(model.nets)}")

        top = model.top_nets(10)
        if top:
            lines.append("\n## Top nets by copper element count")
            for num, net_name, count in top:
                label = net_name if net_name else f"(net {num})"
                lines.append(f"- {label}: {count}")

        return "\n".join(lines)

    @staticmethod
    def _format_pcb_component(model: PCBModel, reference: Optional[str]) -> str:
        if not reference:
            return "Error: reference is required"
        fp = model.footprint(reference)
        if fp is None:
            return f"Component {reference} not found on the PCB"

        lines = [f"# Component: {fp.reference}", ""]
        lines.append(f"**Value:** {fp.value}")
        lines.append(f"**Footprint:** {fp.lib_id}")
        lines.append(f"**Side:** {fp.side}")
        lines.append(
            f"**Position:** ({fp.position.x:.3f}, {fp.position.y:.3f}) mm"
        )
        lines.append(f"**Rotation:** {fp.rotation:.1f}°")

        lines.append(f"\n## Pads ({len(fp.pads)})")
        for pad in fp.pads:
            net = pad.net_name if pad.net_name else "(unconnected)"
            lines.append(
                f"- Pad {pad.number} [{pad.pad_type}] → {net} "
                f"@ ({pad.position.x:.3f}, {pad.position.y:.3f})"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pcb_components_near(
        model: PCBModel, reference: Optional[str], radius_mm: float
    ) -> str:
        if not reference:
            return "Error: reference is required"
        if model.footprint(reference) is None:
            return f"Component {reference} not found on the PCB"

        neighbors = model.footprints_near(reference, radius_mm)
        lines = [
            f"# Components within {radius_mm} mm of {reference}",
            "",
            f"Found {len(neighbors)} component(s):",
            "",
        ]
        for fp, dist in neighbors:
            lines.append(
                f"- {fp.reference} ({fp.value}) — {dist:.2f} mm [{fp.side}]"
            )
        return "\n".join(lines)


    @staticmethod
    def _format_pcb_current_capacity(
        pattern: str,
        reports: list[pcb_electrical.NetCapacityReport],
        limit: int,
        min_current_a: object | None,
    ) -> str:
        threshold = float(min_current_a) if min_current_a is not None else None
        lines = [
            f"# Current Capacity Estimates: {pattern}",
            "",
            "Estimates use IPC-2152 conservative chart fits when in range, "
            "falling back to IPC-2221; this is not thermal simulation.",
            f"Matched {len(reports)} net(s), capped at {limit}.",
        ]
        if threshold is not None:
            lines.append(f"Rows below {threshold:.3f} A are flagged with ⚠.")
        lines.append("")
        if not reports:
            lines.append("No nets matched.")
            return "\n".join(lines)
        lines.append("| Net | Neck (width @ layer) | Est. max A (standard) | Via limit (A × n) | Length | Flags |")
        lines.append("| --- | --- | ---: | --- | ---: | --- |")
        for report in reports:
            neck = report.neck
            if neck is None:
                neck_label = "no tracks"
                current = "n/a"
                fail = False
            else:
                neck_label = f"{neck.width_mm:.3f} mm @ {neck.layer}"
                fail = threshold is not None and neck.estimated_a < threshold
                current = f"{neck.estimated_a:.3f} ({neck.standard})"
            via = "; ".join(
                f"{item.per_via_a:.3f} × {item.count} ({item.span})"
                for item in report.via_limits
            ) or "none"
            flags = list(report.flags)
            if fail:
                flags.insert(0, "⚠ below threshold")
            lines.append(
                f"| {report.net_name} | {neck_label} | {current} | {via} | "
                f"{report.total_length_mm:.3f} mm | {', '.join(flags) or 'none'} |"
            )
        if len(reports) == 1:
            report = reports[0]
            lines.extend(["", f"## Segment detail: {report.net_name}", ""])
            lines.append("| Layer | Width | Length | Copper | Area | IPC-2152 | IPC-2221 | Selected |")
            lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
            for seg in report.segments:
                ipc2152 = f"{seg.ipc2152_a:.3f} A" if seg.ipc2152_a is not None else "out of range"
                lines.append(
                    f"| {seg.layer} | {seg.width_mm:.3f} mm | {seg.length_mm:.3f} mm | "
                    f"{seg.copper_thickness_mm:.3f} mm | {seg.area_mil2:.2f} mil² | "
                    f"{ipc2152} | {seg.ipc2221_a:.3f} A | {seg.estimated_a:.3f} A ({seg.standard}) |"
                )
        assumptions = list(dict.fromkeys(a for report in reports for a in report.assumptions))
        lines.extend(["", "## Assumptions", ""])
        lines.extend(f"- {item}" for item in assumptions)
        return "\n".join(lines)

    @staticmethod
    def _format_pcb_impedance(
        label: str, report: pcb_electrical.ImpedanceReport
    ) -> str:
        lines = [
            f"# Impedance Estimate: {label}",
            "",
            "IPC-2141 closed-form estimate — catches a 90 Ω pair routed as "
            "60 Ω; not a field solver.",
            "",
        ]
        if report.messages:
            lines.extend(f"- {message}" for message in report.messages)
            lines.append("")
        if not report.rows:
            lines.append("No impedance rows available.")
        else:
            lines.append("| Net | Layer | Width | Z0 (Ω) | Model | Notes |")
            lines.append("| --- | --- | ---: | ---: | --- | --- |")
            for row in report.rows:
                z0 = f"{row.z0_ohm:.2f}" if row.z0_ohm is not None else "n/a"
                notes = list(row.notes)
                if row.dielectric_h_mm is not None and row.er is not None:
                    notes.append(f"h={row.dielectric_h_mm:.3f} mm, εr={row.er:.3f}")
                lines.append(
                    f"| {row.net_name} | {row.layer} | {row.width_mm:.3f} mm | "
                    f"{z0} | {row.model} | {', '.join(notes) or 'none'} |"
                )
        if report.coupled_rows:
            lines.extend(["", "## Coupled differential estimate", ""])
            lines.append("| Layer | Derived gap | Center spacing | Zdiff (Ω) | Notes |")
            lines.append("| --- | ---: | ---: | ---: | --- |")
            for row in report.coupled_rows:
                lines.append(
                    f"| {row.layer} | {row.gap_mm:.3f} mm | "
                    f"{row.spacing_center_mm:.3f} mm | {row.zdiff_ohm:.2f} | "
                    f"{', '.join(row.notes) or 'none'} |"
                )
        lines.extend(["", "## Assumptions", ""])
        if report.assumptions:
            lines.extend(f"- {item}" for item in report.assumptions)
        else:
            lines.append("- none")
        return "\n".join(lines)

    @staticmethod
    def _format_pcb_net_route(route: RouteAnalysis) -> str:
        label = route.net_name or f"net {route.net_number}"
        lines = [f"# Route: {label}", ""]
        lines.append(f"**Total routed length:** {route.total_length_mm:.6f} mm")
        lines.append(
            f"**Elements:** {route.track_count} segment(s), {route.arc_count} arc(s), {route.via_count} via(s)"
        )
        lines.append(f"**Layers used:** {', '.join(route.layers_used) if route.layers_used else 'none'}")
        lines.append(f"**Endpoint pads:** {', '.join(route.endpoints) if route.endpoints else 'none'}")

        status = "connected" if route.copper_island_count <= 1 else "disconnected"
        if route.connected_only_through_zone:
            status = "connected only through zone; routed length through pour is undefined"
        lines.append(
            f"**Connectivity:** {status} ({route.copper_island_count} measured copper island(s), tolerance {route.tolerance_mm} mm)"
        )
        if route.zones:
            zone_layers = sorted({layer for zone in route.zones for layer in zone.layers})
            lines.append(
                f"**Zones:** {len(route.zones)} zone(s) on {', '.join(zone_layers) if zone_layers else 'unknown layers'}; excluded from length math"
            )

        lines.append("\n## Layer lengths")
        if route.layer_lengths_mm:
            for layer, length in route.layer_lengths_mm.items():
                lines.append(f"- {layer}: {length:.6f} mm")
        else:
            lines.append("- none")

        lines.append("\n## Width profile")
        if route.min_width_mm is None:
            lines.append("- no routed segments/arcs")
        else:
            lines.append(f"- Min/max: {route.min_width_mm:.6f} / {route.max_width_mm:.6f} mm")
            for width, length in route.width_lengths_mm.items():
                lines.append(f"- {width:.6f} mm: {length:.6f} mm")

        if route.via_spans:
            lines.append("\n## Via spans")
            for span, count in route.via_spans.items():
                lines.append(f"- {span}: {count}")

        if route.copper_island_count > 1:
            lines.append("\n## Copper islands")
            for idx, island in enumerate(route.islands, 1):
                lines.append(f"- Island {idx}: {island.summary()}")
        return "\n".join(lines)

    @staticmethod
    def _format_pcb_diff_pair(pos: RouteAnalysis, neg: RouteAnalysis) -> str:
        mismatch = abs(pos.total_length_mm - neg.total_length_mm)
        lines = [f"# Differential Pair: {pos.net_name} / {neg.net_name}", ""]
        lines.append(f"- {pos.net_name}: {pos.total_length_mm:.6f} mm, {pos.via_count} via(s)")
        lines.append(f"- {neg.net_name}: {neg.total_length_mm:.6f} mm, {neg.via_count} via(s)")
        lines.append(f"**Length mismatch:** {mismatch:.6f} mm")
        via_note = "symmetric" if pos.via_count == neg.via_count else "different"
        lines.append(f"**Via-count symmetry:** {via_note}")
        p_layers = set(pos.layers_used)
        n_layers = set(neg.layers_used)
        if p_layers == n_layers:
            lines.append(f"**Layer usage:** matched ({', '.join(sorted(p_layers))})")
        else:
            lines.append(
                "**Layer usage differs:** "
                f"only {pos.net_name}: {', '.join(sorted(p_layers - n_layers)) or 'none'}; "
                f"only {neg.net_name}: {', '.join(sorted(n_layers - p_layers)) or 'none'}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pcb_net_lengths(
        pattern: str, rows: list[RouteAnalysis], limit: int
    ) -> str:
        lines = [f"# Net Lengths: {pattern}", "", f"Matched {len(rows)} net(s), capped at {limit}.", ""]
        if not rows:
            lines.append("No nets matched.")
            return "\n".join(lines)
        lines.append("| Net | Length (mm) | Vias | Islands | Layers |")
        lines.append("| --- | ---: | ---: | ---: | --- |")
        for route in rows:
            lines.append(
                f"| {route.net_name} | {route.total_length_mm:.6f} | {route.via_count} | {route.copper_island_count} | {', '.join(route.layers_used)} |"
            )
        return "\n".join(lines)

    def _handle_pcb_tool(
        self, name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Dispatch the kicad-cli-backed PCB tools (drc/render/export).

        All failures surface as an explicit ``Error:`` TextContent so the DRC
        tool never reports a false clean pass.
        """
        source = arguments.get("source")
        if not source:
            return [types.TextContent(type="text", text="Error: source parameter is required")]

        try:
            if name == "pcb_drc":
                report = kicad_cli.run_drc(
                    source,
                    severity=arguments.get("severity", "all"),
                    max_violations=arguments.get("max_violations"),
                    config=self.config,
                )
                return [types.TextContent(type="text", text=self._format_drc(report))]

            if name == "pcb_render":
                result = kicad_cli.render_pcb_base64(
                    source,
                    side=arguments.get("side", "top"),
                    width=arguments.get("width", 1600),
                    height=arguments.get("height", 900),
                    quality=arguments.get("quality", "basic"),
                    background=arguments.get("background"),
                    zoom=arguments.get("zoom"),
                    rotate=arguments.get("rotate"),
                    pan=arguments.get("pan"),
                    pivot=arguments.get("pivot"),
                    perspective=arguments.get("perspective", False),
                    floor=arguments.get("floor", False),
                    config=self.config,
                )
                caption = (
                    f"# PCB Render ({result['side']})\n\n"
                    f"**Saved to:** {result['path']}\n"
                    f"**Size:** {result['width']}x{result['height']} px, "
                    f"{result['size_bytes']} bytes\n"
                    f"**Duration:** {result['duration_s']}s\n"
                )
                return [
                    types.ImageContent(
                        type="image",
                        data=result["image_base64"],
                        mimeType="image/png",
                    ),
                    types.TextContent(type="text", text=caption),
                ]

            if name == "pcb_export_layers":
                layers = arguments.get("layers")
                if not layers:
                    return [types.TextContent(type="text", text="Error: layers parameter is required")]
                paths = kicad_cli.export_layers_svg(
                    source,
                    layers,
                    output_dir=arguments.get("output_dir"),
                    fit=arguments.get("fit", "board"),
                    black_and_white=arguments.get("black_and_white", False),
                    config=self.config,
                )
                lines = "\n".join(f"- {p}" for p in paths)
                text = (
                    f"# Exported {len(paths)} layer SVG(s)\n\n"
                    f"**Fit:** {arguments.get('fit', 'board')}\n\n{lines}\n"
                )
                return [types.TextContent(type="text", text=text)]

            return [types.TextContent(type="text", text=f"Unknown PCB tool: {name}")]

        except (KiCadCLIError, ValueError) as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error executing {name}: {e}")]

    @staticmethod
    def _format_drc(report: dict) -> str:
        """Render a parsed DRC report dict as a Markdown summary."""
        lines: list[str] = ["# DRC Report"]
        src = report.get("source") or ""
        meta = []
        if src:
            meta.append(f"**Board:** {src}")
        if report.get("kicad_version"):
            meta.append(f"**KiCad:** {report['kicad_version']}")
        if report.get("duration_s") is not None:
            meta.append(f"**Duration:** {report['duration_s']}s")
        if meta:
            lines.append("\n".join(meta))

        total = report.get("total", 0)
        by_sev = report.get("by_severity", {})
        sev_str = ", ".join(f"{v} {k}" for k, v in sorted(by_sev.items())) or "none"
        lines.append(f"**Total problems:** {total} ({sev_str})")
        if report.get("severity_filter") and report["severity_filter"] != "all":
            lines.append(f"*(filtered to severity: {report['severity_filter']})*")
        if report.get("report_path"):
            lines.append(f"**Report:** {report['report_path']}")

        if total == 0:
            lines.append("\n✅ No DRC violations found.")
            return "\n\n".join(lines)

        if report.get("truncated"):
            lines.append("*(violation listing truncated by max_violations; totals above are exact)*")

        for group in report.get("groups", []):
            sevs = ", ".join(f"{v} {k}" for k, v in sorted(group["severities"].items()))
            header = f"## {group['type']} — {group['count']} ({sevs})"
            block = [header]
            for prob in group["problems"]:
                block.append(f"- **[{prob['severity']}]** {prob['description']}")
                for item in prob["items"]:
                    x, y = item.get("x"), item.get("y")
                    loc = f" @ ({x}, {y})" if x is not None and y is not None else ""
                    block.append(f"    - {item['description']}{loc}")
            lines.append("\n".join(block))

        return "\n\n".join(lines)

    async def run(self):
        """Run the MCP server."""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            init_options = InitializationOptions(
                server_name="kicad-mcp",
                server_version=__version__,
                capabilities=self.server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
            )

            await self.server.run(
                read_stream,
                write_stream,
                init_options
            )


async def _async_main():
    """Async entry point."""
    server = KiCadMCPServer()
    await server.run()


def main():
    """Console-script entry point (synchronous)."""
    import asyncio
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
