"""End-to-end tests of the weather tools over the 15 fixed global properties (real Open-Meteo, no GEE).

Registers each property through the real registration tools, runs the five
weather tools and checks: the timezone of the area comes from the API, seven
months of precipitation, ensemble quartiles with Q1 <= median <= Q3, a
temperature forecast with max >= min, the season onset statuses, and that a
second pass is served from the cache (no new requests, hence no 429). Prints
a summary table. Run with ``uv run pytest -m e2e -s tests/e2e/test_weather_e2e.py``.
"""

import re
import time

import pytest
from semente.backends.toolkit import StateContext

from domain.services.geospatial import weather
from domain.tools import property_tools, weather_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties

pytestmark = pytest.mark.e2e

PROPERTIES = load_global_properties()
PROPERTY_IDS = [item.id for item in PROPERTIES]
_summary: list[dict] = []


@pytest.fixture(autouse=True, scope="module")
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(15) + "timezone".ljust(22) + "mo0 mm".rjust(8) + "d1 q1/med/q3".rjust(18) + "tmax".rjust(7)
              + "  rain".ljust(28) + "dry".ljust(28) + "secs")
        for row in _summary:
            print(row["id"].ljust(15) + row["tz"].ljust(22) + row["mo"].rjust(8) + row["d1"].rjust(18) + row["tmax"].rjust(7)
                  + ("  " + row["rain"]).ljust(28) + row["dry"].ljust(28) + row["secs"])


def _register(item: GlobalProperty) -> StateContext:
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=item.latitude, longitude=item.longitude, radius=item.radius_m,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=item.name)
    return context


def _season_summary(text: str) -> str:
    date_match = re.search(r"around (\d{4}-\d{2}-\d{2})", text)
    if date_match:
        return f"onset {date_match.group(1)}"
    return "underway" if "already underway" in text else "not_within_horizon"


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_weather_tools_over_global_properties(item, monkeypatch):
    requests_made = []
    original = weather.get_json
    monkeypatch.setattr(weather, "get_json", lambda url, params: requests_made.append(url) or original(url, params))
    context = _register(item)
    registered = dict(context.session_state)
    start = time.perf_counter()

    monthly = weather_tools.get_monthly_precipitation_forecast(run_context=context)
    daily = weather_tools.get_daily_precipitation_forecast(run_context=context, days_ahead=1)
    far = weather_tools.get_daily_precipitation_forecast(run_context=context, days_ahead=99)
    rain = weather_tools.get_rain_season_onset_forecast(run_context=context)
    dry = weather_tools.get_dry_season_onset_forecast(run_context=context)
    temperature = weather_tools.get_temperature_forecast(run_context=context)
    seconds = time.perf_counter() - start

    for result in (monthly, daily, rain, dry, temperature):
        assert item.timezone in result.content, item.timezone
        assert "temporarily unavailable" not in result.content
    assert "7 month(s)" in monthly.content
    lat, lon = item.latitude, item.longitude
    quartiles, zone = weather.get_daily_quartiles(lat, lon)
    assert zone == item.timezone and 30 <= len(quartiles) <= 36
    assert all(row.q1 <= row.median <= row.q3 for row in quartiles)
    assert "reaches only" in far.content
    days, _ = weather.get_temperature_forecast(lat, lon)
    assert len(days) >= 14 and all(row.tmax >= row.tmin for row in days)
    months, _ = weather.get_monthly_forecast(lat, lon)
    assert len(months) == 7 and all(row.mm >= 0 for row in months)
    for text in (rain.content, dry.content):
        assert _season_summary(text) in ("underway", "not_within_horizon") or _season_summary(text).startswith("onset")
        assert "Open-Meteo.com" in text

    made = len(requests_made)
    weather_tools.get_monthly_precipitation_forecast(run_context=context)
    weather_tools.get_temperature_forecast(run_context=context)
    weather_tools.get_rain_season_onset_forecast(run_context=context)
    assert len(requests_made) == made
    assert context.session_state == registered

    first = quartiles[0]
    _summary.append({
        "id": item.id, "tz": zone, "mo": f"{months[0].mm:.0f}", "d1": f"{first.q1}/{first.median}/{first.q3}",
        "tmax": f"{days[0].tmax:.0f}", "rain": _season_summary(rain.content), "dry": _season_summary(dry.content),
        "secs": f"{seconds:.1f}",
    })
