"""
Real page/content extraction for FSSHTTPB-encoded .one section files.

Real page order comes from the section root's ElementChildNodes property,
and real per-page content comes from walking that page's own, structurally
separate cell (a page's content can't bleed into another page's, since
each page lives in its own revision chain -- see onestore_objects.py's
Cell class).

See onestore_objects.py's module docstring for how CompactID resolution
itself was verified (cross-checked against msiemens/onenote.rs).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import fsshttpb as fb
from . import onestore_objects as oo

# [MS-ONE] PropertyID constants.
_PID_CONTENT_CHILD_NODES = 0x24001C1F
_PID_ELEMENT_CHILD_NODES = 0x24001C20
_PID_RICH_EDIT_TEXT_UNICODE = 0x1C001C22
_PID_CHILD_GRAPHSPACE_ELEMENT_NODES = 0x2C001D63
_PID_META_OBJECTS_ABOVE_GRAPHSPACE = 0x24003442
_PID_CACHED_TITLE_STRING = 0x1C001CF3


def _decode_text(raw: bytes | None) -> str | None:
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        text = raw.decode('utf-16-le').rstrip('\x00').strip()
    except UnicodeDecodeError:
        return None
    return text or None


@dataclass
class RealPage:
    title: str
    lines: list[tuple[int, str]] = field(default_factory=list)  # (depth, text)


def _walk_content(cell: oo.Cell, guid, depth: int, out: list, seen: set) -> None:
    key = (guid, 1)
    if guid in seen or key not in cell.object_map:
        return
    seen.add(guid)
    props = oo.decode_object(cell, key)

    text = _decode_text(props.get(_PID_RICH_EDIT_TEXT_UNICODE))
    if text:
        out.append((depth, text))

    for child in (props.get(_PID_CONTENT_CHILD_NODES) or []):
        if child:
            _walk_content(cell, child, depth, out, seen)
    for child in (props.get(_PID_ELEMENT_CHILD_NODES) or []):
        if child:
            _walk_content(cell, child, depth + 1, out, seen)


def _page_title(section_cell: oo.Cell, page_container_guid) -> str | None:
    props = oo.decode_object(section_cell, (page_container_guid, 1))
    meta_refs = props.get(_PID_META_OBJECTS_ABOVE_GRAPHSPACE) or []
    for meta_guid in meta_refs:
        if meta_guid is None or (meta_guid, 1) not in section_cell.object_map:
            continue
        meta_props = oo.decode_object(section_cell, (meta_guid, 1))
        title = _decode_text(meta_props.get(_PID_CACHED_TITLE_STRING))
        if title:
            return title
    return None


def extract_pages(path) -> list[RealPage] | None:
    """Real page list + real per-page content for a .one section file, or
    None if this file doesn't decode as an FSSHTTPB Data Element Package
    (or the walk hits a structure this module doesn't understand yet) --
    callers should fall back to a heuristic extractor in that case."""
    data = path.read_bytes() if hasattr(path, 'read_bytes') else open(path, 'rb').read()

    pkg, _ = fb.parse_packaging_structure(data)
    elements = pkg['data_elements']
    storage_index = next((e for e in elements if e['type'] == 0x01), None)
    if storage_index is None:
        return None

    section_cell = oo.get_data_root_cell(elements)
    if section_cell is None or section_cell.content_root() is None:
        return None

    section_props = oo.decode_object(section_cell, (section_cell.content_root(), 1))
    page_containers = section_props.get(_PID_ELEMENT_CHILD_NODES) or []
    if not page_containers:
        return None

    pages: list[RealPage] = []
    for page_guid in page_containers:
        if page_guid is None:
            continue
        title = _page_title(section_cell, page_guid) or '(untitled page)'

        page_props = oo.decode_object(section_cell, (page_guid, 1))
        graphspace_refs = page_props.get(_PID_CHILD_GRAPHSPACE_ELEMENT_NODES) or []
        lines: list[tuple[int, str]] = []
        for cell_id in graphspace_refs:
            if cell_id is None:
                continue
            page_cell = oo.resolve_cell(elements, storage_index, cell_id)
            if page_cell is None:
                continue
            content_root = page_cell.content_root()
            if content_root is None:
                continue
            seen: set = set()
            _walk_content(page_cell, content_root, 0, lines, seen)

        if lines:
            min_depth = min(d for d, _ in lines)
            lines = [(d - min_depth, t) for d, t in lines]
        pages.append(RealPage(title=title, lines=lines))

    return pages
