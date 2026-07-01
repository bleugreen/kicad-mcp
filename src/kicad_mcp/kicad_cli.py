"""Shared discovery of the kicad-cli executable.

kicad-cli location varies by platform and install method. Callers that need to
shell out to it (netlist export, PCB analysis) resolve the executable through
this single function so discovery behavior stays consistent.
"""

import os
import subprocess


def find_kicad_cli() -> str:
    """Locate a working kicad-cli executable.

    Resolution order:
    1. The ``KICAD_CLI`` environment variable, when set (explicit override).
    2. The macOS application bundle path.
    3. ``/usr/bin/kicad-cli`` (typical Linux install).
    4. ``kicad-cli`` on ``PATH``.

    Each candidate is probed with ``--version``; the first one that runs and
    exits 0 wins.

    Returns:
        The path or name of a working kicad-cli executable.

    Raises:
        RuntimeError: if no working kicad-cli can be found.
    """
    candidates = []

    override = os.environ.get("KICAD_CLI")
    if override:
        candidates.append(override)

    candidates.extend([
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS
        "/usr/bin/kicad-cli",  # Linux
        "kicad-cli",  # In PATH
    ])

    for path in candidates:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, timeout=2)
            if result.returncode == 0:
                return path
        except Exception:
            continue

    raise RuntimeError(
        "Could not find kicad-cli. Please install KiCad or provide the path."
    )
