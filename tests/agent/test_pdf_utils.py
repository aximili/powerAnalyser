"""Tests for the PDF helpers used by the Manual tab.

A tiny in-memory PDF is generated with PyMuPDF itself (no network, no fixture
file), so the suite stays fully offline.  If PyMuPDF is not installed the
rendering tests are skipped and only the pure sniffing logic is exercised.
"""

from __future__ import annotations

import io

import pytest

from power_analyser.agent.extractors import pdf_utils


# ── Pure logic (no engine required) ───────────────────────────────────────────


def test_is_pdf_detects_magic_header():
    assert pdf_utils.is_pdf(b"%PDF-1.4\n...") is True


def test_is_pdf_rejects_non_pdf():
    assert pdf_utils.is_pdf(b"\x89PNG\r\n\x1a\n") is False
    assert pdf_utils.is_pdf(b"") is False


def test_friendly_pdf_error_without_fitz(monkeypatch):
    monkeypatch.setattr(pdf_utils, "fitz", None)
    msg = pdf_utils.friendly_pdf_error(RuntimeError("boom"))
    assert "PyMuPDF" in msg


# ── Rendering / text extraction (require PyMuPDF) ─────────────────────────────

pytestmark_engine = pytest.mark.skipif(
    pdf_utils.fitz is None, reason="PyMuPDF (fitz) not installed"
)


def _make_rate_pdf(text_lines: list[str]):
    """Build a small text-only PDF with PyMuPDF; returns its bytes."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "\n".join(text_lines),
        fontsize=12,
    )
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest.mark.skipif(pdf_utils.fitz is None, reason="PyMuPDF (fitz) not installed")
def test_render_pdf_to_png_returns_png():
    pdf_bytes = _make_rate_pdf(["Daily Supply Charge: 98c/day", "Usage: 28c/kWh"])
    png = pdf_utils.render_pdf_to_png(pdf_bytes)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG signature


@pytest.mark.skipif(pdf_utils.fitz is None, reason="PyMuPDF (fitz) not installed")
def test_extract_pdf_text_returns_embedded_text():
    pdf_bytes = _make_rate_pdf(["Daily Supply Charge: 98c/day", "Usage: 28c/kWh"])
    text = pdf_utils.extract_pdf_text(pdf_bytes)

    assert "98c/day" in text
    assert "28c/kWh" in text


@pytest.mark.skipif(pdf_utils.fitz is None, reason="PyMuPDF (fitz) not installed")
def test_pdf_to_image_and_text_returns_both():
    pdf_bytes = _make_rate_pdf(["Solar FiT: 5c/kWh"])
    png, text = pdf_utils.pdf_to_image_and_text(pdf_bytes)

    assert png[:4] == b"\x89PNG"
    assert "5c/kWh" in text


@pytest.mark.skipif(pdf_utils.fitz is None, reason="PyMuPDF (fitz) not installed")
def test_render_multipage_pdf_stitches_into_single_image():
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for n in range(3):
        doc.new_page().insert_text((72, 72), f"PAGE {n}", fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    png = pdf_utils.render_pdf_to_png(buf.getvalue())
    assert png[:4] == b"\x89PNG"

    from PIL import Image

    img = Image.open(io.BytesIO(png))
    # A stitched image is taller than wide for portrait pages stacked vertically.
    assert img.height > img.width
