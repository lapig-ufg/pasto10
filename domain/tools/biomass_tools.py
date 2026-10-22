"""Biomass tools — dry-matter productivity map and totals of a registered area.

Both tools are read-only with respect to ``session_state``: they look up a
registered area (by name, or the latest one), delegate the Earth Engine work
to ``domain/services/geospatial/biomass.py`` and return texts from
``domain/prompts/tools.yml``. Known failures (unknown area, nothing
registered, period without data, no productive pasture, Earth Engine
unavailable) get dedicated texts; anything else is logged in full and the
model only gets a generic message.
"""

from typing import Optional

from semente import Context as RunContext
from semente import Image, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import Feature
from domain.services.geospatial.biomass import (
    GPP_FIRST_YEAR,
    GPP_LAST_YEAR,
    BiomassPeriodUnavailable,
    NoPastureBiomass,
    build_biomass,
    get_biomass_total as measure_total,
    measure_biomass,
)
from domain.services.geospatial.biomass_image import render_biomass_map
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.utils.tool_utils import area_label, lookup_area, unexpected_error

_COMPONENT = "biomass_tools"


def _success_text(tool_key: str, feature: Feature, stats: BiomassStats) -> str:
    """Renders the ``success`` text and the conditional notes for a measured area.

    Adds ``note_mask_year`` when the pasture mask is older than the period,
    ``note_time2graze`` when the source is Time2Graze (short, partial coverage)
    and ``note_partial_year`` when a whole year was requested but only part of
    it is covered.
    """
    text = get_tool_result_text(
        _COMPONENT, tool_key, "success",
        area_name=area_label(feature), period=stats.period_label, source=stats.source.value,
        start=stats.start, end=stats.end, days=stats.days_covered, mean_t_ha=stats.mean_t_ha,
        total_t=stats.total_t, pasture_ha=stats.pasture_ha, area_ha=stats.area_ha,
        pasture_percent=stats.pasture_percent, scale_m=stats.scale_m,
    )
    notes = []
    if stats.mask_year < stats.year:
        notes.append(get_tool_result_text(_COMPONENT, tool_key, "note_mask_year", mask_year=stats.mask_year, year=stats.year))
    if stats.source is BiomassSource.TIME2GRAZE:
        notes.append(get_tool_result_text(
            _COMPONENT, tool_key, "note_time2graze", n_images=stats.n_images, partial=stats.partial,
            start=stats.start, end=stats.end, days=stats.days_covered,
        ))
        if stats.partial and len(stats.period_label) == 4:
            notes.append(get_tool_result_text(
                _COMPONENT, tool_key, "note_partial_year", year=stats.year, start=stats.start, end=stats.end,
                days=stats.days_covered,
            ))
    return "\n".join([text, *notes])


def _handle_known_errors(error: Exception, tool_key: str, feature: Optional[Feature]) -> ToolResult:
    """Maps a failure to its tool result; must be called from an ``except`` block.

    Args:
        error (Exception): The exception being handled.
        tool_key (str): Tool name whose ``results`` texts are rendered.
        feature (Feature, optional): The analyzed area, for texts that name it.

    Returns:
        ToolResult: ``year_unavailable``, ``month_unavailable``, ``no_pasture``,
        ``earth_engine_unavailable`` or the generic ``error`` text.
    """
    if isinstance(error, BiomassPeriodUnavailable):
        log_warning(f"{tool_key}: {error}")
        if error.kind == "month":
            return ToolResult(content=get_tool_result_text(
                _COMPONENT, tool_key, "month_unavailable", t2g_start=error.first, t2g_end=error.last,
            ))
        return ToolResult(content=get_tool_result_text(
            _COMPONENT, tool_key, "year_unavailable", first_year=GPP_FIRST_YEAR, latest_year=GPP_LAST_YEAR,
        ))
    if isinstance(error, NoPastureBiomass):
        log_warning(f"{tool_key}: no productive pasture in the area")
        return ToolResult(content=get_tool_result_text(
            _COMPONENT, tool_key, "no_pasture", area_name=area_label(feature) if feature else "",
        ))
    if isinstance(error, EarthEngineUnavailable):
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "earth_engine_unavailable"))
    return unexpected_error(_COMPONENT, tool_key)


@tool(description=get_tool_description(_COMPONENT, "generate_biomass_map"))
def generate_biomass_map(
    run_context: RunContext, area_name: Optional[str] = None, year: Optional[int] = None, month: Optional[int] = None,
):
    """Generates the dry-matter production map of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.
        year (int, optional): Reference year; the latest Global Pasture Watch year when omitted.
        month (int, optional): Month (1-12) of recent Time2Graze data.

    Returns:
        ToolResult: The ``success`` text (period, source, mean and total) plus
        the map image (JPEG or PNG); or the ``no_registered_area``,
        ``area_not_found``, ``year_unavailable``, ``month_unavailable``,
        ``no_pasture``, ``earth_engine_unavailable`` or ``error`` instructions (no image).

    Side effects:
        None on ``session_state``. Queries Earth Engine and downloads tiles.
    """
    tool_key = "generate_biomass_map"
    log_debug(f"{tool_key}: area_name={area_name!r}, year={year}, month={month}")
    feature = None
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        biomass, roi = build_biomass(feature, year, month)
        stats = measure_biomass(biomass, roi)
        rendered = render_biomass_map(feature, biomass.image, stats)
        image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)
        return ToolResult(content=_success_text(tool_key, feature, stats), images=[image])
    except Exception as error:
        return _handle_known_errors(error, tool_key, feature)


@tool(description=get_tool_description(_COMPONENT, "get_biomass_total"))
def get_biomass_total(
    run_context: RunContext, area_name: Optional[str] = None, year: Optional[int] = None, month: Optional[int] = None,
):
    """Measures the mean and total dry-matter production of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.
        year (int, optional): Reference year; the latest Global Pasture Watch year when omitted.
        month (int, optional): Month (1-12) of recent Time2Graze data.

    Returns:
        ToolResult: The ``success`` text (period, source, mean t DM/ha and total
        t DM); or the ``no_registered_area``, ``area_not_found``,
        ``year_unavailable``, ``month_unavailable``, ``no_pasture``,
        ``earth_engine_unavailable`` or ``error`` instructions.

    Side effects:
        None on ``session_state``. Queries Earth Engine.
    """
    tool_key = "get_biomass_total"
    log_debug(f"{tool_key}: area_name={area_name!r}, year={year}, month={month}")
    feature = None
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        return ToolResult(content=_success_text(tool_key, feature, measure_total(feature, year, month)))
    except Exception as error:
        return _handle_known_errors(error, tool_key, feature)
