"""Tests of the PDF primitives (domain/services/pdf_scripts.py)."""

from io import BytesIO

from PIL import Image
from pypdf import PdfReader
from reportlab.platypus import Spacer

from domain.services import pdf_scripts as pdf


def _png(color=(40, 120, 60), size=(200, 100)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _text(document: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(document)).pages)


def test_latin1_text_keeps_accents_and_replaces_the_rest():
    assert pdf.latin1_text("Pastagem São José") == "Pastagem São José"
    assert pdf.latin1_text("Ñandú Pingüino Cañón ç") == "Ñandú Pingüino Cañón ç"
    assert pdf.latin1_text("area \u2713") == "area ?"
    assert pdf.latin1_text("é") == "é"


def test_pdf_text_escapes_markup():
    assert pdf.pdf_text("A & B <i>") == "A &amp; B &lt;i&gt;"


def test_render_document_builds_a_pdf_with_every_primitive():
    story = [
        *pdf.masthead("Title here", "Subtitle here", "Small line"),
        pdf.section_title("A section"),
        pdf.side_by_side(pdf.single_image(_png(), 200), pdf.key_value_table([("Label", "Value 12.5 ha")], 250), 200),
        pdf.warning_box("Careful: estimate"),
        Spacer(1, 4),
        pdf.paragraph("Body text"),
    ]

    document = pdf.render_document(story, "Doc", "Footer text")

    assert document.startswith(b"%PDF")
    text = _text(document)
    for expected in ("Title here", "Subtitle here", "A section", "Value 12.5 ha", "Careful: estimate", "Body text", "Footer text"):
        assert expected in text
    assert PdfReader(BytesIO(document)).metadata.title == "Doc"


def test_single_image_keeps_the_aspect_ratio():
    image = pdf.single_image(_png(size=(300, 150)), 100)

    assert image.drawWidth == 100 and image.drawHeight == 50
