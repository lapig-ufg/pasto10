"""Open-Meteo client and response extraction for the weather tools.

The only network point is ``get_json`` (a ``requests`` session with retries),
so tests can replace it. Responses are cached for an hour per rounded
coordinate and parameters, with single-flight semantics. Every request uses
``timezone=auto``: dates are the local dates of the area and the local "today"
is derived from the ``utc_offset_seconds`` of the response, with no timezone
database. Weather data by Open-Meteo.com (CC-BY 4.0).
"""

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter, Retry

from semente.logging import log_warning

from domain.schemas.weather import DailyQuartiles, DailyTemperature, MonthlyPrecipitation
from domain.services.geospatial import season_forecast
from domain.utils.cache_utils import SingleFlightCache

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
SEASONAL_URL = "https://seasonal-api.open-meteo.com/v1/seasonal"
ENSEMBLE_MODELS = ("ncep_gefs_seamless", "gem_global")
SEASONAL_DAYS = 217
MONTHLY_FORECAST_DAYS = 214
MAX_MONTHS = 7
MAX_TEMPERATURE_DAYS = 16
REQUEST_TIMEOUT_SECONDS = 20
COORDINATE_DECIMALS = 2
CACHE_TTL_SECONDS = 3600
USER_AGENT = "pasto10 (LAPIG/UFG; +https://github.com/lapig-ufg/pasto10)"

_cache = SingleFlightCache(256, ttl=CACHE_TTL_SECONDS)
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
)))


class WeatherServiceUnavailable(Exception):
    """The weather service cannot be used (network error, timeout, rate limit or an error response).

    Attributes:
        status (int, optional): HTTP status of the failed response, None for network errors, timeouts or empty data.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        """Creates the error.

        Args:
            message (str): Description of the failure (logged, never shown to the model).
            status (int, optional): HTTP status of the failed response, if any.
        """
        super().__init__(message)
        self.status = status


def get_json(url: str, params: dict) -> dict:
    """Performs a GET request and returns the JSON body (the only network access of this module).

    Args:
        url (str): Endpoint URL.
        params (dict): Query parameters.

    Returns:
        dict: The decoded JSON response.

    Raises:
        WeatherServiceUnavailable: On network errors, timeouts, HTTP errors
        (including 400 and 429 after the retries) or a non-JSON body.
    """
    try:
        response = _session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        log_warning(f"weather: request to {url} failed: {type(error).__name__}")
        status = getattr(getattr(error, "response", None), "status_code", None)
        raise WeatherServiceUnavailable(str(error), status) from error


def fetch(url: str, params: dict) -> dict:
    """Returns the JSON of a request, cached for an hour per URL and parameters (single-flight).

    Args:
        url (str): Endpoint URL.
        params (dict): Query parameters; latitude and longitude are rounded to
            ``COORDINATE_DECIMALS`` (2 decimals, about 1 km) for the cache key and the
            request, because the forecast grids are coarser than that, so nearby
            points share one cached response and the API quota.

    Returns:
        dict: The decoded JSON response.

    Raises:
        WeatherServiceUnavailable: See ``get_json``; failures are not cached.
    """
    rounded = {
        **params,
        **{key: round(float(params[key]), COORDINATE_DECIMALS) for key in ("latitude", "longitude") if key in params},
    }
    key = (url, tuple(sorted((name, str(value)) for name, value in rounded.items())))
    return _cache.get_or_compute(key, lambda: get_json(url, rounded))


def local_today(utc_offset_seconds: int, now_utc: Optional[datetime] = None) -> date:
    """Returns the current local date of an area from its UTC offset.

    Args:
        utc_offset_seconds (int): The ``utc_offset_seconds`` of an Open-Meteo response.
        now_utc (datetime, optional): Current UTC time (for tests); defaults to now.

    Returns:
        date: The local date.
    """
    now = now_utc or datetime.now(timezone.utc)
    return (now + timedelta(seconds=utc_offset_seconds)).date()


def monthly_precipitation(payload: dict, months: int = MAX_MONTHS) -> list[MonthlyPrecipitation]:
    """Extracts the monthly precipitation (current month first) from a seasonal response.

    Args:
        payload (dict): Response with ``monthly.time`` and ``monthly.precipitation_mean``.
        months (int): Number of months to keep.

    Returns:
        list[MonthlyPrecipitation]: Months with a value, in order, at most ``months``.
    """
    monthly = payload["monthly"]
    rows = [
        MonthlyPrecipitation(month=day[:7], mm=float(value))
        for day, value in zip(monthly["time"], monthly["precipitation_mean"])
        if value is not None
    ]
    return rows[:months]


def member_series(payload: dict, variable: str = "precipitation_sum") -> list[list[Optional[float]]]:
    """Extracts the daily series of every ensemble member from a response.

    Args:
        payload (dict): Response with a ``daily`` block.
        variable (str): Variable prefix; members are the keys starting with it.

    Returns:
        list[list[float | None]]: One daily series per member, today first.
    """
    daily = payload["daily"]
    return [list(values) for name, values in daily.items() if name.startswith(variable)]


def daily_quartiles(payload: dict) -> list[DailyQuartiles]:
    """Computes the median and quartiles over the members for each day with every member valid.

    The tail of the ensemble forecast is null; the series stops at the first
    day where any member has no value.

    Args:
        payload (dict): Ensemble response with ``daily.time`` and member series.

    Returns:
        list[DailyQuartiles]: One row per valid day, today first.
    """
    members = member_series(payload)
    rows = []
    for day, values in zip(payload["daily"]["time"], zip(*members)):
        if any(value is None for value in values):
            break
        q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else (values[0],) * 3
        rows.append(DailyQuartiles(
            day=date.fromisoformat(day), q1=round(q1, 2), median=round(median, 2), q3=round(q3, 2), members=len(values),
        ))
    return rows


def temperature_series(payload: dict, days: int = MAX_TEMPERATURE_DAYS) -> list[DailyTemperature]:
    """Extracts the daily temperature range from a forecast response.

    Args:
        payload (dict): Response with ``daily.temperature_2m_max`` and ``temperature_2m_min``.
        days (int): Number of days to keep.

    Returns:
        list[DailyTemperature]: Days with both values, today first, at most ``days``.
    """
    daily = payload["daily"]
    rows = [
        DailyTemperature(day=date.fromisoformat(day), tmin=float(low), tmax=float(high))
        for day, low, high in zip(daily["time"], daily["temperature_2m_min"], daily["temperature_2m_max"])
        if low is not None and high is not None
    ]
    return rows[:days]


def get_monthly_forecast(latitude: float, longitude: float, months: int = MAX_MONTHS) -> tuple[list[MonthlyPrecipitation], str]:
    """Fetches the seasonal monthly precipitation forecast of a point (network, cached).

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.
        months (int): Number of months, 1 to ``MAX_MONTHS`` (clamped).

    Returns:
        tuple: (months, timezone name).

    Raises:
        WeatherServiceUnavailable: If the service fails.
    """
    payload = fetch(SEASONAL_URL, {
        "latitude": latitude, "longitude": longitude, "monthly": "precipitation_mean",
        "forecast_days": MONTHLY_FORECAST_DAYS, "timezone": "auto",
    })
    return monthly_precipitation(payload, max(1, min(months, MAX_MONTHS))), payload.get("timezone", "")


def get_daily_quartiles(latitude: float, longitude: float) -> tuple[list[DailyQuartiles], str]:
    """Fetches the ensemble daily precipitation quartiles of a point (network, cached).

    The ``ncep_gefs_seamless`` model is required by the API; ``gem_global`` is
    tried only when the first model is rejected with HTTP 400 (not on rate
    limits or outages, to avoid doubling the retries).

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.

    Returns:
        tuple: (rows with every member valid, timezone name).

    Raises:
        WeatherServiceUnavailable: If the service fails or returns no valid day.
    """
    last_error: Optional[WeatherServiceUnavailable] = None
    for model in ENSEMBLE_MODELS:
        try:
            payload = fetch(ENSEMBLE_URL, {
                "latitude": latitude, "longitude": longitude, "models": model, "daily": "precipitation_sum",
                "forecast_days": 36, "timezone": "auto",
            })
            rows = daily_quartiles(payload)
            if not rows:
                raise WeatherServiceUnavailable("ensemble forecast without valid days")
            return rows, payload.get("timezone", "")
        except WeatherServiceUnavailable as error:
            last_error = error
            if error.status != 400:
                break
    raise last_error


def get_temperature_forecast(latitude: float, longitude: float, days: int = MAX_TEMPERATURE_DAYS) -> tuple[list[DailyTemperature], str]:
    """Fetches the daily temperature forecast of a point (network, cached).

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.
        days (int): Number of days, 1 to ``MAX_TEMPERATURE_DAYS`` (clamped).

    Returns:
        tuple: (rows, timezone name).

    Raises:
        WeatherServiceUnavailable: If the service fails.
    """
    days = max(1, min(days, MAX_TEMPERATURE_DAYS))
    payload = fetch(FORECAST_URL, {
        "latitude": latitude, "longitude": longitude, "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": days, "timezone": "auto",
    })
    return temperature_series(payload, days), payload.get("timezone", "")


def get_season_forecast(kind: str, latitude: float, longitude: float):
    """Predicts the rainy or dry season onset of a point (network, cached).

    Combines the last 30 observed days (forecast API ``past_days``) with the
    daily seasonal ensemble (see ``season_forecast``).

    Args:
        kind (str): ``"rain"`` or ``"dry"``.
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.

    Returns:
        tuple: (SeasonForecast, timezone name).

    Raises:
        WeatherServiceUnavailable: If a request fails or the forecast has no valid day.
    """
    seasonal = fetch(SEASONAL_URL, {
        "latitude": latitude, "longitude": longitude, "daily": "precipitation_sum",
        "forecast_days": SEASONAL_DAYS, "timezone": "auto",
    })
    recent = fetch(FORECAST_URL, {
        "latitude": latitude, "longitude": longitude, "daily": "precipitation_sum",
        "past_days": season_forecast.WINDOW_DAYS, "forecast_days": 1, "timezone": "auto",
    })
    today = local_today(seasonal["utc_offset_seconds"])
    past = [
        float(value) for day, value in zip(recent["daily"]["time"], recent["daily"]["precipitation_sum"])
        if value is not None and date.fromisoformat(day) < today
    ]
    members = member_series(seasonal)
    if season_forecast.horizon_days(members) == 0:
        raise WeatherServiceUnavailable("seasonal forecast without valid days")
    return season_forecast.forecast_season(kind, today, past, members), seasonal.get("timezone", "")
