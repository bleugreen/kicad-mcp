"""Headless ``kicad-cli`` wrappers for PCB tooling, plus shared CLI discovery.

``find_kicad_cli`` is the single discovery point every kicad-cli consumer
(netlist export, PCB analysis) routes through so behavior stays consistent. The
PCB wrappers (DRC, render, SVG export) are importable as plain functions so a
future crop/highlight tool can call the primitives directly without going
through the MCP server.

The wrappers are deliberately *fail-closed*: any invocation that times out,
exits nonzero, or produces missing/empty/unparseable output raises
:class:`KiCadCLIError`. This matters most for DRC — a silent empty result that
reads as a clean pass is the worst possible outcome for a design-rule check.

PCB source resolution (board name / ``.kicad_pcb`` path / ``.kicad_sch`` sibling)
lives on :meth:`KiCadMCPConfig.resolve_pcb_source`; these wrappers route through
it rather than owning a second resolver.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Imported for type hints only. A runtime import here would form a cycle
    # (config -> circuit_graph -> kicad_cli -> config); _resolve_pcb imports it
    # lazily instead.
    from .config import KiCadMCPConfig

# Generous defaults: DRC on a multi-megabyte 4-layer board can take tens of
# seconds, and a 3D render loads 3D models.
DRC_TIMEOUT_S = 300
RENDER_TIMEOUT_S = 300
SVG_TIMEOUT_S = 180


class KiCadCLIError(RuntimeError):
    """A kicad-cli invocation failed in a way the caller must surface.

    Raised on nonzero exit, timeout, or missing/empty/unparseable output.
    """


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


def _resolve_pcb(source: str, config: KiCadMCPConfig | None) -> Path:
    """Resolve a board name or path to a ``.kicad_pcb`` via the config resolver.

    All PCB source resolution routes through
    :meth:`KiCadMCPConfig.resolve_pcb_source` so board lookup stays in one place.
    """
    from .config import KiCadMCPConfig

    cfg = config or KiCadMCPConfig()
    return cfg.resolve_pcb_source(source)


def _run_cli(
    cmd: Sequence[str], timeout: int, what: str
) -> subprocess.CompletedProcess:
    """Run a kicad-cli command, converting failures into :class:`KiCadCLIError`."""
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KiCadCLIError(
            f"{what} timed out after {timeout}s (board too large or kicad-cli hung)."
        ) from exc
    except OSError as exc:
        raise KiCadCLIError(f"{what} could not start: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise KiCadCLIError(
            f"{what} failed (exit {proc.returncode}): {detail or 'no output'}"
        )
    return proc


def _output_dir(output_dir: str | None, prefix: str) -> Path:
    """Resolve (and create) an output directory, defaulting to a per-run temp dir."""
    out = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix=prefix))
    out.mkdir(parents=True, exist_ok=True)
    return out


# --- DRC ---------------------------------------------------------------------

# The categories the DRC JSON report groups problems under. Each carries the
# same per-entry shape ({type, description, severity, items[...]}).
_DRC_CATEGORIES = ("violations", "unconnected_items", "schematic_parity")
_VALID_SEVERITIES = ("error", "warning", "exclusion")


def run_drc(
    source: str,
    *,
    severity: str = "all",
    max_violations: int | None = None,
    units: str = "mm",
    timeout: int = DRC_TIMEOUT_S,
    config: KiCadMCPConfig | None = None,
    output_dir: str | None = None,
    cli: str | None = None,
) -> dict[str, Any]:
    """Run ``kicad-cli pcb drc --format json`` and return a structured summary.

    Always requests every severity from the CLI so the on-disk report is
    complete, then filters/limits in Python. Fail-closed: raises
    :class:`KiCadCLIError` on any CLI failure or missing/unparseable report,
    never returning an empty-but-successful result.

    Args:
        source: Board name or path (see
            :meth:`KiCadMCPConfig.resolve_pcb_source`).
        severity: Which severities to include in the parsed output — ``all`` or
            one of ``error``/``warning``/``exclusion``.
        max_violations: Cap on individual problems listed (totals stay exact).
        units: Coordinate units for the report (``mm``, ``in``, ``mils``).
        timeout: Subprocess timeout in seconds.
        output_dir: Directory for the JSON report (default: a per-run temp dir).
        cli: Override kicad-cli path (mainly for tests).

    Returns:
        The dict produced by :func:`parse_drc_report`, augmented with
        ``report_path`` and ``duration_s``.
    """
    pcb = _resolve_pcb(source, config)
    cli = cli or find_kicad_cli()

    out_dir = _output_dir(output_dir, "kicad_mcp_drc_")
    report_path = out_dir / f"{pcb.stem}-drc.json"

    cmd = [
        cli,
        "pcb",
        "drc",
        "--format",
        "json",
        "--severity-all",
        "--units",
        units,
        "--output",
        str(report_path),
        str(pcb),
    ]

    start = time.monotonic()
    _run_cli(cmd, timeout, "DRC")
    duration = time.monotonic() - start

    if not report_path.exists() or report_path.stat().st_size == 0:
        raise KiCadCLIError(
            f"DRC reported success but wrote no report to {report_path}. "
            f"Refusing to report a clean pass on a run that produced no output."
        )

    try:
        data = json.loads(report_path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        raise KiCadCLIError(
            f"DRC report at {report_path} is not valid JSON: {exc}. "
            f"Refusing to report a clean pass on an unparseable report."
        ) from exc

    result = parse_drc_report(data, severity=severity, max_violations=max_violations)
    result["report_path"] = str(report_path)
    result["duration_s"] = round(duration, 2)
    return result


def parse_drc_report(
    data: Any,
    *,
    severity: str = "all",
    max_violations: int | None = None,
) -> dict[str, Any]:
    """Parse a KiCad DRC JSON report into a grouped, filtered summary.

    Combines the ``violations``, ``unconnected_items`` and ``schematic_parity``
    categories, tagging each problem with its category, then groups by rule
    ``type``. Totals (by severity and category) are always computed over the
    full, severity-filtered set; ``max_violations`` only caps the number of
    individual problems enumerated in ``groups``.

    Raises :class:`KiCadCLIError` if ``data`` is not a JSON object (a defensive
    guard so garbage never parses into a clean-looking empty pass).
    """
    if not isinstance(data, dict):
        raise KiCadCLIError(
            f"DRC report is not a JSON object (got {type(data).__name__})."
        )

    severity = (severity or "all").lower()
    if severity not in ("all", *_VALID_SEVERITIES):
        raise KiCadCLIError(
            f"Invalid severity filter '{severity}'; expected one of "
            f"all/{'/'.join(_VALID_SEVERITIES)}."
        )

    problems: list[dict[str, Any]] = []
    for category in _DRC_CATEGORIES:
        entries = data.get(category) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sev = entry.get("severity", "unknown")
            if severity != "all" and sev != severity:
                continue
            problems.append(
                {
                    "type": entry.get("type", category),
                    "category": category,
                    "severity": sev,
                    "description": entry.get("description", ""),
                    "items": _normalize_items(entry.get("items") or []),
                }
            )

    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for p in problems:
        by_severity[p["severity"]] = by_severity.get(p["severity"], 0) + 1
        by_category[p["category"]] = by_category.get(p["category"], 0) + 1

    # Group by rule type, preserving first-seen order.
    groups: dict[str, dict[str, Any]] = {}
    for p in problems:
        g = groups.get(p["type"])
        if g is None:
            g = {
                "type": p["type"],
                "category": p["category"],
                "count": 0,
                "severities": {},
                "problems": [],
            }
            groups[p["type"]] = g
        g["count"] += 1
        g["severities"][p["severity"]] = g["severities"].get(p["severity"], 0) + 1
        g["problems"].append(p)

    # Apply max_violations as a global cap on enumerated problems, spread across
    # groups in the order they were first seen. Counts/totals are untouched.
    truncated = False
    if max_violations is not None and len(problems) > max_violations:
        truncated = True
        remaining = max(max_violations, 0)
        for g in groups.values():
            if remaining <= 0:
                g["problems"] = []
            elif len(g["problems"]) > remaining:
                g["problems"] = g["problems"][:remaining]
                remaining = 0
            else:
                remaining -= len(g["problems"])

    return {
        "source": data.get("source"),
        "kicad_version": data.get("kicad_version"),
        "date": data.get("date"),
        "coordinate_units": data.get("coordinate_units"),
        "total": len(problems),
        "by_severity": by_severity,
        "by_category": by_category,
        "severity_filter": severity,
        "truncated": truncated,
        "groups": sorted(groups.values(), key=lambda g: g["count"], reverse=True),
    }


def _normalize_items(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Flatten DRC item entries to ``{description, x, y, uuid}`` dicts."""
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pos = item.get("pos") or {}
        out.append(
            {
                "description": item.get("description", ""),
                "x": pos.get("x") if isinstance(pos, dict) else None,
                "y": pos.get("y") if isinstance(pos, dict) else None,
                "uuid": item.get("uuid"),
            }
        )
    return out


# --- Render ------------------------------------------------------------------


def render_pcb(
    source: str,
    *,
    side: str = "top",
    width: int = 1600,
    height: int = 900,
    background: str | None = None,
    quality: str = "basic",
    preset: str | None = None,
    zoom: float | None = None,
    rotate: str | None = None,
    pan: str | None = None,
    pivot: str | None = None,
    perspective: bool = False,
    floor: bool = False,
    output_dir: str | None = None,
    timeout: int = RENDER_TIMEOUT_S,
    config: KiCadMCPConfig | None = None,
    cli: str | None = None,
) -> dict[str, Any]:
    """Render a PCB to a PNG via ``kicad-cli pcb render``.

    Writes into a per-run output directory (never the board's own directory).
    Fail-closed: raises :class:`KiCadCLIError` unless a non-empty PNG results.

    Args mirror the useful camera controls the subcommand exposes; ``rotate``,
    ``pan`` and ``pivot`` are passed through verbatim as ``X,Y,Z`` strings.

    Returns a dict with ``path``, ``side``, ``width``, ``height``,
    ``size_bytes`` and ``duration_s``.
    """
    pcb = _resolve_pcb(source, config)
    cli = cli or find_kicad_cli()

    out_dir = _output_dir(output_dir, "kicad_mcp_render_")
    out_path = out_dir / f"{pcb.stem}-{side}.png"

    cmd = [
        cli,
        "pcb",
        "render",
        "--side",
        side,
        "--width",
        str(width),
        "--height",
        str(height),
        "--quality",
        quality,
        "--output",
        str(out_path),
    ]
    if background:
        cmd += ["--background", background]
    if preset:
        cmd += ["--preset", preset]
    if zoom is not None:
        cmd += ["--zoom", str(zoom)]
    if rotate:
        cmd += ["--rotate", rotate]
    if pan:
        cmd += ["--pan", pan]
    if pivot:
        cmd += ["--pivot", pivot]
    if perspective:
        cmd += ["--perspective"]
    if floor:
        cmd += ["--floor"]
    cmd.append(str(pcb))

    start = time.monotonic()
    _run_cli(cmd, timeout, "Render")
    duration = time.monotonic() - start

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise KiCadCLIError(
            f"Render reported success but produced no image at {out_path}."
        )

    return {
        "path": str(out_path),
        "side": side,
        "width": width,
        "height": height,
        "size_bytes": out_path.stat().st_size,
        "duration_s": round(duration, 2),
    }


def render_pcb_base64(source: str, **kwargs: Any) -> dict[str, Any]:
    """Like :func:`render_pcb` but also returns the PNG as base64 (``image_base64``)."""
    result = render_pcb(source, **kwargs)
    png_bytes = Path(result["path"]).read_bytes()
    result["image_base64"] = base64.b64encode(png_bytes).decode()
    return result


# --- SVG export --------------------------------------------------------------

# Board-area fit is what the crop tool needs; the other modes match the CLI's
# --page-size-mode integers.
_PAGE_SIZE_MODES = {"page": 0, "current": 1, "board": 2}


def export_layers_svg(
    source: str,
    layers: Sequence[str] | str,
    *,
    output_dir: str | None = None,
    fit: str = "board",
    black_and_white: bool = False,
    theme: str | None = None,
    exclude_drawing_sheet: bool = True,
    timeout: int = SVG_TIMEOUT_S,
    config: KiCadMCPConfig | None = None,
    cli: str | None = None,
) -> list[Path]:
    """Export one SVG per layer via ``kicad-cli pcb export svg --mode-multi``.

    ``fit`` selects the page-size mode: ``board`` (board-area only, the default
    the crop tool relies on), ``page`` (framed page), or ``current``.

    Returns the list of generated SVG :class:`Path` objects (one per layer),
    verifying each exists and is non-empty. Fail-closed via
    :class:`KiCadCLIError`.
    """
    pcb = _resolve_pcb(source, config)
    cli = cli or find_kicad_cli()

    layer_list = (
        [layer_str.strip() for layer_str in layers.split(",")]
        if isinstance(layers, str)
        else [str(layer_str).strip() for layer_str in layers]
    )
    layer_list = [layer_str for layer_str in layer_list if layer_str]
    if not layer_list:
        raise KiCadCLIError("No layers specified for SVG export.")

    if fit not in _PAGE_SIZE_MODES:
        raise KiCadCLIError(
            f"Invalid fit '{fit}'; expected one of {'/'.join(_PAGE_SIZE_MODES)}."
        )

    out_dir = _output_dir(output_dir, "kicad_mcp_svg_")

    cmd = [
        cli,
        "pcb",
        "export",
        "svg",
        "--layers",
        ",".join(layer_list),
        "--mode-multi",
        "--page-size-mode",
        str(_PAGE_SIZE_MODES[fit]),
        "--output",
        str(out_dir),
    ]
    if black_and_white:
        cmd += ["--black-and-white"]
    if theme:
        cmd += ["--theme", theme]
    if exclude_drawing_sheet:
        cmd += ["--exclude-drawing-sheet"]
    cmd.append(str(pcb))

    _run_cli(cmd, timeout, "SVG export")

    # In --mode-multi, kicad-cli writes '<board stem>-<layer>.svg' with the
    # layer's '.' replaced by '_'.
    paths: list[Path] = []
    for layer in layer_list:
        svg = out_dir / f"{pcb.stem}-{layer.replace('.', '_')}.svg"
        if not svg.exists() or svg.stat().st_size == 0:
            raise KiCadCLIError(
                f"SVG export did not produce a non-empty file for layer '{layer}' "
                f"(expected {svg})."
            )
        paths.append(svg)
    return paths

