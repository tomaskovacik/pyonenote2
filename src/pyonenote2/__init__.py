"""pyonenote2: a from-spec OneNote (.one / .onetoc2) reader.

Resolves real page order and real per-page content by walking the actual
[MS-ONESTORE]/[MS-FSSHTTPB] object graph, with a heuristic text-scan
fallback for files/structures it doesn't (yet) fully understand. See the
project README for background and known limitations.

Public API:
    extract_section(path) -> list[Page]
    render_section(path) -> str
    render_notebook(notebook_dir) -> str
"""

from .extract import Page, extract_section, render_notebook, render_section
from .onestore_page import RealPage, extract_pages

__all__ = [
    "Page",
    "RealPage",
    "extract_pages",
    "extract_section",
    "render_notebook",
    "render_section",
]

__version__ = "0.1.0"
