"""Circuit graph representation using NetworkX and kinparse.

This module provides a bipartite graph representation of KiCad netlists,
with components and nets as nodes, connected by edges representing pins.
"""

import networkx as nx
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass, field
from kinparse import parse_netlist
import subprocess
import tempfile
import os


@dataclass
class Component:
    """Component data extracted from netlist."""
    reference: str
    value: str
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    pins: Dict[str, str] = field(default_factory=dict)  # pin_number -> pin_name


@dataclass
class Net:
    """Net data extracted from netlist."""
    name: str
    code: str
    connections: List[Tuple[str, str, str]] = field(default_factory=list)  # (ref, pin_num, pin_name)


@dataclass
class Netlist:
    """Clean netlist representation."""
    components: Dict[str, Component] = field(default_factory=dict)
    nets: Dict[str, Net] = field(default_factory=dict)


class CircuitGraph:
    """Bipartite graph representation of a circuit from KiCad netlist."""

    def __init__(self):
        """Initialize an empty circuit graph."""
        self.graph = nx.MultiGraph()  # Changed to MultiGraph to handle multiple pins per net
        self.netlist: Optional[Netlist] = None
        self._component_nodes: Set[Tuple[str, str]] = set()
        self._net_nodes: Set[Tuple[str, str]] = set()
        self._filepath: Optional[Path] = None
        self._netlist_path: Optional[Path] = None

    @classmethod
    def from_kicad_schematic(cls, filepath: Path, kicad_cli_path: str = None) -> "CircuitGraph":
        """Build a circuit graph from a KiCad schematic file.

        Args:
            filepath: Path to .kicad_sch file
            kicad_cli_path: Optional path to kicad-cli executable

        Returns:
            CircuitGraph instance
        """
        circuit = cls()
        circuit.load_schematic(filepath, kicad_cli_path)
        return circuit

    @classmethod
    def from_netlist(cls, netlist_path: Path) -> "CircuitGraph":
        """Build a circuit graph from a KiCad netlist file.

        Args:
            netlist_path: Path to .net file

        Returns:
            CircuitGraph instance
        """
        circuit = cls()
        circuit.load_netlist(netlist_path)
        return circuit

    def load_schematic(self, filepath: Path, kicad_cli_path: str = None) -> None:
        """Load and parse a KiCad schematic file by exporting its netlist.

        Args:
            filepath: Path to .kicad_sch file
            kicad_cli_path: Optional path to kicad-cli executable
        """
        self._filepath = filepath

        # Find kicad-cli
        if not kicad_cli_path:
            # Try common locations
            possible_paths = [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS
                "/usr/bin/kicad-cli",  # Linux
                "kicad-cli"  # In PATH
            ]
            for path in possible_paths:
                try:
                    result = subprocess.run([path, "--version"], capture_output=True, timeout=2)
                    if result.returncode == 0:
                        kicad_cli_path = path
                        break
                except:
                    continue

            if not kicad_cli_path:
                raise RuntimeError("Could not find kicad-cli. Please install KiCad or provide the path.")

        # Export netlist to temporary file
        with tempfile.NamedTemporaryFile(suffix='.net', delete=False) as tmp:
            self._netlist_path = Path(tmp.name)

        try:
            # Export netlist using kicad-cli
            result = subprocess.run(
                [kicad_cli_path, "sch", "export", "netlist", "--format", "kicadsexpr",
                 "-o", str(self._netlist_path), str(filepath)],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to export netlist: {result.stderr}")

            # Load the exported netlist
            self.load_netlist(self._netlist_path)

        finally:
            # Clean up temporary file
            if self._netlist_path and self._netlist_path.exists():
                try:
                    os.unlink(self._netlist_path)
                except:
                    pass

    def load_netlist(self, netlist_path: Path) -> None:
        """Load and parse a KiCad netlist file.

        Args:
            netlist_path: Path to .net file
        """
        self._netlist_path = netlist_path

        # Parse netlist with kinparse
        parsed = parse_netlist(str(netlist_path))

        # Extract netlist data
        self.netlist = self._extract_netlist(parsed)

        # Build the bipartite graph
        self._build_graph()

    def _extract_netlist(self, parsed) -> Netlist:
        """Extract clean netlist data from kinparse result.

        Args:
            parsed: Parsed netlist from kinparse

        Returns:
            Netlist object
        """
        netlist = Netlist()

        # Extract components from parts
        if hasattr(parsed, 'parts'):
            for part in parsed.parts:
                # Convert ParseResults to list if needed
                part_list = part.asList() if hasattr(part, 'asList') else list(part)

                if len(part_list) >= 3:
                    ref = str(part_list[0])
                    value = str(part_list[1])
                    footprint = str(part_list[2]) if len(part_list) > 2 else ""
                    datasheet = str(part_list[3]) if len(part_list) > 3 else ""

                    # Skip power symbols
                    if ref.startswith('#PWR'):
                        continue

                    comp = Component(
                        reference=ref,
                        value=value,
                        footprint=footprint,
                        datasheet=datasheet
                    )

                    # Extract additional fields
                    for i in range(4, len(part_list)):
                        item = part_list[i]
                        if isinstance(item, list) and len(item) >= 2:
                            field_name = str(item[0])
                            field_value = str(item[1]) if len(item) > 1 else ""
                            comp.fields[field_name] = field_value

                    netlist.components[ref] = comp

        # Extract nets
        if hasattr(parsed, 'nets'):
            for net in parsed.nets:
                # Convert ParseResults to list if needed
                net_list = net.asList() if hasattr(net, 'asList') else list(net)

                if len(net_list) >= 4:
                    code = str(net_list[0])
                    name = str(net_list[1]) if len(net_list) > 1 else f"Net_{code}"

                    net_obj = Net(name=name, code=code)

                    # Extract connections
                    # net_list[3] is a list of connections: [ref, pin_num, pin_name, pin_type]
                    connections = net_list[3]
                    if connections:
                        # Convert to list if needed
                        conn_list = connections.asList() if hasattr(connections, 'asList') else list(connections)

                        for conn in conn_list:
                            # Convert connection to list if needed
                            conn_data = conn.asList() if hasattr(conn, 'asList') else list(conn)

                            if len(conn_data) >= 3:
                                ref = str(conn_data[0])
                                pin_num = str(conn_data[1])
                                pin_name = str(conn_data[2]) if len(conn_data) > 2 else pin_num

                                net_obj.connections.append((ref, pin_num, pin_name))

                                # Also store pin info in component
                                if ref in netlist.components:
                                    netlist.components[ref].pins[pin_num] = pin_name

                    # Only add nets with connections
                    if net_obj.connections:
                        netlist.nets[name] = net_obj

        return netlist

    def _build_graph(self) -> None:
        """Build the bipartite graph from the netlist."""
        if not self.netlist:
            return

        self.graph.clear()
        self._component_nodes.clear()
        self._net_nodes.clear()

        # Add component nodes
        for ref, comp in self.netlist.components.items():
            node_id = ("comp", ref)
            node_attrs = {
                'kind': 'component',
                'reference': ref,
                'value': comp.value,
                'footprint': comp.footprint,
                'category': self._get_component_category(ref),
                'pins': comp.pins,
                'fields': comp.fields
            }
            self.graph.add_node(node_id, **node_attrs)
            self._component_nodes.add(node_id)

        # Add net nodes and edges
        for net_name, net in self.netlist.nets.items():
            net_node = ("net", net_name)
            self.graph.add_node(
                net_node,
                kind="net",
                name=net_name,
                code=net.code,
                is_power=self._is_power_net(net_name),
                num_connections=len(net.connections)
            )
            self._net_nodes.add(net_node)

            # Add edges from components to net
            for comp_ref, pin_num, pin_name in net.connections:
                if comp_ref.startswith('#PWR'):
                    continue  # Skip power symbols

                comp_node = ("comp", comp_ref)
                if comp_node in self.graph:
                    self.graph.add_edge(
                        comp_node,
                        net_node,
                        pin_number=pin_num,
                        pin_name=pin_name
                    )

    def _is_power_net(self, net_name: str) -> bool:
        """Check if a net name indicates a power net."""
        power_patterns = [
            'VCC', 'VDD', 'VSS', 'GND', 'DGND', 'AGND', 'PGND',
            '3.3V', '+3V', '5V', '+5V', '12V', '24V', '48V',
            '-12V', '-5V', '-3V', '-2.5V', '+2.5V', '+1.8V',
            'VBAT', 'VBUS', 'VIN', 'VOUT', 'AVDD', 'AVSS', 'DVDD'
        ]
        name_upper = net_name.upper()
        return any(pattern in name_upper for pattern in power_patterns)

    def _get_component_category(self, reference: str) -> str:
        """Determine component category from reference designator."""
        if reference.startswith('#'):
            return 'Power'

        # Extract alphabetic prefix
        prefix = ''.join(c for c in reference if c.isalpha())

        categories = {
            'R': 'Resistors',
            'C': 'Capacitors',
            'L': 'Inductors',
            'D': 'Diodes',
            'Q': 'Transistors',
            'U': 'ICs',
            'IC': 'ICs',
            'J': 'Connectors',
            'P': 'Connectors',
            'CN': 'Connectors',
            'Y': 'Crystals',
            'X': 'Crystals',
            'SW': 'Switches',
            'TP': 'Test Points',
            'LED': 'LEDs',
            'FB': 'Ferrite Beads',
            'F': 'Fuses',
            'T': 'Transformers',
            'BT': 'Batteries',
        }

        # Check IC prefix first
        if prefix.startswith('IC'):
            return 'ICs'

        return categories.get(prefix, 'Other')

    # Query methods
    def get_component(self, reference: str) -> Optional[Dict[str, Any]]:
        """Get component data by reference designator."""
        node_id = ("comp", reference)
        if node_id in self.graph:
            return dict(self.graph.nodes[node_id])
        return None

    def get_nets_of_component(self, reference: str) -> List[str]:
        """Get all nets connected to a component."""
        node_id = ("comp", reference)
        nets = set()  # Use set to deduplicate (MultiGraph might have multiple edges to same net)

        if node_id in self.graph:
            for neighbor in self.graph.neighbors(node_id):
                if neighbor[0] == "net":
                    nets.add(neighbor[1])

        return sorted(list(nets))

    def get_components_on_net(self, net_name: str) -> List[Tuple[str, str, str]]:
        """Get all components and pins connected to a net.

        Returns:
            List of (component_ref, pin_number, pin_name) tuples
        """
        net_node = ("net", net_name)
        components = []

        if net_node in self.graph:
            for neighbor in self.graph.neighbors(net_node):
                if neighbor[0] == "comp":
                    # MultiGraph can have multiple edges, get all of them
                    edges = self.graph[neighbor][net_node]

                    # Handle both fresh and unpickled MultiGraph edge structures
                    if hasattr(edges, 'items'):
                        # Normal case: edges is an AtlasView with items()
                        for edge_key, edge_data in edges.items():
                            if isinstance(edge_data, dict):
                                pin_num = edge_data.get('pin_number', '?')
                                pin_name = edge_data.get('pin_name', '')
                                components.append((neighbor[1], pin_num, pin_name))
                    elif isinstance(edges, dict):
                        # Might be a single edge stored as dict directly
                        pin_num = edges.get('pin_number', '?')
                        pin_name = edges.get('pin_name', '')
                        components.append((neighbor[1], pin_num, pin_name))

        return sorted(components)

    def get_pin_net(self, reference: str, pin_number: str) -> Optional[str]:
        """Get the net connected to a specific component pin."""
        comp_node = ("comp", reference)

        if comp_node in self.graph:
            for neighbor in self.graph.neighbors(comp_node):
                # MultiGraph can have multiple edges, check all of them
                edges = self.graph[comp_node][neighbor]

                # Handle both fresh and unpickled MultiGraph edge structures
                if hasattr(edges, 'items'):
                    # Normal case: edges is an AtlasView with items()
                    for edge_key, edge_data in edges.items():
                        if isinstance(edge_data, dict) and edge_data.get('pin_number') == pin_number:
                            return neighbor[1]
                elif isinstance(edges, dict):
                    # Might be a single edge stored as dict directly
                    if edges.get('pin_number') == pin_number:
                        return neighbor[1]

        return None

    def trace_path(self, start_ref: str, end_ref: str, max_depth: int = 10) -> Optional[List[str]]:
        """Trace the connectivity path between two components."""
        start_node = ("comp", start_ref)
        end_node = ("comp", end_ref)

        if start_node not in self.graph or end_node not in self.graph:
            return None

        try:
            path = nx.shortest_path(self.graph, start_node, end_node)
            # Convert to readable format
            return [node[1] for node in path]
        except nx.NetworkXNoPath:
            return None

    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about the circuit."""
        if not self.netlist:
            return {}

        stats = {
            'total_components': len(self.netlist.components),
            'total_nets': len(self.netlist.nets),
            'total_connections': sum(len(net.connections) for net in self.netlist.nets.values()),
            'component_categories': {},
            'power_nets': [],
            'floating_components': [],
            'largest_nets': []
        }

        # Count components by category
        for ref in self.netlist.components:
            category = self._get_component_category(ref)
            stats['component_categories'][category] = stats['component_categories'].get(category, 0) + 1

        # Find power nets and largest nets
        for net_name, net in self.netlist.nets.items():
            if self._is_power_net(net_name):
                stats['power_nets'].append(net_name)

            # Track largest nets
            stats['largest_nets'].append((net_name, len(net.connections)))

        # Sort largest nets by connection count
        stats['largest_nets'].sort(key=lambda x: x[1], reverse=True)
        stats['largest_nets'] = stats['largest_nets'][:10]  # Keep top 10

        # Find floating components
        connected_components = set()
        for net in self.netlist.nets.values():
            for ref, _, _ in net.connections:
                if not ref.startswith('#'):
                    connected_components.add(ref)

        for ref in self.netlist.components:
            if ref not in connected_components:
                stats['floating_components'].append(ref)

        return stats

    def get_overview_text(self) -> str:
        """Generate a human-readable overview of the circuit."""
        stats = self.get_statistics()

        lines = [
            f"# Circuit Overview",
            f"",
            f"**Components:** {stats['total_components']}",
            f"**Nets:** {stats['total_nets']}",
            f"**Connections:** {stats['total_connections']}",
            f"",
            f"## Component Categories:"
        ]

        for category, count in sorted(stats['component_categories'].items()):
            lines.append(f"- {category}: {count}")

        # Show largest nets (most connected)
        if stats['largest_nets']:
            lines.extend([
                f"",
                f"## Largest Nets (by connections):"
            ])
            for net_name, count in stats['largest_nets'][:5]:
                components = self.get_components_on_net(net_name)[:3]
                comp_str = ', '.join([f"{ref}:{pin}" for ref, pin, _ in components])
                if len(components) < len(self.get_components_on_net(net_name)):
                    comp_str += f", ... ({count} total)"
                lines.append(f"- **{net_name}**: {comp_str}")

        # Show power nets
        if stats['power_nets']:
            lines.extend([
                f"",
                f"## Power Nets:"
            ])
            for net_name in sorted(stats['power_nets'])[:10]:
                net_data = self.netlist.nets.get(net_name)
                if net_data:
                    count = len(net_data.connections)
                    lines.append(f"- **{net_name}**: {count} connections")

        # Show main ICs
        ics = []
        for ref, comp in self.netlist.components.items():
            if self._get_component_category(ref) == 'ICs':
                ics.append((ref, comp.value))

        if ics:
            lines.extend([
                f"",
                f"## Main ICs:"
            ])
            for ref, value in sorted(ics)[:10]:
                nets = self.get_nets_of_component(ref)[:5]
                lines.append(f"- **{ref}**: {value}")
                if nets:
                    lines.append(f"  Nets: {', '.join(nets)}")

        # Show connectors
        connectors = []
        for ref, comp in self.netlist.components.items():
            if self._get_component_category(ref) == 'Connectors':
                connectors.append((ref, comp.value))

        if connectors:
            lines.extend([
                f"",
                f"## Connectors:"
            ])
            for ref, value in sorted(connectors)[:10]:
                pin_count = len(self.netlist.components[ref].pins)
                lines.append(f"- **{ref}**: {value} ({pin_count} pins)")

        if stats['floating_components']:
            lines.extend([
                f"",
                f"## ⚠️ Warning: Floating Components:",
            ])
            for ref in stats['floating_components'][:10]:
                comp = self.netlist.components[ref]
                lines.append(f"- {ref} ({comp.value})")

        return '\n'.join(lines)

    def find_connected_group(self, start_ref: str, max_hops: int = 3) -> Set[str]:
        """Find all components within N hops of a starting component."""
        start_node = ("comp", start_ref)
        if start_node not in self.graph:
            return set()

        connected = set()
        visited = set()
        queue = [(start_node, 0)]

        while queue:
            node, depth = queue.pop(0)
            if node in visited or depth > max_hops:
                continue

            visited.add(node)
            if node[0] == "comp":
                connected.add(node[1])

            for neighbor in self.graph.neighbors(node):
                if neighbor not in visited:
                    queue.append((neighbor, depth + 1))

        return connected

    def get_net_details(self, net_name: str) -> Dict[str, Any]:
        """Get detailed information about a net."""
        if net_name not in self.netlist.nets:
            return None

        net = self.netlist.nets[net_name]
        components = self.get_components_on_net(net_name)

        return {
            'name': net_name,
            'code': net.code,
            'is_power': self._is_power_net(net_name),
            'num_connections': len(net.connections),
            'components': components,
            'component_types': list(set([
                self._get_component_category(ref)
                for ref, _, _ in components
            ]))
        }