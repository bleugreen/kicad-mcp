"KiCad MCP Server with circuit graph functionality."

from pathlib import Path
from typing import Dict, Any, Optional
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .circuit_graph import CircuitGraph
from .multi_board_graph import MultiBoardGraph
from .config import KiCadMCPConfig
from .datasheet_lookup import DatasheetFinder


class KiCadMCPServer:
    """MCP server for KiCad schematic analysis."""

    def __init__(self):
        """Initialize the MCP server."""
        self.server = Server("kicad-mcp")
        self.config = KiCadMCPConfig()  # Load configuration
        self.circuits: Dict[str, CircuitGraph] = {}  # Cache loaded circuits
        self.systems: Dict[str, MultiBoardGraph] = {}  # Cache loaded systems
        self.datasheet_finder = DatasheetFinder(self.config.cache_dir)  # Datasheet lookup
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
            ]

    async def handle_call_tool(
        self, name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handle tool calls."""

        # Some tools don't require arguments
        if not arguments:
            arguments = {}

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

    async def run(self):
        """Run the MCP server."""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            init_options = InitializationOptions(
                server_name="kicad-mcp",
                server_version="0.2.0",
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