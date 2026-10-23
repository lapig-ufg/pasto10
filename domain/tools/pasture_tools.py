"""Pasture tools — pasture map and pasture area of a registered area.

Both tools are read-only with respect to ``session_state``: they look up a
registered area (by name, or the latest one), delegate the Earth Engine work
to ``domain/services/geospatial/pasture.py`` and return texts from
``domain/prompts/tools.yml``. Known failures (unknown area, nothing
registered, year without data, Earth Engine unavailable) get dedicated texts;
anything else is logged in full and the model only gets a generic message.
"""

from typing import Optional

from semente import Context as RunContext
from semente import Image, ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.schemas.feature import Feature
from domain.schemas.pasture import PastureAreaStats
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.pasture import (
    GPW_FIRST_YEAR,
    YearUnavailable,
    classify_pasture,
    get_pasture_area as measure_area,
    measure_pasture_area,
)
from domain.services.geospatial.pasture_image import render_pasture_map
from domain.utils.tool_utils import area_label, area_notes, latest_area_note, lookup_area, unexpected_error, with_note

_COMPONENT = "pasture_tools"


def _success_text(tool_key: str, feature: Feature, stats: PastureAreaStats) -> str:
    """Renders the ``success`` text with the measured figures of an area (hectares with one decimal) and its area notes."""
    text = get_tool_result_text(
        _COMPONENT, tool_key, "success",
        area_name=area_label(feature), year=stats.year, source=stats.source.value,
        pasture_ha=f"{stats.pasture_ha:.1f}", cultivated_ha=f"{stats.cultivated_ha:.1f}", natural_ha=f"{stats.natural_ha:.1f}",
        area_ha=f"{stats.area_ha:.1f}", pasture_percent=stats.pasture_percent,
    )
    return "\n".join([text, *area_notes(_COMPONENT, tool_key, feature, stats.area_ha)])


def _handle_known_errors(error: Exception, tool_key: str) -> ToolResult:
    """Maps a failure to its tool result; must be called from an ``except`` block.

    Args:
        error (Exception): The exception being handled.
        tool_key (str): Tool name whose ``results`` texts are rendered.

    Returns:
        ToolResult: ``year_unavailable`` (with the earliest/latest years),
        ``earth_engine_unavailable`` or the generic ``error`` text.
    """
    if isinstance(error, YearUnavailable):
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(
            _COMPONENT, tool_key, "year_unavailable", first_year=GPW_FIRST_YEAR, latest_year=error.latest,
        ))
    if isinstance(error, EarthEngineUnavailable):
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "earth_engine_unavailable"))
    return unexpected_error(_COMPONENT, tool_key)


@tool(description=get_tool_description(_COMPONENT, "generate_pasture_map"))
def generate_pasture_map(run_context: RunContext, area_name: Optional[str] = None, year: Optional[int] = None):
    """Generates the pasture map image of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.
        year (int, optional): Reference year; 2022, the latest Global Pasture Watch map, when omitted. Later years are experimental estimates.

    Returns:
        ToolResult: The ``success`` text (year, source, areas) plus the map
        image (JPEG or PNG); or the ``no_registered_area``, ``area_not_found``,
        ``year_unavailable``, ``earth_engine_unavailable`` or ``error``
        instructions (no image).

    Side effects:
        None on ``session_state``. Queries Earth Engine and downloads tiles.
    """
    tool_key = "generate_pasture_map"
    log_debug(f"{tool_key}: area_name={area_name!r}, year={year}")
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        classification, roi = classify_pasture(feature, year)
        stats = measure_pasture_area(classification, roi)
        rendered = render_pasture_map(feature, classification.image, stats, classification.scale)
        image = Image(content=rendered.content, format=rendered.format, mime_type=rendered.mime_type)
        return ToolResult(content=with_note(_success_text(tool_key, feature, stats), latest_area_note(run_context, area_name, feature)), images=[image])
    except Exception as error:
        return _handle_known_errors(error, tool_key)


@tool(description=get_tool_description(_COMPONENT, "get_pasture_area"))
def get_pasture_area(run_context: RunContext, area_name: Optional[str] = None, year: Optional[int] = None):
    """Measures the pasture area (hectares) inside a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.
        year (int, optional): Reference year; 2022, the latest Global Pasture Watch map, when omitted. Later years are experimental estimates.

    Returns:
        ToolResult: The ``success`` text (year, source, cultivated, natural and
        total pasture in hectares); or the ``no_registered_area``,
        ``area_not_found``, ``year_unavailable``, ``earth_engine_unavailable``
        or ``error`` instructions.

    Side effects:
        None on ``session_state``. Queries Earth Engine.
    """
    tool_key = "get_pasture_area"
    log_debug(f"{tool_key}: area_name={area_name!r}, year={year}")
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        stats = measure_area(feature, year)
        return ToolResult(content=with_note(_success_text(tool_key, feature, stats), latest_area_note(run_context, area_name, feature)))
    except Exception as error:
        return _handle_known_errors(error, tool_key)
