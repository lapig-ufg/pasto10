"""Tests of the Open-Meteo client (domain/services/geospatial/weather.py) with real trimmed responses.

The single network point ``get_json`` is mocked; nothing touches the network.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from domain.services.geospatial import weather

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openmeteo"
SITES = ("kenya", "ireland", "desert_control", "queensland")


def load(site: str) -> dict:
    return json.loads((FIXTURES / f"{site}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clean_cache():
    weather._cache.clear()
    yield
    weather._cache.clear()


@pytest.fixture
def api(monkeypatch):
    """Serves the fixture of one site for every endpoint and records the calls."""
    state = {"site": "kenya", "calls": [], "fail": {}}

    def get_json(url, params):
        state["calls"].append((url, dict(params)))
        for needle, error in state["fail"].items():
            if needle in url + str(params):
                raise error
        data = load(state["site"])
        if url == weather.ENSEMBLE_URL:
            return data["ensemble"]
        if url == weather.SEASONAL_URL:
            return data["monthly"] if "monthly" in params else data["seasonal_daily"]
        return data["recent"] if "past_days" in params else data["temperature"]

    monkeypatch.setattr(weather, "get_json", get_json)
    monkeypatch.setattr(weather, "local_today", lambda offset, now_utc=None: date.fromisoformat(load(state["site"])["recent"]["daily"]["time"][-1]))
    return state


@pytest.mark.parametrize("site", SITES)
def test_monthly_has_seven_values_current_month_first(site):
    data = load(site)

    rows = weather.monthly_precipitation(data["monthly"])

    assert len(rows) == 7 and rows[0].month == data["monthly"]["monthly"]["time"][0][:7]
    assert all(row.mm >= 0 for row in rows)
    assert [row.month for row in rows] == sorted(row.month for row in rows)


def test_monthly_respects_the_requested_number_of_months(api):
    rows, zone = weather.get_monthly_forecast(-2.0, 37.0, 3)

    assert len(rows) == 3 and zone == "Africa/Nairobi"
    assert len(weather.get_monthly_forecast(-2.0, 37.0, 99)[0]) == 7
    assert len(weather.get_monthly_forecast(-2.0, 37.0, 0)[0]) == 1


@pytest.mark.parametrize("site", SITES)
def test_daily_quartiles_stop_at_the_null_tail(site):
    payload = load(site)["ensemble"]

    rows = weather.daily_quartiles(payload)

    assert 30 <= len(rows) < len(payload["daily"]["time"])
    assert all(row.q1 <= row.median <= row.q3 for row in rows)
    assert rows[0].day == date.fromisoformat(payload["daily"]["time"][0])
    assert all(row.members >= 20 for row in rows)


def test_daily_quartiles_on_a_synthetic_series():
    payload = {"daily": {"time": ["2026-01-01", "2026-01-02", "2026-01-03"],
                         "precipitation_sum": [1.0, 2.0, None], "precipitation_sum_member01": [2.0, 4.0, 1.0],
                         "precipitation_sum_member02": [3.0, 6.0, None], "precipitation_sum_member03": [4.0, 8.0, None]}}

    rows = weather.daily_quartiles(payload)

    assert len(rows) == 2 and (rows[0].q1, rows[0].median, rows[0].q3) == (1.75, 2.5, 3.25)


def test_temperature_series_keeps_days_with_both_values():
    payload = {"daily": {"time": ["2026-01-01", "2026-01-02", "2026-01-03"],
                         "temperature_2m_max": [30.0, 31.0, None], "temperature_2m_min": [20.0, 21.0, None]}}

    rows = weather.temperature_series(payload)

    assert [(row.tmin, row.tmax) for row in rows] == [(20.0, 30.0), (21.0, 31.0)]


@pytest.mark.parametrize("site", SITES)
def test_temperature_fixture_is_consistent(site):
    rows = weather.temperature_series(load(site)["temperature"])

    assert 14 <= len(rows) <= 16 and all(row.tmax >= row.tmin for row in rows)


def test_local_today_uses_the_utc_offset_only():
    now = datetime(2026, 10, 22, 22, 30, tzinfo=timezone.utc)

    assert weather.local_today(3 * 3600, now) == date(2026, 10, 23)
    assert weather.local_today(-5 * 3600, now) == date(2026, 10, 22)


def test_every_request_uses_the_timezone_of_the_area_and_rounded_coordinates(api):
    weather.get_monthly_forecast(-2.004, 37.0049, 7)
    weather.get_daily_quartiles(-2.004, 37.0049)
    weather.get_temperature_forecast(-2.004, 37.0049, 16)
    weather.get_season_forecast("rain", -2.004, 37.0049)

    assert api["calls"]
    for url, params in api["calls"]:
        assert params["timezone"] == "auto"
        assert params["latitude"] == -2.0 and params["longitude"] == 37.0
    assert [p["models"] for u, p in api["calls"] if u == weather.ENSEMBLE_URL] == ["ncep_gefs_seamless"]


def test_responses_are_cached_per_coordinates_and_parameters(api):
    for _ in range(3):
        weather.get_daily_quartiles(-2.0, 37.0)
    weather.get_daily_quartiles(-2.001, 37.001)
    weather.get_daily_quartiles(10.0, 37.0)

    assert len(api["calls"]) == 2


def test_failed_requests_are_not_cached(api):
    api["fail"]["ensemble"] = weather.WeatherServiceUnavailable("down")
    for _ in range(2):
        with pytest.raises(weather.WeatherServiceUnavailable):
            weather.get_daily_quartiles(-2.0, 37.0)
    del api["fail"]["ensemble"]

    assert weather.get_daily_quartiles(-2.0, 37.0)[0]


def test_ensemble_falls_back_to_the_second_model(api):
    api["fail"]["ncep_gefs_seamless"] = weather.WeatherServiceUnavailable("400", 400)

    rows, _ = weather.get_daily_quartiles(-2.0, 37.0)

    assert rows and [p["models"] for u, p in api["calls"] if u == weather.ENSEMBLE_URL] == ["ncep_gefs_seamless", "gem_global"]


@pytest.mark.parametrize("site", SITES)
def test_season_forecast_runs_on_real_responses(api, site):
    api["site"] = site

    for kind in ("rain", "dry"):
        result, zone = weather.get_season_forecast(kind, 0.0, 0.0)
        assert result.status in ("onset", "underway", "not_within_horizon") and result.horizon_days > 100 and zone
        assert result.current_state in ("wet", "dry") and result.recent_mm >= 0


class _Response:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("outcome", [_Response(400), _Response(429), _Response(200), requests.Timeout("slow"), requests.ConnectionError("down")])
def test_get_json_failures_become_service_unavailable(monkeypatch, outcome):
    def get(url, params, timeout):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(weather._session, "get", get)

    with pytest.raises(weather.WeatherServiceUnavailable):
        weather.get_json("https://example.test", {})


def test_get_json_returns_the_body(monkeypatch):
    monkeypatch.setattr(weather._session, "get", lambda url, params, timeout: _Response(200, {"ok": 1}))

    assert weather.get_json("https://example.test", {}) == {"ok": 1}


def test_retry_policy_covers_rate_limits():
    retry = weather._session.get_adapter("https://x").max_retries

    assert 429 in retry.status_forcelist and retry.total == 3


def test_seasonal_forecast_without_valid_days_is_service_unavailable(api, monkeypatch):
    data = load("kenya")
    for name in [key for key in data["seasonal_daily"]["daily"] if key.startswith("precipitation_sum")]:
        data["seasonal_daily"]["daily"][name] = [None] * len(data["seasonal_daily"]["daily"][name])
    original = weather.get_json
    monkeypatch.setattr(weather, "get_json", lambda url, params: data["seasonal_daily"] if (url == weather.SEASONAL_URL and "daily" in params) else original(url, params))

    with pytest.raises(weather.WeatherServiceUnavailable):
        weather.get_season_forecast("rain", -2.0, 37.0)


def test_empty_ensemble_is_service_unavailable(api, monkeypatch):
    payload = {"timezone": "x", "daily": {"time": ["2026-01-01"], "precipitation_sum": [None], "precipitation_sum_member01": [None]}}
    monkeypatch.setattr(weather, "get_json", lambda url, params: payload)

    with pytest.raises(weather.WeatherServiceUnavailable):
        weather.get_daily_quartiles(-2.0, 37.0)


def test_fallback_model_is_tried_only_after_a_400(api):
    api["fail"]["ncep_gefs_seamless"] = weather.WeatherServiceUnavailable("rate limited", 429)

    with pytest.raises(weather.WeatherServiceUnavailable):
        weather.get_daily_quartiles(-2.0, 37.0)

    assert [p["models"] for u, p in api["calls"] if u == weather.ENSEMBLE_URL] == ["ncep_gefs_seamless"]


@pytest.mark.parametrize(
    "timestamp, month",
    [("2026-10-01", "2026-10"), ("2026-09-30", "2026-10"), ("2026-10-31", "2026-11"), ("2026-12-31", "2027-01"), ("2026-01-01", "2026-01")],
)
def test_monthly_labels_are_right_in_zones_west_and_east_of_utc(timestamp, month):
    assert weather.month_of(timestamp) == month


def test_monthly_series_west_of_utc_starts_in_the_current_month():
    payload = {"monthly": {"time": ["2026-09-30", "2026-10-31", "2026-11-30"], "precipitation_mean": [125.0, 138.8, 229.1]}}

    assert [row.month for row in weather.monthly_precipitation(payload)] == ["2026-10", "2026-11", "2026-12"]
