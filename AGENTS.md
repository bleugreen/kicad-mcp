# kicad-mcp

Model Context Protocol server for analyzing KiCad printed circuit boards: multi-board
schematic signal tracing, datasheet lookup, and PCB layout analysis (design-rule
checking, rendering, and route measurement) over `.kicad_pcb` files. The full tool
inventory and configuration format live in `README.md`.

## Where things live

- `src/kicad_mcp/server.py` — MCP tool definitions and dispatch; the package entry
  point is `kicad-mcp = kicad_mcp.server:main`.
- `src/kicad_mcp/config.py` — `KiCadMCPConfig`. All PCB source resolution routes
  through `KiCadMCPConfig.resolve_pcb_source` (a configured board name, a direct
  `.kicad_pcb` path, or a `.kicad_sch` sibling); it raises `ValueError` on a miss.
- `src/kicad_mcp/kicad_cli.py` — `find_kicad_cli` discovery (the `KICAD_CLI` env var,
  then `PATH`, then the macOS app-bundle path) plus the headless `kicad-cli` wrappers
  for DRC, 3D render, and per-layer SVG export.
- `src/kicad_mcp/pcb_model.py` — the typed in-memory `.kicad_pcb` model parsed with
  `kiutils`, cached by `(path, mtime)`; the query API the parsed-model tools build on.
- `src/kicad_mcp/pcb_route.py` — copper route and length analysis over the parsed model.
- `src/kicad_mcp/pcb_rendering.py` — direct 2D PNG rendering from the parsed model.
- `src/kicad_mcp/kicad_ipc.py` — live KiCad 9 IPC (`kicad-python` / `kipy`) session tools.
- `tests/` — the pytest suite, with committed synthetic boards under `tests/fixtures/`.

## Validating changes

`uv run pytest` is the only green gate, and it must stay green (the suite passes from a
clean checkout). Run it after `uv sync --extra dev`: a plain `uv sync` does not install
pytest, ruff, or mypy, so pytest fails to spawn without the dev extra.

`ruff` and `mypy` are **not** clean baselines. The tree carries a few hundred
pre-existing ruff findings and dozens of mypy errors in the older modules, so their
whole-repo output is not a regression signal. Scope any lint or type check to the lines
you actually changed rather than treating the repo total as something to drive to zero.

Some tests skip unless optional local resources are present — `kicad-cli` (located via
`find_kicad_cli`) and private real boards referenced by absolute path. Their absence
skips those tests rather than failing them, so a green run on a machine without them is
expected.
