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
from semente import Image, ToolResult, Video, tool
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
from domain.services.geospatial.biomass_video import (
    VideoDeadlineExceeded,
    VideoEncoderUnavailable,
    VideoEncodingFailed,
    generate_biomass_video as render_video,
)
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.utils.tool_utils import area_label, area_notes, latest_area_note, lookup_area, unexpected_error, with_note

_COMPONENT = "biomass_tools"


def _success_text(tool_key: str, feature: Feature, stats: BiomassStats, latest: bool = False) -> str:
    """Renders the ``success`` text and the conditional notes for a measured area.

    ``latest`` is True when the user gave neither year nor month, so the
    service chose the most recent data for the area: ``note_latest`` is added
    for Time2Graze and ``note_latest_annual`` when the area has no Time2Graze
    images and the latest annual year was used instead. Adds
    ``note_mask_year`` when the pasture mask is older than the period,
    ``note_time2graze`` when the source is Time2Graze (short, partial coverage)
    and ``note_partial_year`` when a whole year was requested but only part of
    it is covered.
    """
    text = get_tool_result_text(
        _COMPONENT, tool_key, "success",
        area_name=area_label(feature), period=stats.period_label, source=stats.source.value,
        start=stats.start, end=stats.end, days=stats.days_covered, mean_t_ha=stats.mean_t_ha,
        total_t=stats.total_t, pasture_ha=f"{stats.pasture_ha:.1f}", area_ha=f"{stats.area_ha:.1f}",
        pasture_percent=stats.pasture_percent, scale_m=stats.scale_m,
    )
    notes = area_notes(_COMPONENT, tool_key, feature, stats.area_ha)
    if latest:
        key = "note_latest" if stats.source is BiomassSource.TIME2GRAZE else "note_latest_annual"
        notes.append(get_tool_result_text(_COMPONENT, tool_key, key, period=stats.period_label, source=stats.source.value))
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
        year (int, optional): Reference year; with no year and no month, the most recent data available for the area.
        month (int, optional): Month (1-12) of recent Time2Graze data.

    Returns:
        ToolResult: The ``success`` text (period, source, mean and total) plus
        the map image (JPEG or PNG); or the ``no_registered_area``,
        ``area_not_found``, ``year_unavailable``, ``month_unavailable``,
        ``no_pasture``, ``earth_engine_unavailable`` or ``error`` instructions (no image).

    Side effects:
        None on ``session_state``. Queries Earth Engine and downloads tiles.
    """
    year, month = year or None, month or None
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
        return ToolResult(content=with_note(_success_text(tool_key, feature, stats, year is None and month is None), latest_area_note(run_context, area_name, feature)), images=[image])
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
        year (int, optional): Reference year; with no year and no month, the most recent data available for the area.
        month (int, optional): Month (1-12) of recent Time2Graze data.

    Returns:
        ToolResult: The ``success`` text (period, source, mean t DM/ha and total
        t DM); or the ``no_registered_area``, ``area_not_found``,
        ``year_unavailable``, ``month_unavailable``, ``no_pasture``,
        ``earth_engine_unavailable`` or ``error`` instructions.

    Side effects:
        None on ``session_state``. Queries Earth Engine.
    """
    year, month = year or None, month or None
    tool_key = "get_biomass_total"
    log_debug(f"{tool_key}: area_name={area_name!r}, year={year}, month={month}")
    feature = None
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        return ToolResult(content=with_note(_success_text(tool_key, feature, measure_total(feature, year, month), year is None and month is None), latest_area_note(run_context, area_name, feature)))
    except Exception as error:
        return _handle_known_errors(error, tool_key, feature)


@tool(description=get_tool_description(_COMPONENT, "generate_biomass_video"))
def generate_biomass_video(run_context: RunContext, area_name: Optional[str] = None, years: Optional[int] = None):
    """Generates an animated video of the evolution of the dry-matter production of a registered area.

    Mode: one frame per month of recent Time2Graze data when at least two usable
    months exist over the area; otherwise an annual fallback with the last
    ``years`` Global Pasture Watch years (not monthly). All frames share one
    fixed color scale and are labeled with their period.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest registered area when omitted.
        years (int, optional): Number of annual frames for the fallback (2 to 12, default 10).

    Returns:
        ToolResult: ``success_monthly`` or ``success_annual_fallback`` (mode, frames,
        color scale) with the MP4 video attached; or ``no_registered_area``,
        ``area_not_found``, ``no_pasture``, ``insufficient_data``,
        ``encoder_unavailable``, ``encoding_failed``, ``timeout``,
        ``earth_engine_unavailable`` or ``error`` instructions (no video).

    Side effects:
        None on ``session_state``. Queries Earth Engine, downloads tiles and runs ffmpeg.
    """
    tool_key = "generate_biomass_video"
    log_debug(f"{tool_key}: area_name={area_name!r}, years={years}")
    feature = None
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        video = render_video(feature, years)
        key = "success_monthly" if video.mode == "monthly" else "success_annual_fallback"
        content = get_tool_result_text(
            _COMPONENT, tool_key, key, area_name=area_label(feature), n_frames=len(video.frames),
            labels=", ".join(video.frames), vmin=video.vmin, vmax=video.vmax, duration=video.duration,
            reason_text=get_tool_result_text(_COMPONENT, tool_key, f"fallback_reason_{video.reason}") if video.mode != "monthly" else "",
        )
        media = Video(
            content=video.content, format=video.format, mime_type=video.mime_type, duration=video.duration,
            width=video.width, height=video.height, fps=video.fps,
        )
        return ToolResult(content=with_note(content, latest_area_note(run_context, area_name, feature)), videos=[media])
    except NoPastureBiomass:
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "no_pasture", area_name=area_label(feature) if feature else ""))
    except BiomassPeriodUnavailable as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "insufficient_data"))
    except VideoEncoderUnavailable as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "encoder_unavailable"))
    except VideoEncodingFailed as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "encoding_failed"))
    except VideoDeadlineExceeded:
        log_warning(f"{tool_key}: deadline exceeded")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "timeout"))
    except EarthEngineUnavailable as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "earth_engine_unavailable"))
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)
