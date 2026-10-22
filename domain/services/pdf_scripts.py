"""PDF primitives (reportlab/platypus) shared by the document builders.

Small neutral building blocks (masthead, section title, key-value table, image,
warning box, side-by-side layout) and ``render_document``. The built-in
Helvetica font covers Latin-1 only: every text passes through ``pdf_text``,
which decomposes accents (NFKD), keeps what Latin-1 can show and replaces the
rest with ``?`` (a documented limitation: text is rendered in Latin-1, which covers
English, Spanish, Portuguese and other Western European languages).
"""

import unicodedata
from io import BytesIO
from typing import Callable, Optional, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import Flowable

PAGE_MARGIN = 16 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN
ACCENT = colors.HexColor("#2f6b3a")
MUTED = colors.HexColor("#5f6b63")
LINE = colors.HexColor("#c9d1cb")
WARNING_BACKGROUND = colors.HexColor("#fff6dd")
WARNING_BORDER = colors.HexColor("#d9a400")

_base = getSampleStyleSheet()
TITLE = ParagraphStyle("Title", parent=_base["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, alignment=TA_LEFT, textColor=ACCENT)
SUBTITLE = ParagraphStyle("Subtitle", parent=_base["Normal"], fontName="Helvetica", fontSize=10, leading=13, textColor=MUTED)
SECTION = ParagraphStyle("Section", parent=_base["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("Body", parent=_base["Normal"], fontName="Helvetica", fontSize=9, leading=12)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=7.5, leading=10, textColor=MUTED)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=9, leading=11)
CELL_LABEL = ParagraphStyle("CellLabel", parent=CELL, fontName="Helvetica-Bold", textColor=MUTED)


def latin1_text(text: object) -> str:
    """Makes a text drawable with the built-in Latin-1 Helvetica font.

    Args:
        text (object): Any value; converted with ``str``.

    Returns:
        str: The text normalized with NFKD and recomposed, keeping the
        characters Latin-1 can show (e.g. accented letters) and replacing every
        other character by ``?``.
    """
    composed = unicodedata.normalize("NFC", unicodedata.normalize("NFKD", str(text)))
    return "".join(char if ord(char) < 256 else "?" for char in composed)


def pdf_text(text: object) -> str:
    """Makes a text safe for Paragraph markup: Latin-1 only (see ``latin1_text``) and XML-escaped.

    Args:
        text (object): Any value; converted with ``str``.

    Returns:
        str: The sanitized, escaped text.
    """
    return escape(latin1_text(text))


def masthead(title: str, subtitle: str, line: str) -> list[Flowable]:
    """Builds the document header.

    Args:
        title (str): Main title.
        subtitle (str): Subtitle (e.g. the area name).
        line (str): Small line below (e.g. the generation date).

    Returns:
        list[Flowable]: Title, subtitle, small line and a rule.
    """
    rule = Table([[""]], colWidths=[CONTENT_WIDTH], rowHeights=[2])
    rule.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.5, ACCENT)]))
    return [Paragraph(pdf_text(title), TITLE), Paragraph(pdf_text(subtitle), SUBTITLE), Paragraph(pdf_text(line), SMALL), Spacer(1, 3), rule]


def section_title(text: str) -> Paragraph:
    """Returns a section heading.

    Args:
        text (str): Heading text.

    Returns:
        Paragraph: The styled heading.
    """
    return Paragraph(pdf_text(text), SECTION)


def paragraph(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    """Returns a body paragraph.

    Args:
        text (str): Text (sanitized here).
        style (ParagraphStyle): Paragraph style.

    Returns:
        Paragraph: The paragraph.
    """
    return Paragraph(pdf_text(text), style)


def key_value_table(rows: Sequence[tuple[str, str]], width: float) -> Table:
    """Builds a two-column label/value table.

    Args:
        rows (Sequence[tuple[str, str]]): (label, value) pairs.
        width (float): Total table width in points.

    Returns:
        Table: The table with light horizontal rules.
    """
    data = [[Paragraph(pdf_text(label), CELL_LABEL), Paragraph(pdf_text(value), CELL)] for label, value in rows]
    table = Table(data, colWidths=[width * 0.48, width * 0.52])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def single_image(content: bytes, width: float) -> Image:
    """Builds an image flowable scaled to a width, keeping the aspect ratio.

    Args:
        content (bytes): Encoded PNG or JPEG image.
        width (float): Width in points.

    Returns:
        Image: The flowable.
    """
    image = Image(BytesIO(content))
    image.drawHeight = width * image.imageHeight / image.imageWidth
    image.drawWidth = width
    return image


def warning_box(text: str, width: float = CONTENT_WIDTH) -> Table:
    """Builds a highlighted box for warnings.

    Args:
        text (str): Warning text.
        width (float): Box width in points.

    Returns:
        Table: A one-cell table with a tinted background and border.
    """
    table = Table([[Paragraph(pdf_text(text), BODY)]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WARNING_BACKGROUND), ("BOX", (0, 0), (-1, -1), 0.8, WARNING_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def side_by_side(left: Flowable, right: Flowable, left_width: float, gap: float = 6 * mm) -> Table:
    """Places two flowables side by side.

    Args:
        left (Flowable): Left flowable (typically an image).
        right (Flowable): Right flowable (typically a table).
        left_width (float): Width of the left column in points.
        gap (float): Space between the columns.

    Returns:
        Table: A borderless two-column layout table.
    """
    table = Table([[left, "", right]], colWidths=[left_width, gap, CONTENT_WIDTH - left_width - gap])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return table


def keep_together(flowables: Sequence[Flowable]) -> KeepTogether:
    """Keeps a section on one page when possible.

    Args:
        flowables (Sequence[Flowable]): Flowables of the section.

    Returns:
        KeepTogether: The wrapped section.
    """
    return KeepTogether(list(flowables))


def render_document(story: Sequence[Flowable], title: str, footer: str = "") -> bytes:
    """Renders a story into an A4 PDF.

    Args:
        story (Sequence[Flowable]): Flowables in order.
        title (str): PDF metadata title.
        footer (str): Text printed in the footer of every page, before the page number.

    Returns:
        bytes: The PDF bytes (start with ``%PDF``).
    """
    buffer = BytesIO()
    footer_text = latin1_text(footer)

    def draw_footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(PAGE_MARGIN, 9 * mm, footer_text)
        canvas.drawRightString(A4[0] - PAGE_MARGIN, 9 * mm, str(document.page))
        canvas.restoreState()

    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN, topMargin=14 * mm, bottomMargin=16 * mm,
        title=title, author="Pasto10",
    )
    document.build(list(story), onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
