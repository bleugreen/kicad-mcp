"""Multi-board circuit graph for cross-board signal tracing."""

from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
import re
import networkx as nx
from .circuit_graph_netlist import CircuitGraph


class MultiBoardGraph:
    """Combines multiple circuit boards into a unified graph."""

    def __init__(self):
        """Initialize multi-board graph."""
        self.boards: Dict[str, CircuitGraph] = {}
        self.unified_graph = nx.MultiGraph()  # Changed to MultiGraph to match circuit graphs
        self.board_map: Dict[str, str] = {}  # node -> board_name mapping
        self.ignored_components: Dict[str, Set[str]] = {}  # board -> set of ignored component refs

    def _collect_schematic_hierarchy(self, schematic_path: Path) -> List[Path]:
        """Recursively collect all .kicad_sch files in the hierarchy.

        Args:
            schematic_path: Path to a .kicad_sch file

        Returns:
            List of all schematic files in the hierarchy
        """
        files = [schematic_path]
        visited = {schematic_path}

        with open(schematic_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Find hierarchical sheet references
        # KiCad format: (property "Sheetfile" "filename.kicad_sch"
        # Note: closing paren may be on next line
        sheet_files = re.findall(r'\(property "Sheetfile" "([^"]+)"', content)

        for sheet_file in sheet_files:
            # Sheet file is relative to current schematic's directory
            # Resolve to absolute path to handle ../../ paths correctly
            sheet_path = (schematic_path.parent / sheet_file).resolve()
            if sheet_path.exists() and sheet_path not in visited:
                visited.add(sheet_path)
                files.append(sheet_path)  # Add the sheet itself
                # Recursively collect from sub-sheets
                sub_files = self._collect_schematic_hierarchy(sheet_path)
                for f in sub_files:
                    if f not in visited:
                        files.append(f)
                        visited.add(f)

        return files

    def _find_component_schematics(self, schematic_path: Path) -> Dict[str, str]:
        """Find which schematic file each component is defined in.

        Args:
            schematic_path: Path to top-level .kicad_sch file

        Returns:
            Dict mapping component reference to schematic filename
        """
        # 1. Collect all schematic files in the hierarchy
        all_schematics = self._collect_schematic_hierarchy(schematic_path)
        print(f"  Found {len(all_schematics)} schematic files in hierarchy")
        for sch in all_schematics:
            print(f"    - {sch.name}")

        # 2. For each schematic, find which components it defines
        component_map = {}

        for sch_file in all_schematics:
            with open(sch_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Find all symbols with their references
            # KiCad format: (symbol ... (property "Reference" "J1" ...)
            # Note: closing paren may be on next line
            refs = re.findall(r'\(property "Reference" "([^"]+)"', content)

            # Only store refs with numbers (skip templates like "J", "CN", "IC")
            for ref in refs:
                # Only map if ref has a number (e.g., J1, CN2, IC1)
                if any(c.isdigit() for c in ref):
                    # Only store if not already mapped (first occurrence wins)
                    if ref not in component_map:
                        component_map[ref] = sch_file.name

        print(f"  Mapped {len(component_map)} components to schematics")
        return component_map

    def add_board(self, name: str, schematic_path: Path, ignore_list: List[str] = None) -> None:
        """Add a board to the multi-board system.

        Args:
            name: Board identifier (e.g., 'main', 'sense')
            schematic_path: Path to the board's .kicad_sch file
            ignore_list: List of component references to ignore on this board
        """
        print(f"Loading board '{name}' from {schematic_path}")
        circuit = CircuitGraph.from_kicad_schematic(schematic_path)
        self.boards[name] = circuit

        # Store ignored components for this board
        if ignore_list:
            self.ignored_components[name] = set(ignore_list)
            print(f"  Ignoring components: {', '.join(ignore_list)}")
        else:
            self.ignored_components[name] = set()

        # Find which schematic each component is defined in
        component_schematics = self._find_component_schematics(schematic_path)

        # Add this board's components and nets to unified graph
        # Prefix component refs with board name to avoid collisions
        for ref, comp in circuit.netlist.components.items():
            # Skip ignored components
            if ref in self.ignored_components[name]:
                continue

            node_id = f"{name}:{ref}"
            self.unified_graph.add_node(
                node_id,
                type='component',
                board=name,
                reference=ref,
                value=comp.value,
                category=circuit._get_component_category(ref),
                schematic=component_schematics.get(ref, 'unknown')
            )
            self.board_map[node_id] = name

        # Add nets - prefix with board name for clear signal flow
        net_mapping = {}  # base_net_name -> list of boards that have it
        for net_name in circuit.netlist.nets:
            # Create board-specific net node
            board_net = f"{name}:{net_name}"
            self.unified_graph.add_node(
                board_net,
                type='net',
                board=name,
                base_net=net_name
            )

            # Track which boards have this base net
            if net_name not in net_mapping:
                net_mapping[net_name] = []
            net_mapping[net_name].append(name)

        # Add edges from components to board-specific nets
        for net_name, net in circuit.netlist.nets.items():
            board_net = f"{name}:{net_name}"
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
                        board_net,
                        pin=pin
                    )

        # Connect board-specific nets across boards
        # Only connect if boards share an interface schematic (indicating physical connection)
        for net_name in circuit.netlist.nets:
            board_net = f"{name}:{net_name}"

            # Get components on this net on this board
            my_comps_on_net = []
            for neighbor in self.unified_graph.neighbors(board_net):
                if self.unified_graph.nodes[neighbor].get('type') == 'component':
                    my_comps_on_net.append(neighbor)

            # Check other boards
            for other_board in self.boards.keys():
                if other_board == name:
                    continue

                other_board_net = f"{other_board}:{net_name}"
                if other_board_net not in self.unified_graph:
                    continue

                # Get components on this net on the other board
                other_comps_on_net = []
                for neighbor in self.unified_graph.neighbors(other_board_net):
                    if self.unified_graph.nodes[neighbor].get('type') == 'component':
                        other_comps_on_net.append(neighbor)

                # Check if any components share a schematic (indicating they're mating connectors)
                shared_schematic = False
                for my_comp in my_comps_on_net:
                    my_sch = self.unified_graph.nodes[my_comp].get('schematic', '')
                    for other_comp in other_comps_on_net:
                        other_sch = self.unified_graph.nodes[other_comp].get('schematic', '')
                        if my_sch == other_sch and my_sch != '' and my_sch != 'unknown':
                            shared_schematic = True
                            break
                    if shared_schematic:
                        break

                # Only connect if boards share an interface schematic
                if shared_schematic:
                    self.unified_graph.add_edge(
                        board_net,
                        other_board_net,
                        type='cross_board'
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
            Dict mapping base net names to list of connected boards
        """
        # Group board-specific nets by their base net name
        base_net_to_boards: Dict[str, Set[str]] = {}

        # Find all nets with cross_board edges
        visited_nets = set()
        for u, v, edge_data in self.unified_graph.edges(data=True):
            if edge_data.get('type') == 'cross_board':
                # Both u and v are net nodes from different boards
                for net_node in [u, v]:
                    if net_node in visited_nets:
                        continue
                    visited_nets.add(net_node)

                    node_data = self.unified_graph.nodes[net_node]
                    if node_data.get('type') == 'net':
                        base_net = node_data.get('base_net', net_node)
                        board = node_data.get('board', 'unknown')

                        if base_net not in base_net_to_boards:
                            base_net_to_boards[base_net] = set()
                        base_net_to_boards[base_net].add(board)

        # Convert to desired format - only include nets shared by multiple boards
        cross_board = {}
        for base_net, boards in base_net_to_boards.items():
            if len(boards) > 1:
                cross_board[base_net] = sorted(list(boards))

        return cross_board

    def trace_signal_path(self, signal_net: str,
                          start_comp: str = None, end_comp: str = None,
                          start_board: str = None, end_board: str = None) -> Optional[List[str]]:
        """Trace how a specific signal travels between components in order.

        Args:
            signal_net: The signal net to follow (e.g., '/MISO')
            start_comp: Starting component (optional)
            end_comp: Ending component (optional)
            start_board: Board containing start component
            end_board: Board containing end component

        Returns:
            Ordered path showing how the signal flows through components
        """
        # Find all board-specific versions of this net
        connected = set()
        for node in self.unified_graph.nodes():
            node_data = self.unified_graph.nodes[node]
            if node_data.get('type') == 'net' and node_data.get('base_net') == signal_net:
                # Get components connected to this board-specific net
                for neighbor in self.unified_graph.neighbors(node):
                    if self.unified_graph.nodes[neighbor].get('type') == 'component':
                        connected.add(neighbor)

        connected = list(connected)  # Convert back to list

        if not connected:
            return None

        # Determine starting component
        if start_comp:
            start_nodes = [n for n in connected if n.endswith(f":{start_comp}")]
            if start_board:
                start_nodes = [n for n in start_nodes if n.startswith(f"{start_board}:")]
            start_node = start_nodes[0] if start_nodes else None
        else:
            # Auto-detect start: prefer ICs, then connectors
            start_node = self._find_signal_source(connected)

        if not start_node:
            start_node = connected[0]

        # Build ordered path by traversing the graph
        ordered_path = self._order_signal_path(signal_net, connected, start_node)

        return ordered_path

    def _find_signal_source(self, components: List[str]) -> Optional[str]:
        """Find the likely source/driver of a signal.

        Prefers: ICs > other components > connectors
        """
        ics = []
        others = []
        connectors = []

        for comp in components:
            category = self.unified_graph.nodes[comp].get('category', '')
            if category == 'ICs':
                ics.append(comp)
            elif 'Connector' in category:
                connectors.append(comp)
            else:
                others.append(comp)

        # Prefer ICs, then others, then connectors
        if ics:
            return ics[0]
        if others:
            return others[0]
        if connectors:
            return connectors[0]
        return None

    def _order_signal_path(self, signal_net: str, components: List[str],
                          start: str) -> List[str]:
        """Order components by their position in signal flow.

        With board-prefixed nets, we can simply use graph distance to order components.
        Components closer to the start (in graph hops) come first.

        Args:
            signal_net: The base net being traced (e.g., '/C_TYP')
            components: All components on this net
            start: Starting component

        Returns:
            Ordered list showing signal flow
        """
        # Calculate shortest path length from start to each component
        comp_distances = {}

        for comp in components:
            if comp == start:
                comp_distances[comp] = 0
            else:
                try:
                    distance = nx.shortest_path_length(self.unified_graph, start, comp)
                    comp_distances[comp] = distance
                except nx.NetworkXNoPath:
                    comp_distances[comp] = float('inf')

        # Group by distance, then by board (maintaining board order based on first appearance)
        by_distance: Dict[int, List[str]] = {}
        for comp in components:
            dist = comp_distances[comp]
            if dist not in by_distance:
                by_distance[dist] = []
            by_distance[dist].append(comp)

        # Sort components at each distance level
        path = []
        prev_comps = []

        for dist in sorted(by_distance.keys()):
            comps_at_dist = by_distance[dist]

            # Group by board at this distance level
            by_board: Dict[str, List[str]] = {}
            board_order = []
            for comp in comps_at_dist:
                board = comp.split(':')[0]
                if board not in by_board:
                    by_board[board] = []
                    board_order.append(board)
                by_board[board].append(comp)

            # For each board at this distance, sort by schematic priority
            for board in board_order:
                comps = by_board[board]

                # Get schematics from previous components
                prev_schematics = set()
                for prev_comp in prev_comps:
                    sch = self.unified_graph.nodes[prev_comp].get('schematic', '')
                    if sch and sch != 'unknown':
                        prev_schematics.add(sch)

                # Sort components: shared schematics first, then by schematic name, then by component name
                def sort_key(c):
                    sch = self.unified_graph.nodes[c].get('schematic', 'zzz')
                    shared_with_prev = 0 if sch in prev_schematics else 1
                    return (shared_with_prev, sch, c)

                sorted_group = sorted(comps, key=sort_key)
                path.extend(sorted_group)
                prev_comps.extend(sorted_group)

        # Insert net indicators between board transitions
        final_path = []
        prev_board = None
        for comp in path:
            curr_board = comp.split(':')[0]
            if prev_board and curr_board != prev_board:
                # Board transition
                final_path.append(signal_net)
            final_path.append(comp)
            prev_board = curr_board

        return final_path

    def _boards_share_nets(self, board1: str, board2: str) -> bool:
        """Check if two boards share any nets."""
        board1_nets = set()
        board2_nets = set()

        for node, data in self.unified_graph.nodes(data=True):
            if data.get('type') == 'net' and 'boards' in data:
                if board1 in data['boards']:
                    board1_nets.add(node)
                if board2 in data['boards']:
                    board2_nets.add(node)

        return bool(board1_nets & board2_nets)

    def _order_components_within_board(self, components: List[str], board: str,
                                       prev_board: Optional[str], next_board: Optional[str],
                                       start: str) -> List[str]:
        """Order components within a board based on signal flow.

        Groups components by their source schematic file, then orders schematic
        groups based on signal flow (input schematic -> processing -> output schematic).

        Args:
            components: List of component nodes on this board
            board: Current board name
            prev_board: Previous board in signal chain (or None)
            next_board: Next board in signal chain (or None)
            start: Starting component of entire trace

        Returns:
            Ordered list of components
        """
        # Group components by source schematic
        by_schematic: Dict[str, List[str]] = {}
        for comp in components:
            schematic = self.unified_graph.nodes[comp].get('schematic', 'unknown')
            if schematic not in by_schematic:
                by_schematic[schematic] = []
            by_schematic[schematic].append(comp)

        # Determine schematic order based on connectivity
        # Input schematics connect to prev_board
        # Output schematics connect to next_board
        # Others are in the middle
        input_schematics = []
        output_schematics = []
        other_schematics = []

        print(f"    Ordering board '{board}': prev={prev_board}, next={next_board}")

        for schematic, comps in by_schematic.items():
            # Check if any component in this schematic connects to prev/next board
            connects_to_prev = False
            connects_to_next = False

            for comp in comps:
                comp_boards = self._get_component_connected_boards(comp)
                if prev_board and prev_board in comp_boards:
                    connects_to_prev = True
                if next_board and next_board in comp_boards:
                    connects_to_next = True

            print(f"      Schematic '{schematic}': prev={connects_to_prev}, next={connects_to_next}")

            if connects_to_prev:
                input_schematics.append(schematic)
            elif connects_to_next:
                output_schematics.append(schematic)
            else:
                other_schematics.append(schematic)

        print(f"      → input: {input_schematics}, other: {other_schematics}, output: {output_schematics}")

        # Build final order: input schematics -> other -> output schematics
        ordered = []
        for schematic in (input_schematics + other_schematics + output_schematics):
            # Add all components from this schematic (excluding start if already added)
            for comp in by_schematic[schematic]:
                if comp != start and comp not in ordered:
                    ordered.append(comp)

        return ordered

    def _get_component_connected_boards(self, comp_node: str, exclude_net: Optional[str] = None) -> Set[str]:
        """Get all boards this component connects to via its nets.

        Args:
            comp_node: Component node ID (e.g., 'main:J1')
            exclude_net: Optional net to exclude from connectivity check

        Returns:
            Set of board names this component's nets connect to
        """
        connected_boards = set()

        # Get all nets this component is connected to
        for neighbor in self.unified_graph.neighbors(comp_node):
            if self.unified_graph.nodes[neighbor].get('type') == 'net':
                # Skip the excluded net
                if exclude_net and neighbor == exclude_net:
                    continue

                # Get boards that share this net
                boards = self.unified_graph.nodes[neighbor].get('boards', set())
                connected_boards.update(boards)

        # Remove the component's own board
        own_board = comp_node.split(':')[0]
        connected_boards.discard(own_board)

        return connected_boards

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

        for net, boards in sorted(cross_board.items()):
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