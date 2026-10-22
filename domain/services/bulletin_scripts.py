"""Area bulletin layout — builds the story of the PDF from already computed results.

Pure layout: no Earth Engine, no network. The caller passes the figures, the
rendered images and every label (all texts come from ``tools.yml``,
``report_tools.generate_area_bulletin.results.pdf_*``), so nothing is
recalculated here. A section whose data failed shows the "not available" text
with its reason instead.
"""

from dataclasses import dataclass, field
from typing import Optional

from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Spacer

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services import pdf_scripts as pdf

IMAGE_WIDTH = 78 * mm


@dataclass(frozen=True)
class BulletinData:
    """Everything the bulletin shows.

    Attributes:
        area_name (str): Display name of the registered area.
        generated_on (str): UTC generation date (ISO).
        latitude (float): Center latitude.
        longitude (float): Center longitude.
        radius_m (float): Buffer radius in meters.
        area_ha (float): Area in hectares.
        location_image (bytes): Encoded location map.
        pasture (PastureAreaStats, optional): Pasture figures, None when unavailable.
        pasture_image (bytes, optional): Encoded pasture map.
        pasture_reason (str, optional): Why the pasture section is unavailable.
        biomass (BiomassStats, optional): Biomass figures, None when unavailable.
        biomass_image (bytes, optional): Encoded biomass map.
        biomass_reason (str, optional): Why the biomass section is unavailable.
        attribution (str): Basemap attribution text (empty when no basemap was used).
        extra_sources (list[str]): Additional source lines.
    """

    area_name: str
    generated_on: str
    latitude: float
    longitude: float
    radius_m: float
    area_ha: float
    location_image: bytes
    pasture: Optional[PastureAreaStats] = None
    pasture_image: Optional[bytes] = None
    pasture_reason: str = ""
    biomass: Optional[BiomassStats] = None
    biomass_image: Optional[bytes] = None
    biomass_reason: str = ""
    attribution: str = ""
    extra_sources: list[str] = field(default_factory=list)


def _section(title: str, image: Optional[bytes], rows: list[tuple[str, str]], unavailable: str) -> list[Flowable]:
    """Builds one section: title plus image and table side by side, or the unavailable text."""
    parts: list[Flowable] = [pdf.section_title(title)]
    if image is None:
        parts.append(pdf.warning_box(unavailable))
    else:
        table_width = pdf.CONTENT_WIDTH - IMAGE_WIDTH - 6 * mm
        parts.append(pdf.side_by_side(pdf.single_image(image, IMAGE_WIDTH), pdf.key_value_table(rows, table_width), IMAGE_WIDTH))
    return [pdf.keep_together(parts)]


def build_bulletin_story(data: BulletinData, labels: dict[str, str]) -> list[Flowable]:
    """Builds the flowables of the area bulletin.

    Args:
        data (BulletinData): Figures, images and reasons to show.
        labels (dict[str, str]): The ``pdf_*`` texts of ``tools.yml`` (titles,
            row labels, notes, method and source lines, footer).

    Returns:
        list[Flowable]: The story for ``pdf_scripts.render_document``.

    Raises:
        KeyError: If a label is missing.
    """
    story: list[Flowable] = pdf.masthead(labels["pdf_title"], data.area_name, labels["pdf_generated"].format(date=data.generated_on))
    story.append(Spacer(1, 4))

    story += _section(labels["pdf_section_location"], data.location_image, [
        (labels["pdf_loc_center"], f"{data.latitude:.5f}, {data.longitude:.5f}"),
        (labels["pdf_loc_radius"], labels["pdf_fmt_m"].format(value=f"{data.radius_m:g}")),
        (labels["pdf_loc_area"], labels["pdf_fmt_ha"].format(value=f"{data.area_ha:g}")),
    ], "")

    def ha(value: float) -> str:
        return labels["pdf_fmt_ha"].format(value=f"{value:g}")

    pasture_rows = []
    if data.pasture is not None:
        pasture_stats = data.pasture
        pasture_rows = [
            (labels["pdf_past_year"], str(pasture_stats.year)),
            (labels["pdf_past_source"], pasture_stats.source.value),
            (labels["pdf_past_cultivated"], ha(pasture_stats.cultivated_ha)),
            (labels["pdf_past_natural"], ha(pasture_stats.natural_ha)),
            (labels["pdf_past_total"], ha(pasture_stats.pasture_ha)),
            (labels["pdf_past_percent"], labels["pdf_fmt_share"].format(
                percent=f"{pasture_stats.pasture_percent:g}", ha=f"{pasture_stats.area_ha:g}")),
        ]
    story += _section(
        labels["pdf_section_pasture"], data.pasture_image if data.pasture is not None else None, pasture_rows,
        labels["pdf_unavailable"].format(reason=data.pasture_reason),
    )
    if data.pasture is not None and data.pasture.source is PastureSource.CLASSIFICATION:
        story.append(pdf.warning_box(labels["pdf_estimate_warning"]))

    biomass_rows = []
    bio_stats = data.biomass
    if bio_stats is not None:
        biomass_rows = [
            (labels["pdf_bio_period"], labels["pdf_fmt_period"].format(
                label=bio_stats.period_label, start=bio_stats.start, end=bio_stats.end, days=bio_stats.days_covered)),
            (labels["pdf_bio_source"], bio_stats.source.value),
            (labels["pdf_bio_mean"], labels["pdf_fmt_t_ha"].format(value=f"{bio_stats.mean_t_ha:g}")),
            (labels["pdf_bio_total"], labels["pdf_fmt_t"].format(value=f"{bio_stats.total_t:g}")),
            (labels["pdf_bio_pasture_ha"], labels["pdf_fmt_share"].format(
                percent=f"{bio_stats.pasture_percent:g}", ha=f"{bio_stats.pasture_ha:g}")),
        ]
    story += _section(
        labels["pdf_section_biomass"], data.biomass_image if bio_stats is not None else None, biomass_rows,
        labels["pdf_unavailable"].format(reason=data.biomass_reason),
    )
    if bio_stats is not None:
        if bio_stats.mask_year < bio_stats.year:
            story.append(pdf.paragraph(
                labels["pdf_bio_mask_note"].format(mask_year=bio_stats.mask_year, year=bio_stats.year), pdf.SMALL))
        if bio_stats.source is BiomassSource.TIME2GRAZE and bio_stats.partial:
            story.append(pdf.paragraph(labels["pdf_bio_partial_note"].format(days=bio_stats.days_covered), pdf.SMALL))

    story.append(pdf.keep_together([
        pdf.section_title(labels["pdf_section_method"]),
        *[pdf.paragraph(labels["pdf_bullet"].format(text=labels[key]), pdf.BODY) for key in sorted(k for k in labels if k.startswith("pdf_method_"))],
    ]))
    sources = [labels["pdf_source_gpw"]]
    if bio_stats is not None and bio_stats.source is BiomassSource.TIME2GRAZE:
        sources.append(labels["pdf_source_t2g"])
    if data.attribution:
        sources.append(labels["pdf_source_basemap"].format(attribution=data.attribution.replace("\n", " ")))
    sources += data.extra_sources + [labels["pdf_source_app"]]
    story.append(pdf.keep_together([
        pdf.section_title(labels["pdf_section_sources"]), *[pdf.paragraph(labels["pdf_bullet"].format(text=line), pdf.SMALL) for line in sources],
    ]))
    return story
