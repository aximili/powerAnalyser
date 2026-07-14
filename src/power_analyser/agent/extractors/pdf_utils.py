"""PDF handling for the Manual tab.

Retailers frequently publish their rate sheets as PDFs.  These helpers turn a
PDF into the two things the extractor needs:

  * a single PNG image (all rendered pages stitched top-to-bottom) that can be
    fed to a vision-capable LLM via ``complete_with_image``;
  * the extracted plain text, embedded in the prompt so text-only models (or
    text-based rate PDFs) work without OCR.

PyMuPDF (import name ``fitz``) is the rendering/text engine.  It is imported
lazily so that a missing install degrades into a clear error message instead of
crashing the GUI on startup.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Hard caps so a giant rate booklet cannot blow up tokens / memory.
_MAX_RENDER_PAGES = 8
_MAX_TEXT_PAGES = 12
_MAX_TEXT_CHARS = 12_000
_RENDER_DPI = 150
_PAGE_GAP_PX = 12  # white gutter between stitched pages
_MAX_STITCHED_WIDTH = 2000  # downscale very wide pages before stitching

# PyMuPDF is optional at import time; surfaced as a friendly error on use.
try:  # pragma: no cover - exercised only when the wheel is present
    import fitz  # type: ignore
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore


def is_pdf(data: bytes) -> bool:
    """True if *data* looks like a PDF (magic ``%PDF-`` header)."""
    return bool(data) and data[:5] == b"%PDF-"


def _require_fitz() -> None:
    if fitz is None:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "PyMuPDF is not installed (pip install PyMuPDF). "
            "It is required to read PDF rate sheets."
        )


def extract_pdf_text(data: bytes) -> str:
    """Return the concatenated plain text of a PDF (capped for token safety)."""
    _require_fitz()
    doc = fitz.open(stream=data, filetype="pdf")  # type: ignore[union-attr]
    try:
        return _extract_text_from_doc(doc)
    finally:
        doc.close()


def render_pdf_to_png(data: bytes) -> bytes:
    """Render a PDF to a single stitched PNG (pages stacked vertically)."""
    _require_fitz()
    doc = fitz.open(stream=data, filetype="pdf")  # type: ignore[union-attr]
    try:
        return _render_doc_to_png(doc)
    finally:
        doc.close()


def pdf_to_image_and_text(data: bytes) -> tuple[bytes, str]:
    """Render a PDF to a stitched PNG and extract its text in one pass.

    Returns ``(png_bytes, text)``.  Text may be empty for scanned/image-only
    PDFs; the rendered image still carries the information in that case.
    """
    _require_fitz()
    doc = fitz.open(stream=data, filetype="pdf")  # type: ignore[union-attr]
    try:
        text = _extract_text_from_doc(doc)
        png = _render_doc_to_png(doc)
        return png, text
    finally:
        doc.close()


# ── Internal helpers operating on an open fitz document ──────────────────────


def _extract_text_from_doc(doc) -> str:
    chunks: list[str] = []
    total = 0
    for i in range(min(len(doc), _MAX_TEXT_PAGES)):
        text = doc[i].get_text() or ""
        if total + len(text) >= _MAX_TEXT_CHARS:
            chunks.append(text[: _MAX_TEXT_CHARS - total])
            break
        chunks.append(text)
        total += len(text)
    return "\n".join(chunks).strip()


def _render_doc_to_png(doc) -> bytes:
    from PIL import Image  # Pillow is a core dependency

    page_images: list[Image.Image] = []
    for i in range(min(len(doc), _MAX_RENDER_PAGES)):
        pix = doc[i].get_pixmap(dpi=_RENDER_DPI)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        page_images.append(img)

    if not page_images:
        raise ValueError("The PDF contains no renderable pages.")

    if len(page_images) == 1:
        out = page_images[0]
    else:
        # Downscale any overly-wide page before stitching.
        scaled = []
        for img in page_images:
            if img.width > _MAX_STITCHED_WIDTH:
                ratio = _MAX_STITCHED_WIDTH / img.width
                img = img.resize(
                    (_MAX_STITCHED_WIDTH, max(1, int(img.height * ratio))),
                    Image.LANCZOS,
                )
            scaled.append(img)
        width = max(im.width for im in scaled)
        height = sum(im.height for im in scaled) + _PAGE_GAP_PX * (len(scaled) - 1)
        canvas = Image.new("RGB", (width, height), "white")
        y = 0
        for im in scaled:
            canvas.paste(im, (0, y))
            y += im.height + _PAGE_GAP_PX
        out = canvas

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def friendly_pdf_error(exc: Exception) -> str:
    """Turn a PDF-processing failure into a user-facing message."""
    if fitz is None:  # pragma: no cover
        return "PyMuPDF is not installed (pip install PyMuPDF) — needed to read PDFs."
    return f"Could not read the PDF: {exc}"
