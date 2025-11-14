"""KiCad MCP Server with circuit graph functionality."""

import sys
from pathlib import Path
from typing import Dict, Any, Optional
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .circuit_graph_netlist import CircuitGraph
from .multi_board_graph import MultiBoardGraph
from .config import KiCadMCPConfig


class KiCadMCPServer:
    """MCP server for KiCad schematic analysis."""

    def __init__(self):
        """Initialize the MCP server."""
        self.server = Server("kicad-mcp")
        self.config = KiCadMCPConfig()  # Load configuration
        self.circuits: Dict[str, CircuitGraph] = {}  # Cache loaded circuits
        self.systems: Dict[str, MultiBoardGraph] = {}  # Cache loaded systems
        self.setup_handlers()

    def setup_handlers(self) -> None:
        """Setup tool handlers."""

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """List available tools."""
            return [
                types.Tool(
                    name="get_overview",
                    description="Get a high-level overview of a KiCad schematic",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            }
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="list_components",
                    description="List all components in the schematic",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "category": {
                                "type": "string",
                                "description": "Optional: Filter by category (e.g., 'ICs', 'Resistors')"
                            }
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="list_nets",
                    description="List all nets in the schematic",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "power_only": {
                                "type": "boolean",
                                "description": "Only show power nets",
                                "default": False
                            }
                        },
                        "required": ["source"]
                    }
                ),
                types.Tool(
                    name="examine_component",
                    description="Get detailed information about a specific component",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference (e.g., 'IC2', 'R1')"
                            }
                        },
                        "required": ["source", "reference"]
                    }
                ),
                types.Tool(
                    name="examine_net",
                    description="Get detailed information about a specific net",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "net_name": {
                                "type": "string",
                                "description": "Net name (e.g., 'GND', 'VCC')"
                            }
                        },
                        "required": ["source", "net_name"]
                    }
                ),
                types.Tool(
                    name="trace_connection",
                    description="Find the connection path between two components",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "start_ref": {
                                "type": "string",
                                "description": "Starting component reference"
                            },
                            "end_ref": {
                                "type": "string",
                                "description": "Ending component reference"
                            }
                        },
                        "required": ["source", "start_ref", "end_ref"]
                    }
                ),
                types.Tool(
                    name="find_connected_components",
                    description="Find all components connected to a given component within N hops",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference"
                            },
                            "max_hops": {
                                "type": "integer",
                                "description": "Maximum number of hops",
                                "default": 2
                            }
                        },
                        "required": ["source", "reference"]
                    }
                ),
                types.Tool(
                    name="check_pin_connection",
                    description="Check what net a specific component pin is connected to",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Board name from config (e.g., 'main', 'sense') OR path to .kicad_sch file"
                            },
                            "reference": {
                                "type": "string",
                                "description": "Component reference"
                            },
                            "pin_number": {
                                "type": "string",
                                "description": "Pin number"
                            }
                        },
                        "required": ["source", "reference", "pin_number"]
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

        @self.server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict | None
        ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Handle tool calls."""

            # Some tools don't require arguments
            if not arguments:
                arguments = {}

            # Get source parameter (board name or file path)
            source = arguments.get("source")
            if not source and name not in ["list_configured_boards", "list_configured_systems",
                                           "load_board", "load_system", "trace_cross_board_signal",
                                           "get_system_overview", "reload_config", "add_board",
                                           "remove_board", "add_system", "remove_system"]:
                return [types.TextContent(
                    type="text",
                    text="Error: source parameter is required"
                )]

            # Load or get cached circuit for tools that need it
            circuit = None
            if source:
                circuit = self._load_circuit(source)
            if source and not circuit:
                return [types.TextContent(
                    type="text",
                    text=f"Error: Could not load schematic from {source}"
                )]

            try:
                if name == "get_overview":
                    result = circuit.get_overview_text()

                elif name == "list_components":
                    category = arguments.get("category")
                    components = []

                    for ref, comp in circuit.netlist.components.items():
                        comp_cat = circuit._get_component_category(ref)
                        if not category or comp_cat == category:
                            components.append(f"- **{ref}**: {comp.value} ({comp_cat})")

                    result = f"# Components {'(' + category + ')' if category else ''}\n\n"
                    result += '\n'.join(components) if components else "No components found"

                elif name == "list_nets":
                    power_only = arguments.get("power_only", False)
                    nets = []

                    for net_name, net in circuit.netlist.nets.items():
                        if not power_only or circuit._is_power_net(net_name):
                            conn_count = len(net.connections)
                            nets.append(f"- **{net_name}**: {conn_count} connections")

                    result = f"# Nets {'(Power only)' if power_only else ''}\n\n"
                    result += '\n'.join(sorted(nets)) if nets else "No nets found"

                elif name == "examine_component":
                    reference = arguments.get("reference")
                    comp_data = circuit.get_component(reference)

                    if comp_data:
                        result = f"# Component: {reference}\n\n"
                        result += f"**Value:** {comp_data.get('value')}\n"
                        result += f"**Category:** {comp_data.get('category')}\n"
                        result += f"**Footprint:** {comp_data.get('footprint', 'N/A')}\n\n"

                        # Show connected nets
                        nets = circuit.get_nets_of_component(reference)
                        result += f"## Connected Nets ({len(nets)})\n\n"
                        for net in nets[:20]:  # Limit to 20
                            result += f"- {net}\n"

                        # Show pins
                        pins = comp_data.get('pins', {})
                        if pins:
                            result += f"\n## Pins ({len(pins)})\n\n"
                            for pin_num, pin_name in list(pins.items())[:20]:
                                net = circuit.get_pin_net(reference, pin_num)
                                result += f"- Pin {pin_num} ({pin_name}): {net or 'NC'}\n"
                    else:
                        result = f"Component {reference} not found"

                elif name == "examine_net":
                    net_name = arguments.get("net_name")
                    net_details = circuit.get_net_details(net_name)

                    if net_details:
                        result = f"# Net: {net_name}\n\n"
                        result += f"**Power net:** {'Yes' if net_details['is_power'] else 'No'}\n"
                        result += f"**Connections:** {net_details['num_connections']}\n"
                        result += f"**Component types:** {', '.join(net_details['component_types'])}\n\n"

                        result += "## Connected Components\n\n"
                        for ref, pin, name in net_details['components'][:30]:
                            result += f"- {ref}:{pin} ({name})\n"
                    else:
                        result = f"Net {net_name} not found"

                elif name == "trace_connection":
                    start_ref = arguments.get("start_ref")
                    end_ref = arguments.get("end_ref")

                    path = circuit.trace_path(start_ref, end_ref)
                    if path:
                        result = f"# Connection Path: {start_ref} → {end_ref}\n\n"
                        result += " → ".join(path)
                    else:
                        result = f"No connection path found between {start_ref} and {end_ref}"

                elif name == "find_connected_components":
                    reference = arguments.get("reference")
                    max_hops = arguments.get("max_hops", 2)

                    connected = circuit.find_connected_group(reference, max_hops)
                    connected.discard(reference)  # Remove the starting component

                    result = f"# Components within {max_hops} hops of {reference}\n\n"
                    result += f"Found {len(connected)} components:\n\n"

                    for comp_ref in sorted(list(connected)[:30]):
                        comp = circuit.netlist.components.get(comp_ref)
                        if comp:
                            result += f"- {comp_ref}: {comp.value}\n"

                elif name == "check_pin_connection":
                    reference = arguments.get("reference")
                    pin_number = arguments.get("pin_number")

                    net = circuit.get_pin_net(reference, pin_number)
                    comp = circuit.netlist.components.get(reference)

                    result = f"# Pin Connection: {reference}:{pin_number}\n\n"
                    if comp:
                        pin_name = comp.pins.get(pin_number, "Unknown")
                        result += f"**Pin name:** {pin_name}\n"

                    if net:
                        result += f"**Connected to net:** {net}\n\n"

                        # Show what else is on this net
                        net_details = circuit.get_net_details(net)
                        if net_details:
                            result += f"**Other components on this net:**\n"
                            for ref, pin, name in net_details['components'][:10]:
                                if ref != reference:
                                    result += f"- {ref}:{pin} ({name})\n"
                    else:
                        result += "**Not connected**"

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
                        result = circuit.get_overview_text()
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
                            for node in path:
                                if ':' in str(node):
                                    board, ref = node.split(':', 1)
                                    comp = system.boards[board].netlist.components.get(ref)
                                    value = comp.value if comp else 'unknown'
                                    result += f"- **{node}** ({value})\n"
                                else:
                                    boards = system.get_connected_boards(node)
                                    if len(boards) > 1:
                                        result += f"- → {node} [crosses: {' ↔ '.join(boards)}]\n"
                                    else:
                                        result += f"- → {node}\n"
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
                    self.config.add_board(board_name, board_path, board_desc)

                    result = f"# Board Added\n\n"
                    result += f"**Name:** {board_name}\n"
                    result += f"**Path:** {board_path}\n"
                    result += f"**Description:** {board_desc}\n\n"
                    result += f"Saved to: {self.config.config_path}\n"

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

    def _load_circuit(self, source: str) -> Optional[CircuitGraph]:
        """Load a circuit from cache or file.

        Args:
            source: Either a board name from config or a full path to a schematic
        """
        # First check if it's a board name from config
        if not ('/' in source or '\\' in source):
            # Looks like a board name, try to load from config
            circuit = self.config.load_board(source)
            if circuit:
                self.circuits[source] = circuit
                return circuit

        # Otherwise treat as a file path
        path = Path(source)

        # Check cache
        if source in self.circuits:
            # Check if file has been modified
            cached = self.circuits[source]
            if cached._filepath and cached._filepath.exists():
                # For now, just return cached version
                # TODO: Check modification time
                return cached

        # Load new circuit
        try:
            if path.suffix == '.net':
                circuit = CircuitGraph.from_netlist(path)
            else:
                circuit = CircuitGraph.from_kicad_schematic(path)

            # Cache it
            self.circuits[source] = circuit
            return circuit

        except Exception as e:
            print(f"Error loading circuit: {e}", file=sys.stderr)
            return None

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


async def main():
    """Main entry point."""
    server = KiCadMCPServer()
    await server.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())