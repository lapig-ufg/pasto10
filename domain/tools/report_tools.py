"""Report tools — the PDF bulletin of a registered area.

``generate_area_bulletin`` consolidates the location, the pasture map and
area and the biomass map and totals of a registered area into one shareable
PDF. It reuses the existing services with the defaults of the pasture and
biomass tools (pasture 2022, biomass of the most recent data available for the area): the
statistics come from the per-process cache (no new Earth Engine measurement),
but the maps and basemap tiles are drawn again for every bulletin, which takes
about 10-25 seconds. The tool is read-only with respect to ``session_state``. A section whose data fail is shown as "not
available" (partial result); with nothing available there is no PDF. Every
text, including all labels and notes inside the PDF, comes from
``domain/prompts/tools.yml``.
"""

import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from semente import Context as RunContext
from semente import File, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.schemas.feature import Feature
from domain.services import pdf_scripts
from domain.services.bulletin_scripts import BulletinData, build_bulletin_story
from domain.services.geospatial.basemap import resolve_providers
from domain.services.geospatial.biomass import BiomassPeriodUnavailable, NoPastureBiomass, build_biomass, measure_biomass
from domain.services.geospatial.biomass_image import render_biomass_map
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.image import parse_center, render_buffer_image
from domain.services.geospatial.pasture import YearUnavailable, classify_pasture, measure_pasture_area
from domain.services.geospatial.pasture_image import render_pasture_map
from domain.utils.tool_utils import area_label, latest_area_note, lookup_area, unexpected_error, with_note

_COMPONENT = "report_tools"
_TOOL = "generate_area_bulletin"


def _text(key: str, **values) -> str:
    """Renders one result text of the bulletin tool from ``tools.yml``.

    Args:
        key (str): Result key (``success``, ``pdf_title``...).
        **values: Placeholders of the template.

    Returns:
        str: The rendered text.

    Raises:
        MissingPromptError: If the text is missing.
    """
    return get_tool_result_text(_COMPONENT, _TOOL, key, **values)


PDF_LABEL_KEYS = (
    "pdf_title", "pdf_generated", "pdf_section_location", "pdf_loc_center", "pdf_loc_radius", "pdf_loc_area",
    "pdf_section_pasture", "pdf_past_year", "pdf_past_source", "pdf_past_cultivated", "pdf_past_natural",
    "pdf_past_total", "pdf_past_percent", "pdf_section_biomass", "pdf_bio_period", "pdf_bio_source", "pdf_bio_mean",
    "pdf_bio_total", "pdf_bio_pasture_ha", "pdf_unavailable", "pdf_estimate_warning", "pdf_bio_mask_note",
    "pdf_bio_partial_note", "pdf_section_method", "pdf_method_1", "pdf_method_2", "pdf_method_3", "pdf_method_4",
    "pdf_section_sources", "pdf_source_gpw", "pdf_source_t2g", "pdf_source_basemap", "pdf_source_app", "pdf_footer",
    "pdf_fmt_m", "pdf_fmt_ha", "pdf_fmt_t_ha", "pdf_fmt_t", "pdf_fmt_share", "pdf_fmt_period", "pdf_bullet",
)
_PLACEHOLDERS = (
    "date", "reason", "mask_year", "year", "days", "attribution", "value", "percent", "ha", "label", "start", "end", "text",
)


def _labels() -> dict[str, str]:
    """Collects every ``pdf_*`` label of the tool for the layout.

    Placeholders (``{date}``, ``{reason}``...) are kept literally so the layout
    fills them with the figures of each section.

    Returns:
        dict[str, str]: Label key to text.

    Raises:
        MissingPromptError: If a label is missing in ``tools.yml``.
    """
    keep = {name: "{" + name + "}" for name in _PLACEHOLDERS}
    return {key: _text(key, **keep) for key in PDF_LABEL_KEYS}


def file_slug(name: str) -> str:
    """Turns an area name into a safe ASCII file name part.

    Args:
        name (str): Area display name.

    Returns:
        str: Lowercase letters, digits and hyphens, or ``area`` when nothing is left.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "area"


def _reason(error: Exception) -> str:
    """Maps a section failure to its ``pdf_reason_*`` text (logging unexpected errors)."""
    if isinstance(error, (YearUnavailable, BiomassPeriodUnavailable)):
        return _text("pdf_reason_year")
    if isinstance(error, NoPastureBiomass):
        return _text("pdf_reason_no_pasture")
    if isinstance(error, EarthEngineUnavailable):
        return _text("pdf_reason_ee")
    log_warning(f"{_TOOL}: section failed: {type(error).__name__}: {error}")
    return _text("pdf_reason_error")


def _pasture_section(feature: Feature):
    """Computes the pasture stats and map with the defaults of ``generate_pasture_map`` (cached)."""
    classification, roi = classify_pasture(feature, None)
    stats = measure_pasture_area(classification, roi)
    return stats, render_pasture_map(feature, classification.image, stats, classification.scale).content


def _biomass_section(feature: Feature):
    """Computes the biomass stats and map with the defaults of ``generate_biomass_map`` (the most recent data for the area, cached)."""
    biomass, roi = build_biomass(feature, None, None)
    stats = measure_biomass(biomass, roi)
    return stats, render_biomass_map(feature, biomass.image, stats).content


@tool(description=get_tool_description(_COMPONENT, _TOOL))
def generate_area_bulletin(run_context: RunContext, area_name: Optional[str] = None):
    """Generates the PDF bulletin of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.

    Returns:
        ToolResult: The ``success`` text (or ``partial`` when a section is
        unavailable) with the PDF file attached; or ``no_registered_area``,
        ``area_not_found``, ``no_data``, ``earth_engine_unavailable`` or
        ``error`` instructions (no file).

    Side effects:
        None on ``session_state``. Reads cached results or computes them
        through the pasture and biomass services (Earth Engine) and downloads
        basemap tiles.
    """
    log_debug(f"{_TOOL}: area_name={area_name!r}")
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, _TOOL)
        if failure is not None:
            return failure
        label = area_label(feature)
        sections, reasons, errors = {}, {}, []
        for name, build in (("pasture", _pasture_section), ("biomass", _biomass_section)):
            try:
                sections[name] = build(feature)
            except Exception as error:
                errors.append(error)
                reasons[name] = _reason(error)
        if not sections:
            if any(isinstance(error, EarthEngineUnavailable) for error in errors):
                return ToolResult(content=_text("earth_engine_unavailable"))
            return ToolResult(content=_text("no_data", area_name=label))

        latitude, longitude = parse_center(feature)
        location = render_buffer_image(feature)
        attribution = "; ".join(provider.attribution.replace("\n", " ") for provider in resolve_providers()) if location.format == "jpeg" else ""
        pasture = sections.get("pasture")
        biomass = sections.get("biomass")
        data = BulletinData(
            area_name=label, generated_on=datetime.now(timezone.utc).strftime("%Y-%m-%d"), latitude=latitude,
            longitude=longitude, radius_m=float(feature.get_metadata("radius", 0)), area_ha=feature.total_area,
            location_image=location.content, pasture=pasture[0] if pasture else None,
            pasture_image=pasture[1] if pasture else None, pasture_reason=reasons.get("pasture", ""),
            biomass=biomass[0] if biomass else None, biomass_image=biomass[1] if biomass else None,
            biomass_reason=reasons.get("biomass", ""), attribution=attribution,
        )
        story = build_bulletin_story(data, _labels())
        pdf_bytes = pdf_scripts.render_document(story, f"{_text('pdf_title')} - {label}", _text("pdf_footer"))

        name = f"bulletin_{file_slug(label)}.pdf"
        summary = []
        if pasture:
            summary.append(_text("summary_pasture", year=pasture[0].year, pasture_ha=pasture[0].pasture_ha, area_ha=pasture[0].area_ha))
        if biomass:
            summary.append(_text("summary_biomass", period=biomass[0].period_label, source=biomass[0].source.value, mean_t_ha=biomass[0].mean_t_ha, total_t=biomass[0].total_t))
        missing = [key for key in ("pasture", "biomass") if key not in sections]
        if missing:
            content = _text("partial", area_name=label, file_name=name, summary="\n".join(summary), missing=", ".join(missing))
        else:
            content = _text("success", area_name=label, file_name=name, summary="\n".join(summary))
        file = File(content=pdf_bytes, name=name, mime_type="application/pdf", format="pdf")
        return ToolResult(content=with_note(content, latest_area_note(run_context, area_name, feature)), files=[file])
    except Exception:
        return unexpected_error(_COMPONENT, _TOOL)
