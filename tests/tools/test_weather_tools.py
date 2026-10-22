"""Tests of the weather tools (domain/tools/weather_tools.py) with real trimmed Open-Meteo responses.

No network: ``weather.get_json`` is mocked. The tools must leave ``session_state`` untouched.
"""

import copy
import json
from datetime import date
from pathlib import Path

import pytest
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_tool_description

from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.schemas.weather import SeasonForecast
from domain.services.geospatial import weather
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import weather_tools
from domain.utils.feature_utils import set_registered_features

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openmeteo"
DOMAIN = Path(__file__).resolve().parents[2] / "domain"
ALL_TOOLS = [
    "get_monthly_precipitation_forecast", "get_daily_precipitation_forecast", "get_rain_season_onset_forecast",
    "get_dry_season_onset_forecast", "get_temperature_forecast",
]


def _context(*names, latitude=-2.0, longitude=37.0) -> StateContext:
    features = []
    for index, name in enumerate(names):
        feature = build_buffered_area(latitude + index, longitude, 500)
        if name:
            feature.metadata.append(FeatureMetadata(key="name", value=name))
        features.append(feature)
    state = {}
    set_registered_features(state, RegisteredFeatures(features=features))
    return StateContext(session_state=state)


@pytest.fixture
def api(monkeypatch):
    state = {"site": "kenya", "calls": [], "error": None}

    def data():
        return json.loads((FIXTURES / f"{state['site']}.json").read_text(encoding="utf-8"))

    def get_json(url, params):
        state["calls"].append((url, dict(params)))
        if state["error"] is not None:
            raise state["error"]
        payload = data()
        if url == weather.ENSEMBLE_URL:
            return payload["ensemble"]
        if url == weather.SEASONAL_URL:
            return payload["monthly"] if "monthly" in params else payload["seasonal_daily"]
        return payload["recent"] if "past_days" in params else payload["temperature"]

    weather._cache.clear()
    monkeypatch.setattr(weather, "get_json", get_json)
    monkeypatch.setattr(weather, "local_today", lambda offset, now_utc=None: date.fromisoformat(data()["recent"]["daily"]["time"][-1]))
    yield state
    weather._cache.clear()


def test_monthly_lists_months_with_timezone_and_attribution(api):
    result = weather_tools.get_monthly_precipitation_forecast(run_context=_context("Savanna"), months=3)

    assert "Savanna" in result.content and "Africa/Nairobi" in result.content and "3 month(s)" in result.content
    assert result.content.count(" mm") == 3 and "Open-Meteo.com (CC-BY 4.0)" in result.content


def test_daily_returns_median_and_quartiles_for_the_target_day(api):
    result = weather_tools.get_daily_precipitation_forecast(run_context=_context("A"), days_ahead=5)

    assert "day 5" in result.content and "median" in result.content and "interquartile" in result.content
    assert "Open-Meteo.com" in result.content


def test_daily_beyond_the_horizon_reports_the_available_horizon(api):
    result = weather_tools.get_daily_precipitation_forecast(run_context=_context("A"), days_ahead=32)
    horizon = len(weather.daily_quartiles(json.loads((FIXTURES / "kenya.json").read_text())["ensemble"]))

    if horizon < 32:
        assert f"reaches only {horizon} days" in result.content and "do NOT invent" in result.content
    result = weather_tools.get_daily_precipitation_forecast(run_context=_context("A"), days_ahead=99)
    assert f"reaches only {horizon} days" in result.content


def test_temperature_lists_days_with_min_and_max(api):
    result = weather_tools.get_temperature_forecast(run_context=_context("A"), forecast_days=5)

    assert result.content.count("min ") == 5 and "°C" in result.content and "Africa/Nairobi" in result.content


@pytest.mark.parametrize("tool_name", ["get_rain_season_onset_forecast", "get_dry_season_onset_forecast"])
def test_season_tools_state_the_heuristic_and_its_limits(api, tool_name):
    result = getattr(weather_tools, tool_name)(run_context=_context("A"))

    for expected in ("heuristic", "low skill", "demonstration", "marked wet/dry cycle", "Open-Meteo.com"):
        assert expected in result.content


@pytest.mark.parametrize(
    "status, expected",
    [("onset", "predicted to start around 2026-12-01"), ("underway", "already underway"), ("not_within_horizon", "within the 194-day")],
)
def test_rain_season_statuses(monkeypatch, status, expected):
    forecast = SeasonForecast(
        kind="rain", status=status, onset_date=date(2026, 12, 1) if status == "onset" else None,
        days_ahead=40 if status == "onset" else None, horizon_days=194, current_state="dry", recent_mm=12.0,
    )
    monkeypatch.setattr(weather, "get_season_forecast", lambda kind, lat, lon: (forecast, "Africa/Nairobi"))

    result = weather_tools.get_rain_season_onset_forecast(run_context=_context("A"))

    assert expected in result.content


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_session_state_is_untouched_and_area_is_resolved(api, tool_name):
    run_context = _context("North", "South", latitude=-2.0)
    before = copy.deepcopy(run_context.session_state)

    getattr(weather_tools, tool_name)(run_context=run_context, area_name="north")
    getattr(weather_tools, tool_name)(run_context=run_context)

    assert run_context.session_state == before
    latitudes = {round(params["latitude"]) for _, params in api["calls"]}
    assert latitudes == {-2, -1}


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_all_requests_use_the_timezone_of_the_area(api, tool_name):
    getattr(weather_tools, tool_name)(run_context=_context("A"))

    assert api["calls"] and all(params["timezone"] == "auto" for _, params in api["calls"])


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_no_registered_area_and_unknown_name(api, tool_name):
    tool = getattr(weather_tools, tool_name)

    empty = tool(run_context=_context())
    unknown = tool(run_context=_context("North"), area_name="Lake")

    assert "no registered area" in empty.content and "Lake" in unknown.content and api["calls"] == []


@pytest.mark.parametrize("error", [weather.WeatherServiceUnavailable("HTTP 429 secret-token"), weather.WeatherServiceUnavailable("timeout")])
@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_service_failures_become_service_unavailable_without_leaking_details(api, tool_name, error):
    api["error"] = error

    result = getattr(weather_tools, tool_name)(run_context=_context("A"))

    assert "temporarily unavailable" in result.content and "secret-token" not in result.content


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_unexpected_errors_use_the_generic_text(monkeypatch, tool_name):
    monkeypatch.setattr(weather, "fetch", lambda url, params: (_ for _ in ()).throw(RuntimeError("boom secret")))

    result = getattr(weather_tools, tool_name)(run_context=_context("A"))

    assert "unexpected error" in result.content and "secret" not in result.content


@pytest.mark.parametrize("tool_name", ALL_TOOLS)
def test_descriptions_exist_and_mention_the_area_argument(tool_name):
    assert "area_name" in get_tool_description("weather_tools", tool_name)


def test_no_hardcoded_timezone_anywhere_in_the_domain():
    for path in DOMAIN.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".yml", ".md"):
            assert "Sao_Paulo" not in path.read_text(encoding="utf-8"), path


def test_season_texts_state_window_persistence_and_the_gray_zone(api):
    result = weather_tools.get_dry_season_onset_forecast(run_context=_context("A"))

    for expected in ("30-day rainfall total", "7 consecutive days", "between 30.0 and 60.0 mm", "0 days ahead means today"):
        assert expected in result.content


def test_daily_description_matches_the_code_horizon():
    description = get_tool_description("weather_tools", "get_daily_precipitation_forecast")

    assert "1 = today" in description and "horizon_exceeded" in description and "33-34" in description
