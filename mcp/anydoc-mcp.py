#!/usr/bin/env python
"""
anydoc MCP Server — Document to Markdown conversion for Bearden/Onyx stack.

Exposes:
  - convert_document(file_path) -> str  : Convert PDF/Word/Excel/etc. to Markdown
  - convert_with_ocr(file_path) -> str  : OCR scanned PDFs first, then convert

Runs on the gateway box (CPU-only) under the Hermes venv python
(<hermes-home>/hermes-agent/venv), which has
the `mcp` SDK and an editable install of chandra (<path-to-chandra-clone>).
The anydoc Rust extension (firecrawl-anydoc) lives in Python 3.14's site-packages
and is abi3, so it is imported via the sys.path insert below.

For scanned PDFs, uses the shared grm_ocr module (chandra page loading +
prompts, direct streaming client) to call GRM (grm-2.6-plus-0628) on the
forge 5090 vLLM server with thinking disabled.
"""

import sys
from pathlib import Path

# anydoc (Rust extension, abi3) is installed under Python 3.14's site-packages
_ANYDOC_SITE = Path("C:/path/to/python/Lib/site-packages")
if _ANYDOC_SITE.exists() and str(_ANYDOC_SITE) not in sys.path:
    sys.path.insert(0, str(_ANYDOC_SITE))

import anydoc

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
import grm_ocr  # shared GRM client: thinking disabled, fence/body normalization

mcp = FastMCP("anydoc-mcp")


def _load(file_path: str) -> bytes:
    p = Path(file_path)
    if not p.is_absolute():
        raise ValueError(f"Path must be absolute: {file_path}")
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return p.read_bytes()


def _to_markdown(data: bytes, file_path: str) -> str:
    fmt = anydoc.format_from_path(str(file_path))
    if fmt is None:
        fmt = anydoc.format_from_bytes(data)
    if fmt is None:
        raise ValueError(f"Unrecognized format: {file_path}")
    md = anydoc.to_markdown_bytes(data, format=fmt)
    return md if isinstance(md, str) else md.decode("utf-8")


@mcp.tool()
def convert_document(file_path: str) -> str:
    """Convert any supported document (PDF, Word, Excel, PowerPoint, ODF, RTF,
    EPUB, CSV) to GitHub-Flavored Markdown.

    Args:
        file_path: Absolute path to the document file.

    Returns:
        Markdown content as a string. Raises if the PDF is scanned
        (image-only) — use convert_with_ocr for those.
    """
    data = _load(file_path)
    return _to_markdown(data, file_path)


@mcp.tool()
def convert_with_ocr(file_path: str) -> str:
    """Convert a document to Markdown, with OCR fallback for scanned PDFs.

    Text-based files convert locally via anydoc. Scanned PDFs are OCR'd by
    the Chandra pipeline using GRM on the forge 5090, which returns Markdown
    directly.

    Args:
        file_path: Absolute path to the document file.

    Returns:
        Markdown content as a string.
    """
    data = _load(file_path)
    try:
        return _to_markdown(data, file_path)
    except (anydoc.ConvertError, ValueError) as e:
        if "OCR" not in str(e) and "Unrecognized" not in str(e):
            raise
        # Fall through to GRM OCR for scanned/image PDFs
    from chandra.input import load_file

    images = load_file(str(file_path), {})
    pages = [grm_ocr.ocr_page_markdown(img) for img in images]
    return "\n\n".join(pages)


if __name__ == "__main__":
    mcp.run()
