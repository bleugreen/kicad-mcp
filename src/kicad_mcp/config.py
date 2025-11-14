"""Configuration and caching system for KiCad MCP."""

import os
import yaml
import pickle
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from .circuit_graph_netlist import CircuitGraph
from .multi_board_graph import MultiBoardGraph


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

        Args:
            board_name: Board identifier from config
            force_reload: Force reload even if cached

        Returns:
            CircuitGraph instance or None if board not found
        """
        # Check memory cache first
        if not force_reload and board_name in self._cached_boards:
            return self._cached_boards[board_name]

        board_path = self.get_board_path(board_name)
        if not board_path or not board_path.exists():
            print(f"Board '{board_name}' not found in config or file doesn't exist")
            return None

        # Try to load from disk cache
        if self.cache_dir and not force_reload:
            cache_path = self._get_cache_path(board_path)
            if self._is_cache_valid(board_path, cache_path):
                try:
                    with open(cache_path, 'rb') as f:
                        circuit = pickle.load(f)
                    print(f"Loaded '{board_name}' from cache")
                    self._cached_boards[board_name] = circuit
                    return circuit
                except Exception as e:
                    print(f"Cache load failed: {e}")

        # Load from schematic
        print(f"Loading '{board_name}' from {board_path}")
        circuit = CircuitGraph.from_kicad_schematic(board_path)

        # Save to cache
        if self.cache_dir and circuit:
            cache_path = self._get_cache_path(board_path)
            try:
                with open(cache_path, 'wb') as f:
                    pickle.dump(circuit, f)
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