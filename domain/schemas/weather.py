"""Weather schemas — forecast values returned by the weather tools.

All models are immutable plain data; the Open-Meteo calls that fill them live
in ``domain/services/geospatial/weather.py`` and the season heuristic in
``domain/services/geospatial/season_forecast.py``.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MonthlyPrecipitation(BaseModel):
    """Seasonal-forecast precipitation of one month."""

    model_config = ConfigDict(frozen=True)

    month: str = Field(..., description="Month as YYYY-MM.", examples=["2026-10"])
    mm: float = Field(..., description="Mean precipitation of the month in mm.", examples=[64.4])


class DailyQuartiles(BaseModel):
    """Ensemble precipitation of one day: median and quartiles over the members."""

    model_config = ConfigDict(frozen=True)

    day: date = Field(..., description="Local date.")
    q1: float = Field(..., description="First quartile in mm.")
    median: float = Field(..., description="Median in mm.")
    q3: float = Field(..., description="Third quartile in mm.")
    members: int = Field(..., description="Number of ensemble members used.")


class DailyTemperature(BaseModel):
    """Forecast daily temperature range."""

    model_config = ConfigDict(frozen=True)

    day: date = Field(..., description="Local date.")
    tmin: float = Field(..., description="Minimum temperature in degrees Celsius.")
    tmax: float = Field(..., description="Maximum temperature in degrees Celsius.")


class SeasonForecast(BaseModel):
    """Result of the rain/dry season onset heuristic.

    ``status`` is ``onset`` (``onset_date`` set), ``underway`` (the season is
    already running now) or ``not_within_horizon``.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(..., description="'rain' or 'dry'.")
    status: str = Field(..., description="'onset', 'underway' or 'not_within_horizon'.")
    onset_date: Optional[date] = Field(default=None, description="Predicted first day of the season.")
    days_ahead: Optional[int] = Field(default=None, description="Days from today to the onset (0 = today).")
    horizon_days: int = Field(..., description="Number of forecast days with valid data for every member.")
    current_state: str = Field(..., description="'wet' or 'dry' state at the start of the forecast.")
    recent_mm: float = Field(..., description="Precipitation of the last 30 days in mm.")
