"""Multi-board circuit graph for cross-board signal tracing."""

from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
import networkx as nx
from .circuit_graph_netlist import CircuitGraph


class MultiBoardGraph:
    """Combines multiple circuit boards into a unified graph."""

    def __init__(self):
        """Initialize multi-board graph."""
        self.boards: Dict[str, CircuitGraph] = {}
        self.unified_graph = nx.MultiGraph()  # Changed to MultiGraph to match circuit graphs
        self.board_map: Dict[str, str] = {}  # node -> board_name mapping

    def add_board(self, name: str, schematic_path: Path) -> None:
        """Add a board to the multi-board system.

        Args:
            name: Board identifier (e.g., 'main', 'sense')
            schematic_path: Path to the board's .kicad_sch file
        """
        print(f"Loading board '{name}' from {schematic_path}")
        circuit = CircuitGraph.from_kicad_schematic(schematic_path)
        self.boards[name] = circuit

        # Add this board's components and nets to unified graph
        # Prefix component refs with board name to avoid collisions
        for ref, comp in circuit.netlist.components.items():
            node_id = f"{name}:{ref}"
            self.unified_graph.add_node(
                node_id,
                type='component',
                board=name,
                reference=ref,
                value=comp.value,
                category=circuit._get_component_category(ref)
            )
            self.board_map[node_id] = name

        # Add nets - these are NOT prefixed as they connect across boards!
        for net_name in circuit.netlist.nets:
            if net_name not in self.unified_graph:
                self.unified_graph.add_node(
                    net_name,
                    type='net',
                    boards=set()
                )
            # Track which boards this net appears on
            self.unified_graph.nodes[net_name]['boards'].add(name)

        # Add edges
        for net_name, net in circuit.netlist.nets.items():
            for conn in net.connections:
                # conn is a tuple: (reference, pin, name)
                if len(conn) == 3:
                    comp_ref, pin, _ = conn
                elif len(conn) == 2:
                    comp_ref, pin = conn
                else:
                    print(f"Unexpected conn structure: {conn}")
                    continue
                comp_node = f"{name}:{comp_ref}"
                if comp_node in self.unified_graph:
                    self.unified_graph.add_edge(
                        comp_node,
                        net_name,
                        pin=pin
                    )

    def get_connected_boards(self, net_name: str) -> List[str]:
        """Get all boards that share a particular net.

        Args:
            net_name: The net to check

        Returns:
            List of board names that contain this net
        """
        if net_name in self.unified_graph:
            return list(self.unified_graph.nodes[net_name].get('boards', set()))
        return []

    def trace_cross_board(self, start_comp: str, end_comp: str,
                         start_board: str = None, end_board: str = None) -> Optional[List[str]]:
        """Trace a path between components, potentially across boards.

        Args:
            start_comp: Starting component reference
            end_comp: Ending component reference
            start_board: Board containing start component (optional if unique)
            end_board: Board containing end component (optional if unique)

        Returns:
            Path as list of nodes, or None if no path exists
        """
        # Build full node IDs
        start_nodes = []
        end_nodes = []

        for board_name in self.boards:
            if f"{board_name}:{start_comp}" in self.unified_graph:
                if start_board is None or board_name == start_board:
                    start_nodes.append(f"{board_name}:{start_comp}")

            if f"{board_name}:{end_comp}" in self.unified_graph:
                if end_board is None or board_name == end_board:
                    end_nodes.append(f"{board_name}:{end_comp}")

        if not start_nodes or not end_nodes:
            return None

        # Try to find a path between any valid start/end combination
        for start_node in start_nodes:
            for end_node in end_nodes:
                try:
                    path = nx.shortest_path(self.unified_graph, start_node, end_node)
                    return path
                except nx.NetworkXNoPath:
                    continue

        return None

    def find_signal_source(self, net_name: str) -> List[Tuple[str, str]]:
        """Find all components driving a signal/net across all boards.

        Args:
            net_name: The net to analyze

        Returns:
            List of (board, component) tuples that could be sources
        """
        sources = []

        if net_name not in self.unified_graph:
            return sources

        # Look for typical source components
        source_categories = {'ICs', 'Regulators', 'Other', 'Switches'}

        for neighbor in self.unified_graph.neighbors(net_name):
            if self.unified_graph.nodes[neighbor].get('type') == 'component':
                category = self.unified_graph.nodes[neighbor].get('category', '')
                if category in source_categories:
                    board = self.unified_graph.nodes[neighbor].get('board', 'unknown')
                    ref = self.unified_graph.nodes[neighbor].get('reference', neighbor)
                    sources.append((board, ref))

        return sources

    def get_cross_board_connections(self) -> Dict[str, List[str]]:
        """Find all nets that connect multiple boards.

        Returns:
            Dict mapping net names to list of connected boards
        """
        cross_board = {}

        for node, data in self.unified_graph.nodes(data=True):
            if data.get('type') == 'net':
                boards = data.get('boards', set())
                if len(boards) > 1:
                    cross_board[node] = sorted(list(boards))

        return cross_board

    def trace_signal_path(self, signal_net: str,
                          start_comp: str = None, end_comp: str = None,
                          start_board: str = None, end_board: str = None) -> Optional[List[str]]:
        """Trace how a specific signal travels between components.

        Args:
            signal_net: The signal net to follow (e.g., '/MISO')
            start_comp: Starting component (optional)
            end_comp: Ending component (optional)
            start_board: Board containing start component
            end_board: Board containing end component

        Returns:
            Path showing how the signal travels
        """
        if signal_net not in self.unified_graph:
            return None

        # Get all components connected to this signal
        connected = []
        for neighbor in self.unified_graph.neighbors(signal_net):
            if self.unified_graph.nodes[neighbor].get('type') == 'component':
                connected.append(neighbor)

        # If start/end specified, filter
        if start_comp:
            start_nodes = [n for n in connected if n.endswith(f":{start_comp}")]
            if start_board:
                start_nodes = [n for n in start_nodes if n.startswith(f"{start_board}:")]
        else:
            start_nodes = connected

        if end_comp:
            end_nodes = [n for n in connected if n.endswith(f":{end_comp}")]
            if end_board:
                end_nodes = [n for n in end_nodes if n.startswith(f"{end_board}:")]
        else:
            end_nodes = connected

        # Build path through the signal net
        if start_nodes and end_nodes:
            start = start_nodes[0] if start_nodes else connected[0]
            end = end_nodes[0] if end_nodes else connected[-1]

            # Simple path: start -> signal -> end
            path = [start, signal_net, end]

            # Add any intermediate components on the signal
            for comp in connected:
                if comp not in path and comp != start and comp != end:
                    # Insert between signal and end
                    path.insert(-1, comp)

            return path

        return None

    def get_overview(self) -> str:
        """Get a text overview of the multi-board system."""
        lines = ["# Multi-Board System Overview\n"]

        # Board summary
        lines.append("## Boards")
        for name, circuit in self.boards.items():
            stats = circuit.get_statistics()
            lines.append(f"- **{name}**: {stats['total_components']} components, "
                        f"{stats['total_nets']} nets")

        # Cross-board connections
        cross_board = self.get_cross_board_connections()
        lines.append(f"\n## Cross-Board Connections ({len(cross_board)} shared nets)")

        # Group by connection type
        power_nets = []
        signal_nets = []

        for net, boards in sorted(cross_board.items())[:20]:  # Limit to 20
            boards_str = ' ↔ '.join(boards)
            if any(kw in net.upper() for kw in ['VDD', 'VCC', 'GND', '3V', '5V', '+', '-']):
                power_nets.append(f"- **{net}**: {boards_str}")
            else:
                signal_nets.append(f"- **{net}**: {boards_str}")

        if power_nets:
            lines.append("\n### Power")
            lines.extend(power_nets)

        if signal_nets:
            lines.append("\n### Signals")
            lines.extend(signal_nets)

        # Unified graph stats
        lines.append(f"\n## Unified Graph Statistics")
        num_components = sum(1 for n, d in self.unified_graph.nodes(data=True)
                           if d.get('type') == 'component')
        num_nets = sum(1 for n, d in self.unified_graph.nodes(data=True)
                      if d.get('type') == 'net')
        lines.append(f"- Total components: {num_components}")
        lines.append(f"- Total unique nets: {num_nets}")
        lines.append(f"- Total connections: {self.unified_graph.number_of_edges()}")

        return '\n'.join(lines)