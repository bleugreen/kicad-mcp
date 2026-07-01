"""Live KiCad IPC integration built on the official kicad-python client.

This module intentionally exposes a small, plain-dict service layer around
``kipy``.  The MCP server can format those dicts, and tests can stub the service
without needing a running KiCad GUI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import KiCadMCPConfig

DEFAULT_TIMEOUT_MS = 1000
CLIENT_NAME = "kicad-mcp"
ENABLE_API_HINT = "Enable KiCad API in KiCad Preferences → Plugins."


def _nm_to_mm(value_nm: int | float | None) -> float | None:
    if value_nm is None:
        return None
    return round(float(value_nm) / 1_000_000, 6)


def _vector_to_mm(value: Any) -> dict[str, float] | None:
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if x is None or y is None:
        return None
    return {
        "x_mm": round(float(x) / 1_000_000, 6),
        "y_mm": round(float(y) / 1_000_000, 6),
    }


def _id_value(item: Any) -> str | None:
    item_id = getattr(item, "id", None)
    value = getattr(item_id, "value", None)
    return str(value) if value else None


@dataclass
class KiCadIPCError(Exception):
    """Actionable live KiCad IPC failure."""

    message: str
    socket_path: str | None = None

    def __str__(self) -> str:
        if self.socket_path:
            return f"{self.message} Attempted socket: {self.socket_path}."
        return self.message


class KiCadIPC:
    """Small live-session adapter for KiCad's official IPC API."""

    def __init__(
        self,
        config: KiCadMCPConfig | None = None,
        *,
        socket_path: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.config = config or KiCadMCPConfig()
        self.socket_path = socket_path
        self.timeout_ms = timeout_ms

    def session(self) -> dict[str, Any]:
        """Return KiCad reachability, version, and open PCB documents."""
        kicad = self._connect()
        document_type = self._document_type()
        version = kicad.get_version()
        boards = [
            self._document_info(doc)
            for doc in kicad.get_open_documents(document_type.DOCTYPE_PCB)
        ]
        return {
            "reachable": True,
            "version": getattr(version, "full_version", str(version)),
            "api_socket": self._default_socket_path(),
            "boards": boards,
        }

    def focus(
        self, *, reference: str | None = None, position: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Select a footprint or item at a position in the live PCB editor.

        KiCad 9's Python IPC surface exposes selection but not a typed view-pan or
        zoom-to-item command.  This therefore performs the reversible selection
        half of cross-probing and reports that view centering is unavailable.
        """
        if bool(reference) == bool(position):
            raise ValueError("Pass exactly one of reference or position")

        board = self._board()
        if reference:
            item = self._find_footprint(board, reference)
            if item is None:
                raise ValueError(
                    f"Footprint '{reference}' was not found in the live board"
                )
            selected = self._replace_selection(board, [item])
            return {
                "action": "focus_reference",
                "reference": reference,
                "selected_count": len(selected),
                "selection": self.selection_summary(selected),
                "view_control": "not_available_in_kicad_python_0.7.1",
            }

        assert position is not None
        x_mm = _required_float(position, "x_mm")
        y_mm = _required_float(position, "y_mm")
        item = self._hit_test_item(board, x_mm, y_mm)
        if item is None:
            raise ValueError(f"No selectable item found at ({x_mm}, {y_mm}) mm")
        selected = self._replace_selection(board, [item])
        return {
            "action": "focus_position",
            "position": {"x_mm": x_mm, "y_mm": y_mm},
            "selected_count": len(selected),
            "selection": self.selection_summary(selected),
            "view_control": "not_available_in_kicad_python_0.7.1",
        }

    def highlight_net(self, net: str) -> dict[str, Any]:
        """Select all live board items on ``net`` so KiCad highlights them."""
        if not net:
            raise ValueError("net parameter is required")
        board = self._board()
        net_obj = self._find_net(board, net)
        if net_obj is None:
            raise ValueError(f"Net '{net}' was not found in the live board")

        # KiCad 9.0.7's IPC server has no handler for GetItemsByNet (verified
        # against a live session), even though kipy 0.7.1 exposes it. Fetch the
        # copper items and filter by net client-side instead.
        net_name = getattr(net_obj, "name", str(net))
        items = [
            item
            for item in board.get_items(self._copper_item_types())
            if net_name in self._item_nets(item)
        ]
        if not items:
            raise ValueError(
                f"Net '{net}' has no selectable copper items in the live board"
            )

        selected = self._replace_selection(board, items)
        summary = self.selection_summary(selected)
        return {
            "action": "highlight_net",
            "net": getattr(net_obj, "name", net),
            "selected_count": len(selected),
            "selection": summary,
            "highlight_mode": "selected_copper_items",
        }

    def get_selection(self) -> dict[str, Any]:
        board = self._board()
        selected = list(board.get_selection())
        return self.selection_summary(selected)

    def selection_summary(self, items: Sequence[Any]) -> dict[str, Any]:
        normalized = [self._item_summary(item) for item in items]
        references = sorted(
            {item["reference"] for item in normalized if item.get("reference")}
        )
        nets = sorted({net for item in normalized for net in item.get("nets", [])})
        type_counts: dict[str, int] = {}
        for item in normalized:
            item_type = item["type"]
            type_counts[item_type] = type_counts.get(item_type, 0) + 1
        return {
            "count": len(normalized),
            "references": references,
            "nets": nets,
            "type_counts": type_counts,
            "items": normalized,
        }

    def open_board(self, source: str) -> dict[str, Any]:
        """Resolve a board source and report why live opening is unavailable.

        The official KiCad 9 ``kicad-python`` surface exposes open-document
        discovery but no open-board command.  Keeping this as an explicit
        fail-closed tool is more honest than pretending to open via side effects.
        """
        if not source:
            raise ValueError("source parameter is required")
        path = self.config.resolve_pcb_source(source)
        raise KiCadIPCError(
            "kicad_open_board is not supported by kicad-python 0.7.1 / KiCad 9 IPC; "
            f"resolved source to {path} but did not open it."
        )

    def _connect(self) -> Any:
        try:
            from kipy.errors import ApiError
            from kipy.errors import ConnectionError as KiCadConnectionError
            from kipy.kicad import KiCad
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise KiCadIPCError(
                "kicad-python is not installed; install the kicad-python package"
            ) from exc

        socket_path = self._default_socket_path()
        try:
            kicad = KiCad(
                socket_path=self.socket_path,
                client_name=CLIENT_NAME,
                timeout_ms=self.timeout_ms,
            )
            kicad.ping()
            return kicad
        except KiCadConnectionError as exc:
            raise KiCadIPCError(
                "Could not reach a running KiCad IPC API server: "
                f"{exc}. {ENABLE_API_HINT}",
                socket_path=socket_path,
            ) from exc
        except ApiError as exc:
            raise KiCadIPCError(
                f"KiCad IPC API returned an error during connection: {exc}",
                socket_path=socket_path,
            ) from exc

    def _board(self) -> Any:
        kicad = self._connect()
        try:
            return kicad.get_board()
        except Exception as exc:
            raise KiCadIPCError(
                f"KiCad is reachable but no PCB board document is open: {exc}"
            ) from exc

    def _default_socket_path(self) -> str:
        try:
            from kipy.kicad import _default_socket_path

            return self.socket_path or _default_socket_path()
        except ImportError:  # pragma: no cover - dependency is declared
            return self.socket_path or "ipc:///tmp/kicad/api.sock"

    @staticmethod
    def _document_type() -> Any:
        from kipy.proto.common.types import DocumentType

        return DocumentType

    @staticmethod
    def _document_info(document: Any) -> dict[str, Any]:
        board_filename = getattr(document, "board_filename", "")
        project_filename = getattr(document, "project_filename", "")
        return {
            "path": board_filename,
            "name": Path(board_filename).name if board_filename else "",
            "project": project_filename,
        }

    @staticmethod
    def _copper_item_types() -> list[int]:
        from kipy.proto.common.types import KiCadObjectType

        return [
            KiCadObjectType.KOT_PCB_TRACE,
            KiCadObjectType.KOT_PCB_ARC,
            KiCadObjectType.KOT_PCB_VIA,
            KiCadObjectType.KOT_PCB_PAD,
            KiCadObjectType.KOT_PCB_ZONE,
        ]

    @staticmethod
    def _find_net(board: Any, net_name: str) -> Any | None:
        for net in board.get_nets():
            if getattr(net, "name", None) == net_name or str(
                getattr(net, "code", "")
            ) == str(net_name):
                return net
        return None

    @staticmethod
    def _find_footprint(board: Any, reference: str) -> Any | None:
        for footprint in board.get_footprints():
            ref_field = getattr(footprint, "reference_field", None)
            ref_text = getattr(getattr(ref_field, "text", None), "value", None)
            if ref_text == reference:
                return footprint
        return None

    @staticmethod
    def _replace_selection(board: Any, items: Sequence[Any]) -> Sequence[Any]:
        board.clear_selection()
        return list(board.add_to_selection(list(items)))

    @staticmethod
    def _hit_test_item(board: Any, x_mm: float, y_mm: float) -> Any | None:
        from kipy.geometry import Vector2

        target = Vector2.from_xy_mm(x_mm, y_mm)
        candidates = list(
            board.get_items(
                KiCadIPC._copper_item_types() + [KiCadIPC._footprint_type()]
            )
        )
        for item in candidates:
            bbox = board.get_item_bounding_box(item, include_text=True)
            if _box_contains(bbox, target.x, target.y):
                return item
        return None

    @staticmethod
    def _footprint_type() -> int:
        from kipy.proto.common.types import KiCadObjectType

        return KiCadObjectType.KOT_PCB_FOOTPRINT

    def _item_summary(self, item: Any) -> dict[str, Any]:
        item_type = type(item).__name__
        summary: dict[str, Any] = {"type": item_type}
        item_id = _id_value(item)
        if item_id:
            summary["id"] = item_id

        reference = self._item_reference(item)
        if reference:
            summary["reference"] = reference

        nets = self._item_nets(item)
        if nets:
            summary["nets"] = sorted(set(nets))

        position = self._item_position(item)
        if position:
            summary["position"] = position
        return summary

    @staticmethod
    def _item_reference(item: Any) -> str | None:
        ref_field = getattr(item, "reference_field", None)
        ref_text = getattr(getattr(ref_field, "text", None), "value", None)
        if ref_text:
            return str(ref_text)
        name = getattr(item, "name", None)
        if type(item).__name__ == "Field" and name == "Reference":
            return str(getattr(getattr(item, "text", None), "value", "") or "") or None
        return None

    @staticmethod
    def _item_nets(item: Any) -> list[str]:
        nets: list[str] = []
        net = getattr(item, "net", None)
        net_name = getattr(net, "name", None)
        if net_name:
            nets.append(str(net_name))
        definition = getattr(item, "definition", None)
        pads = getattr(definition, "pads", []) if definition is not None else []
        for pad in pads:
            pad_net_name = getattr(getattr(pad, "net", None), "name", None)
            if pad_net_name:
                nets.append(str(pad_net_name))
        return nets

    @staticmethod
    def _item_position(item: Any) -> dict[str, float] | None:
        position = getattr(item, "position", None)
        if position is not None:
            return _vector_to_mm(position)
        text = getattr(item, "text", None)
        return _vector_to_mm(getattr(text, "position", None))


def _required_float(values: dict[str, Any], key: str) -> float:
    if key not in values:
        raise ValueError(f"position.{key} is required")
    try:
        return float(values[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position.{key} must be a number") from exc


def _box_contains(box: Any, x_nm: int, y_nm: int) -> bool:
    if box is None:
        return False
    proto = getattr(box, "proto", None)
    if proto is not None:
        min_x = getattr(getattr(proto, "top_left", None), "x_nm", None)
        min_y = getattr(getattr(proto, "top_left", None), "y_nm", None)
        max_x = getattr(getattr(proto, "bottom_right", None), "x_nm", None)
        max_y = getattr(getattr(proto, "bottom_right", None), "y_nm", None)
        if (
            min_x is not None
            and min_y is not None
            and max_x is not None
            and max_y is not None
        ):
            lo_x, hi_x = sorted((int(min_x), int(max_x)))
            lo_y, hi_y = sorted((int(min_y), int(max_y)))
            return bool(lo_x <= x_nm <= hi_x and lo_y <= y_nm <= hi_y)

    pos = getattr(box, "pos", None)
    size = getattr(box, "size", None)
    if pos is not None and size is not None:
        pos_x = int(pos.x)
        pos_y = int(pos.y)
        size_x = int(size.x)
        size_y = int(size.y)
        lo_x, hi_x = sorted((pos_x, pos_x + size_x))
        lo_y, hi_y = sorted((pos_y, pos_y + size_y))
        return bool(lo_x <= x_nm <= hi_x and lo_y <= y_nm <= hi_y)

    left = getattr(box, "left", None)
    right = getattr(box, "right", None)
    top = getattr(box, "top", None)
    bottom = getattr(box, "bottom", None)
    if (
        left is not None
        and right is not None
        and top is not None
        and bottom is not None
    ):
        lo_x, hi_x = sorted((int(left), int(right)))
        lo_y, hi_y = sorted((int(top), int(bottom)))
        return bool(lo_x <= x_nm <= hi_x and lo_y <= y_nm <= hi_y)
    return False


def format_session(data: dict[str, Any]) -> str:
    lines = [
        "# Live KiCad Session",
        "",
        f"**Reachable:** {data.get('reachable', False)}",
    ]
    if data.get("version"):
        lines.append(f"**KiCad version:** {data['version']}")
    if data.get("api_socket"):
        lines.append(f"**API socket:** {data['api_socket']}")
    boards = data.get("boards", [])
    lines.append(f"**Open PCB documents:** {len(boards)}")
    for board in boards:
        path = board.get("path") or "(unknown path)"
        lines.append(f"- {path}")
    return "\n".join(lines)


def format_selection(
    data: dict[str, Any], *, title: str = "Live KiCad Selection"
) -> str:
    lines = [f"# {title}", "", f"**Items:** {data.get('count', 0)}"]
    references = data.get("references") or []
    nets = data.get("nets") or []
    if references:
        lines.append(f"**References:** {', '.join(references)}")
    if nets:
        lines.append(f"**Nets:** {', '.join(nets)}")
    type_counts = data.get("type_counts") or {}
    if type_counts:
        lines.append("\n## Types")
        for item_type, count in sorted(type_counts.items()):
            lines.append(f"- {item_type}: {count}")
    items = data.get("items") or []
    if items:
        lines.append("\n## Items")
        for item in items[:25]:
            parts = [item.get("type", "Item")]
            if item.get("reference"):
                parts.append(f"ref={item['reference']}")
            if item.get("nets"):
                parts.append(f"nets={','.join(item['nets'])}")
            if item.get("position"):
                pos = item["position"]
                parts.append(f"@({pos['x_mm']:.3f}, {pos['y_mm']:.3f}) mm")
            lines.append(f"- {'; '.join(parts)}")
        if len(items) > 25:
            lines.append(f"- … {len(items) - 25} more item(s)")
    return "\n".join(lines)
