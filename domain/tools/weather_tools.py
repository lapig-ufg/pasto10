"""Weather tools — Open-Meteo forecasts for a registered area.

Five read-only tools (monthly and daily precipitation, rainy and dry season
onset, temperature) that look up a registered area (by name, or the latest
one), take its center and call ``domain/services/geospatial/weather.py``. The
timezone is always the one of the area (``timezone=auto``). Every text comes
from ``domain/prompts/tools.yml``; known failures (service unavailable,
unknown area, horizon exceeded) get dedicated texts and anything else is
logged in full while the model only gets a generic message.
"""

from typing import Callable, Optional

from semente import Context as RunContext
from semente import ToolResult, tool
from semente.logging import log_debug, log_warning
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.services.geospatial import season_forecast, weather
from domain.services.geospatial.image import parse_center
from domain.services.geospatial.weather import WeatherServiceUnavailable
from domain.utils.tool_utils import area_label, latest_area_note, lookup_area, unexpected_error, with_note

_COMPONENT = "weather_tools"


def _run(
    run_context: RunContext, area_name: Optional[str], tool_key: str, render: Callable[..., ToolResult],
) -> ToolResult:
    """Runs a weather tool: resolves the area, calls ``render`` and maps failures to YAML texts.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.
        tool_key (str): Tool name under ``weather_tools`` in ``tools.yml``.
        render (Callable): ``render(feature_label, latitude, longitude)`` returning the ToolResult.

    Returns:
        ToolResult: The rendered result, or the ``no_registered_area``,
        ``area_not_found``, ``service_unavailable`` or ``error`` instructions.

    Side effects:
        None on ``session_state``. Calls the Open-Meteo APIs through the service cache.
    """
    log_debug(f"{tool_key}: area_name={area_name!r}")
    try:
        feature, failure = lookup_area(run_context, area_name, _COMPONENT, tool_key)
        if failure is not None:
            return failure
        latitude, longitude = parse_center(feature)
        result = render(area_label(feature), latitude, longitude)
        return ToolResult(content=with_note(result.content, latest_area_note(run_context, area_name, feature)), images=result.images)
    except WeatherServiceUnavailable as error:
        log_warning(f"{tool_key}: {error}")
        return ToolResult(content=get_tool_result_text(_COMPONENT, tool_key, "service_unavailable"))
    except Exception:
        return unexpected_error(_COMPONENT, tool_key)


def _text(tool_key: str, key: str, **values) -> str:
    """Renders one result text of a weather tool from ``tools.yml``.

    Args:
        tool_key (str): Tool name under ``weather_tools``.
        key (str): Result key (``success``, ``line``, ``underway``...).
        **values: Placeholders of the template.

    Returns:
        str: The rendered instruction text.

    Raises:
        MissingPromptError: If the text is missing.
    """
    return get_tool_result_text(_COMPONENT, tool_key, key, **values)


@tool(description=get_tool_description(_COMPONENT, "get_monthly_precipitation_forecast"))
def get_monthly_precipitation_forecast(run_context: RunContext, area_name: Optional[str] = None, months: int = 7):
    """Returns the seasonal monthly precipitation forecast of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.
        months (int): Number of months (1 to 7, clamped); the current month comes first. Default 7.

    Returns:
        ToolResult: The ``success`` text with one ``line`` per month (mm), or the
        ``no_registered_area``, ``area_not_found``, ``service_unavailable`` or ``error`` instructions.

    Side effects:
        None on ``session_state``.
    """
    tool_key = "get_monthly_precipitation_forecast"

    def render(label, latitude, longitude):
        rows, timezone = weather.get_monthly_forecast(latitude, longitude, months)
        lines = "\n".join(_text(tool_key, "line", month=row.month, mm=row.mm) for row in rows)
        return ToolResult(content=_text(tool_key, "success", area_name=label, timezone=timezone, count=len(rows), lines=lines))

    return _run(run_context, area_name, tool_key, render)


@tool(description=get_tool_description(_COMPONENT, "get_daily_precipitation_forecast"))
def get_daily_precipitation_forecast(run_context: RunContext, area_name: Optional[str] = None, days_ahead: int = 1):
    """Returns the ensemble precipitation forecast (median and quartiles) of one future day.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.
        days_ahead (int): Target day counted from today in the area's timezone (1 = today), at least 1; days beyond the
            forecast horizon (about 33) are answered with ``horizon_exceeded``. Default 1.

    Returns:
        ToolResult: The ``success`` text (Q1, median, Q3 in mm), ``horizon_exceeded``
        with the available horizon when the day is beyond it, or the usual failure instructions.

    Side effects:
        None on ``session_state``.
    """
    tool_key = "get_daily_precipitation_forecast"

    def render(label, latitude, longitude):
        rows, timezone = weather.get_daily_quartiles(latitude, longitude)
        target = max(1, days_ahead)
        if target > len(rows):
            return ToolResult(content=_text(tool_key, "horizon_exceeded", area_name=label, days_ahead=days_ahead, horizon=len(rows)))
        row = rows[target - 1]
        return ToolResult(content=_text(
            tool_key, "success", area_name=label, timezone=timezone, day=row.day.isoformat(), days_ahead=target,
            q1=row.q1, median=row.median, q3=row.q3, members=row.members,
        ))

    return _run(run_context, area_name, tool_key, render)


def _season_tool(run_context: RunContext, area_name: Optional[str], tool_key: str, kind: str) -> ToolResult:
    """Shared body of the two season onset tools.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.
        tool_key (str): Tool name under ``weather_tools`` (rain or dry onset).
        kind (str): ``"rain"`` or ``"dry"``.

    Returns:
        ToolResult: ``success`` (onset date), ``underway`` or ``not_within_horizon``
        with the heuristic's thresholds, or the usual failure instructions.
    """
    def render(label, latitude, longitude):
        result, timezone = weather.get_season_forecast(kind, latitude, longitude)
        values = dict(
            area_name=label, timezone=timezone, onset_date=result.onset_date.isoformat() if result.onset_date else "",
            days_ahead=result.days_ahead, horizon_days=result.horizon_days, recent_mm=result.recent_mm,
            window_days=season_forecast.WINDOW_DAYS, wet_mm=season_forecast.WET_MM, dry_mm=season_forecast.DRY_MM,
            persistence_days=season_forecast.PERSISTENCE_DAYS,
        )
        key = {"onset": "success", "underway": "underway", "not_within_horizon": "not_within_horizon"}[result.status]
        return ToolResult(content=_text(tool_key, key, **values))

    return _run(run_context, area_name, tool_key, render)


@tool(description=get_tool_description(_COMPONENT, "get_rain_season_onset_forecast"))
def get_rain_season_onset_forecast(run_context: RunContext, area_name: Optional[str] = None):
    """Predicts when the rainy season starts in a registered area (demonstrative heuristic).

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.

    Returns:
        ToolResult: ``success`` with the predicted date, ``underway`` when the
        rainy season is already running, ``not_within_horizon`` when it does not
        start within the forecast horizon, or the usual failure instructions.

    Side effects:
        None on ``session_state``.
    """
    return _season_tool(run_context, area_name, "get_rain_season_onset_forecast", "rain")


@tool(description=get_tool_description(_COMPONENT, "get_dry_season_onset_forecast"))
def get_dry_season_onset_forecast(run_context: RunContext, area_name: Optional[str] = None):
    """Predicts when the dry season starts in a registered area (demonstrative heuristic).

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.

    Returns:
        ToolResult: ``success`` with the predicted date, ``underway`` when the dry
        season is already running, ``not_within_horizon`` when it does not start
        within the forecast horizon, or the usual failure instructions.

    Side effects:
        None on ``session_state``.
    """
    return _season_tool(run_context, area_name, "get_dry_season_onset_forecast", "dry")


@tool(description=get_tool_description(_COMPONENT, "get_temperature_forecast"))
def get_temperature_forecast(run_context: RunContext, area_name: Optional[str] = None, forecast_days: int = 16):
    """Returns the daily minimum and maximum temperature forecast of a registered area.

    Args:
        run_context (RunContext): The run context holding ``session_state`` (read only).
        area_name (str, optional): Name of the registered area; the latest when omitted.
        forecast_days (int): Number of days (1 to 16, clamped), today first. Default 16.

    Returns:
        ToolResult: The ``success`` text with one ``line`` per day (min and max in °C),
        or the usual failure instructions.

    Side effects:
        None on ``session_state``.
    """
    tool_key = "get_temperature_forecast"

    def render(label, latitude, longitude):
        rows, timezone = weather.get_temperature_forecast(latitude, longitude, forecast_days)
        lines = "\n".join(_text(tool_key, "line", day=row.day.isoformat(), tmin=row.tmin, tmax=row.tmax) for row in rows)
        return ToolResult(content=_text(tool_key, "success", area_name=label, timezone=timezone, count=len(rows), lines=lines, first_day=rows[0].day.isoformat() if rows else ""))

    return _run(run_context, area_name, tool_key, render)
