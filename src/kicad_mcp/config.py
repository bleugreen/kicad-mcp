"""Configuration and caching system for KiCad MCP."""

import os
import yaml
import pickle
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

from .circuit_graph import CircuitGraph
from .multi_board_graph import MultiBoardGraph


# Bump whenever the pickled CircuitGraph shape changes in a way that would make
# older cache files crash or behave incorrectly. Cache files are tagged with
# this and rejected (then rebuilt) on mismatch.
CACHE_VERSION = 2


class KiCadMCPConfig:
    """Manages board configurations and caching."""

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize configuration system.

        Args:
            config_path: Path to config file (defaults to .kicad_mcp.yaml in cwd or parent dirs)
        """
        self.config_path = config_path or self._find_config()
        self.config = self._load_config()
        self.cache_dir = self._setup_cache_dir()
        self._cached_boards: Dict[str, CircuitGraph] = {}
        self._last_diff: Optional[Dict[str, Any]] = None  # Store last detected diff

    def _find_config(self) -> Path:
        """Search for config file with priority order:
        1. Environment variable: KICAD_MCP_CONFIG
        2. Local project: .kicad_mcp.yaml (cwd or parent dirs)
        3. Global config: ~/.config/kicad_mcp/config.yaml
        4. Default: empty config (returns non-existent path)
        """
        # 1. Check environment variable
        env_config = os.environ.get('KICAD_MCP_CONFIG')
        if env_config:
            env_path = Path(env_config).expanduser()
            if env_path.exists():
                print(f"Using config from environment: {env_path}")
                return env_path
            else:
                print(f"Warning: KICAD_MCP_CONFIG points to non-existent file: {env_path}")

        # 2. Search up the directory tree for local project config
        current = Path.cwd()
        for _ in range(5):  # Limit search depth
            for config_name in ['.kicad_mcp.yaml', '.kicad_mcp.yml']:
                config_file = current / config_name
                if config_file.exists():
                    print(f"Using local config: {config_file}")
                    return config_file

            if current.parent == current:
                break
            current = current.parent

        # 3. Check global config location
        global_config = Path.home() / '.config' / 'kicad_mcp' / 'config.yaml'
        if global_config.exists():
            print(f"Using global config: {global_config}")
            return global_config

        # 4. Default: return path that likely doesn't exist
        # This will trigger the default config in _load_config
        return Path.cwd() / '.kicad_mcp.yaml'

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file."""
        if not self.config_path.exists():
            # Return default config
            return {
                'boards': {},
                'systems': {},
                'cache': {
                    'enabled': False,
                    'directory': '~/.cache/kicad_mcp',
                    'check_mtime': True
                }
            }

        with open(self.config_path, 'r') as f:
            return yaml.safe_load(f) or {}

    def _setup_cache_dir(self) -> Optional[Path]:
        """Setup cache directory if caching is enabled."""
        if not self.config['cache'].get('enabled', False):
            return None

        cache_dir = Path(self.config['cache']['directory']).expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _get_cache_path(self, board_path: Path) -> Path:
        """Get cache file path for a board."""
        # Use hash of absolute path for cache filename
        path_hash = hashlib.md5(str(board_path.absolute()).encode()).hexdigest()[:8]
        return self.cache_dir / f"{board_path.stem}_{path_hash}.cache"

    def _is_cache_valid(self, board_path: Path, cache_path: Path) -> bool:
        """Check if cached board is still valid."""
        if not cache_path.exists():
            return False

        if not self.config['cache'].get('check_mtime', True):
            return True

        # Check modification times
        board_mtime = board_path.stat().st_mtime
        cache_mtime = cache_path.stat().st_mtime

        return cache_mtime > board_mtime

    def _save_cache(self, cache_path: Path, obj: Any) -> None:
        """Pickle an object to the cache, tagged with the current cache version."""
        with open(cache_path, 'wb') as f:
            pickle.dump({"version": CACHE_VERSION, "obj": obj}, f)

    def _load_cache(self, cache_path: Path) -> Optional[Any]:
        """Load a versioned cache entry, returning None if missing/stale/corrupt.

        Rejects legacy (unversioned) and version-mismatched payloads so that a
        CircuitGraph shape change can never resurrect an incompatible object.
        """
        try:
            with open(cache_path, 'rb') as f:
                payload = pickle.load(f)
        except Exception as e:
            print(f"Cache load failed: {e}")
            return None

        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            print(f"Ignoring stale cache (version mismatch): {cache_path.name}")
            return None

        return payload.get("obj")

    def _compute_diff(self, old_circuit: CircuitGraph, new_circuit: CircuitGraph) -> Dict[str, Any]:
        """Compute meaningful differences between two circuit versions.

        Args:
            old_circuit: Previous circuit state
            new_circuit: New circuit state

        Returns:
            Dict with categorized changes
        """
        diff = {
            'components_added': [],
            'components_removed': [],
            'components_changed': [],
            'nets_added': [],
            'nets_removed': [],
            'connections_changed': []
        }

        if not old_circuit.netlist or not new_circuit.netlist:
            return diff

        old_comps = old_circuit.netlist.components
        new_comps = new_circuit.netlist.components
        old_nets = old_circuit.netlist.nets
        new_nets = new_circuit.netlist.nets

        # Component changes
        old_refs = set(old_comps.keys())
        new_refs = set(new_comps.keys())

        # Added components
        for ref in new_refs - old_refs:
            comp = new_comps[ref]
            diff['components_added'].append({
                'ref': ref,
                'value': comp.value,
                'footprint': comp.footprint
            })

        # Removed components
        for ref in old_refs - new_refs:
            comp = old_comps[ref]
            diff['components_removed'].append({
                'ref': ref,
                'value': comp.value,
                'footprint': comp.footprint
            })

        # Changed components (value or footprint)
        for ref in old_refs & new_refs:
            old_comp = old_comps[ref]
            new_comp = new_comps[ref]

            if old_comp.value != new_comp.value or old_comp.footprint != new_comp.footprint:
                changes = {'ref': ref}
                if old_comp.value != new_comp.value:
                    changes['value'] = {'old': old_comp.value, 'new': new_comp.value}
                if old_comp.footprint != new_comp.footprint:
                    changes['footprint'] = {'old': old_comp.footprint, 'new': new_comp.footprint}
                diff['components_changed'].append(changes)

        # Net changes
        old_net_names = set(old_nets.keys())
        new_net_names = set(new_nets.keys())

        # Added nets
        for net_name in new_net_names - old_net_names:
            net = new_nets[net_name]
            diff['nets_added'].append({
                'name': net_name,
                'connections': len(net.connections)
            })

        # Removed nets
        for net_name in old_net_names - new_net_names:
            net = old_nets[net_name]
            diff['nets_removed'].append({
                'name': net_name,
                'connections': len(net.connections)
            })

        # Connection changes (same net, different connections)
        for net_name in old_net_names & new_net_names:
            old_conns = set((ref, pin) for ref, pin, _ in old_nets[net_name].connections)
            new_conns = set((ref, pin) for ref, pin, _ in new_nets[net_name].connections)

            if old_conns != new_conns:
                added = new_conns - old_conns
                removed = old_conns - new_conns
                if added or removed:
                    diff['connections_changed'].append({
                        'net': net_name,
                        'added': list(added),
                        'removed': list(removed)
                    })

        return diff

    def _format_diff(self, diff: Dict[str, Any]) -> str:
        """Format diff as human-readable markdown."""
        lines = ["## Changes Detected\n"]

        has_changes = False

        if diff['components_added']:
            has_changes = True
            for item in diff['components_added']:
                lines.append(f"- **{item['ref']}**: added ({item['value']})")

        if diff['components_removed']:
            has_changes = True
            for item in diff['components_removed']:
                lines.append(f"- **{item['ref']}**: removed ({item['value']})")

        if diff['components_changed']:
            has_changes = True
            for item in diff['components_changed']:
                if 'value' in item:
                    lines.append(f"- **{item['ref']}**: {item['value']['old']} → {item['value']['new']}")
                elif 'footprint' in item:
                    lines.append(f"- **{item['ref']}**: footprint changed")

        if diff['nets_added']:
            has_changes = True
            for item in diff['nets_added']:
                lines.append(f"- Net **{item['name']}**: added ({item['connections']} connections)")

        if diff['nets_removed']:
            has_changes = True
            for item in diff['nets_removed']:
                lines.append(f"- Net **{item['name']}**: removed")

        if diff['connections_changed']:
            has_changes = True
            for item in diff['connections_changed']:
                if item['added']:
                    for ref, pin in item['added']:
                        lines.append(f"- Net **{item['net']}**: {ref}:{pin} connected")
                if item['removed']:
                    for ref, pin in item['removed']:
                        lines.append(f"- Net **{item['net']}**: {ref}:{pin} disconnected")

        if not has_changes:
            return ""

        return '\n'.join(lines)

    def get_last_diff(self) -> Optional[str]:
        """Get formatted string of last detected diff, then clear it.

        Returns:
            Formatted diff string or None if no diff
        """
        if self._last_diff:
            formatted = self._format_diff(self._last_diff)
            self._last_diff = None
            return formatted if formatted else None
        return None

    def get_board_path(self, board_name: str) -> Optional[Path]:
        """Get the file path for a named board.

        Args:
            board_name: Board identifier from config

        Returns:
            Path to board schematic file, or None if not found
        """
        board_info = self.config.get('boards', {}).get(board_name)
        if not board_info:
            return None

        return Path(board_info['path']).expanduser()

    def get_board_ignore_list(self, board_name: str) -> List[str]:
        """Get the list of components to ignore for a board.

        Args:
            board_name: Board identifier from config

        Returns:
            List of component references to ignore, empty list if none
        """
        board_info = self.config.get('boards', {}).get(board_name)
        if not board_info:
            return []

        return board_info.get('ignore', [])

    def load_board(self, board_name: str, force_reload: bool = False) -> Optional[CircuitGraph]:
        """Load a board by name, using cache if available.

        Auto-reloads if the schematic file has been modified since last load.
        If changes are detected, stores diff accessible via get_last_diff().

        Args:
            board_name: Board identifier from config
            force_reload: Force reload even if cached

        Returns:
            CircuitGraph instance or None if board not found
        """
        board_path = self.get_board_path(board_name)
        if not board_path or not board_path.exists():
            print(f"Board '{board_name}' not found in config or file doesn't exist")
            return None

        # Check memory cache with mtime validation
        if not force_reload and board_name in self._cached_boards:
            cached = self._cached_boards[board_name]
            current_mtime = board_path.stat().st_mtime

            # Check if file has been modified since we loaded it
            if cached._load_mtime and cached._load_mtime >= current_mtime:
                return cached  # Cache is fresh

            # File has changed - reload and compute diff
            print(f"Schematic '{board_name}' has changed, auto-reloading...")
            new_circuit = CircuitGraph.from_kicad_schematic(board_path)

            # Compute and store diff
            if new_circuit:
                self._last_diff = self._compute_diff(cached, new_circuit)
                self._cached_boards[board_name] = new_circuit

                # Update disk cache if enabled
                if self.cache_dir:
                    cache_path = self._get_cache_path(board_path)
                    try:
                        self._save_cache(cache_path, new_circuit)
                    except Exception as e:
                        print(f"Cache save failed: {e}")

                return new_circuit

        # Try to load from disk cache (only if not in memory)
        if self.cache_dir and not force_reload:
            cache_path = self._get_cache_path(board_path)
            if self._is_cache_valid(board_path, cache_path):
                circuit = self._load_cache(cache_path)
                if circuit is not None:
                    print(f"Loaded '{board_name}' from cache")
                    self._cached_boards[board_name] = circuit
                    return circuit

        # Load from schematic
        print(f"Loading '{board_name}' from {board_path}")
        circuit = CircuitGraph.from_kicad_schematic(board_path)

        # Save to cache
        if self.cache_dir and circuit:
            cache_path = self._get_cache_path(board_path)
            try:
                self._save_cache(cache_path, circuit)
                print(f"Cached '{board_name}' for faster loading")
            except Exception as e:
                print(f"Cache save failed: {e}")

        # Store in memory cache
        self._cached_boards[board_name] = circuit
        return circuit

    def load_system(self, system_name: str, force_reload: bool = False) -> Optional[MultiBoardGraph]:
        """Load a multi-board system by name.

        Args:
            system_name: System identifier from config
            force_reload: Force reload even if cached

        Returns:
            MultiBoardGraph instance or None if system not found
        """
        system_info = self.config.get('systems', {}).get(system_name)
        if not system_info:
            print(f"System '{system_name}' not found in config")
            return None

        board_names = system_info.get('boards', [])
        if not board_names:
            print(f"System '{system_name}' has no boards defined")
            return None

        # Create multi-board graph
        multi = MultiBoardGraph()

        for board_name in board_names:
            board_path = self.get_board_path(board_name)
            ignore_list = self.get_board_ignore_list(board_name)
            if board_path and board_path.exists():
                multi.add_board(board_name, board_path, ignore_list=ignore_list)
            else:
                print(f"Warning: Board '{board_name}' not found, skipping")

        return multi if multi.boards else None

    def list_boards(self) -> List[str]:
        """List all configured boards."""
        boards = []
        for name, info in self.config.get('boards', {}).items():
            desc = info.get('description', 'No description')
            boards.append(f"{name}: {desc}")
        return boards

    def list_systems(self) -> List[str]:
        """List all configured systems."""
        systems = []
        for name, info in self.config.get('systems', {}).items():
            desc = info.get('description', 'No description')
            board_list = ', '.join(info.get('boards', []))
            systems.append(f"{name}: {desc} [{board_list}]")
        return systems

    def save_config(self) -> None:
        """Save current configuration to file."""
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)

    def add_board(self, name: str, path: str, description: str = "") -> None:
        """Add a board to the configuration.

        Args:
            name: Board identifier
            path: Path to schematic file
            description: Board description
        """
        if 'boards' not in self.config:
            self.config['boards'] = {}

        self.config['boards'][name] = {
            'path': str(Path(path).absolute()),
            'description': description
        }
        self.save_config()

    def add_system(self, name: str, boards: List[str], description: str = "") -> None:
        """Add a system to the configuration.

        Args:
            name: System identifier
            boards: List of board names
            description: System description
        """
        if 'systems' not in self.config:
            self.config['systems'] = {}

        self.config['systems'][name] = {
            'boards': boards,
            'description': description
        }
        self.save_config()

    def remove_board(self, name: str) -> bool:
        """Remove a board from the configuration.

        Args:
            name: Board identifier

        Returns:
            True if board was removed, False if not found
        """
        if 'boards' not in self.config or name not in self.config['boards']:
            return False

        del self.config['boards'][name]
        self.save_config()
        return True

    def remove_system(self, name: str) -> bool:
        """Remove a system from the configuration.

        Args:
            name: System identifier

        Returns:
            True if system was removed, False if not found
        """
        if 'systems' not in self.config or name not in self.config['systems']:
            return False

        del self.config['systems'][name]
        self.save_config()
        return True

    def reload_config(self) -> None:
        """Reload configuration from disk and clear cached boards."""
        self.config = self._load_config()
        self._cached_boards.clear()
        print(f"Configuration reloaded from {self.config_path}")