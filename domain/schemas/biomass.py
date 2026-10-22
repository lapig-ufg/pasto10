"""Biomass schemas — data source and statistics of a dry-matter productivity analysis.

``BiomassSource`` tells which dataset produced the figures; ``BiomassStats``
is the result of measuring the productivity of the pasture inside a
registered area for one period. The Earth Engine calls that fill them live in
``domain/services/geospatial/biomass.py``.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BiomassSource(str, Enum):
    """Dataset that produced the productivity figures of a period.

    The value is the human-readable provenance name shown to the user and
    drawn on the map attribution.
    """

    GPW_ANNUAL = "Global Pasture Watch (annual gross primary productivity)"
    TIME2GRAZE = "Time2Graze (recent 10 m productivity)"


class BiomassStats(BaseModel):
    """Dry-matter productivity of the pasture inside a registered area.

    Figures are accumulated dry-matter production of the period (t DM/ha),
    not standing biomass nor available forage. ``mean_t_ha`` is the
    area-weighted mean over the productive pasture pixels and ``total_t`` the
    sum over them (about ``mean_t_ha * pasture_ha``). Immutable, so cached
    instances can be shared.
    """

    model_config = ConfigDict(frozen=True)

    source: BiomassSource = Field(..., description="Dataset that produced the figures.")

    period_label: str = Field(..., description="Period of the accumulation (e.g. '2022' or '2026-07').", examples=["2022"])

    start: str = Field(..., description="First day of the period actually covered (ISO date).", examples=["2022-01-01"])

    end: str = Field(..., description="Last day of the period actually covered (ISO date).", examples=["2022-12-31"])

    mean_t_ha: float = Field(..., description="Area-weighted mean dry-matter production in t DM/ha.", examples=[17.0])

    total_t: float = Field(..., description="Total dry-matter production of the pasture in t DM.", examples=[3481.2])

    p02_t_ha: float = Field(..., description="2nd percentile of the pixel values in t DM/ha (map stretch).", examples=[13.7])

    p98_t_ha: float = Field(..., description="98th percentile of the pixel values in t DM/ha (map stretch).", examples=[20.5])

    pasture_ha: float = Field(..., description="Productive pasture area used, in hectares.", examples=[205.4])

    area_ha: float = Field(..., description="Total area analyzed in hectares.", examples=[312.5])

    scale_m: int = Field(..., description="Analysis resolution in meters.", examples=[30])

    mask_year: int = Field(..., description="Year of the Global Pasture Watch grassland map used as pasture mask.", examples=[2022])

    year: int = Field(..., description="Year of the period.", examples=[2022])

    partial: bool = Field(default=False, description="True when the data cover less than the requested period.")

    n_images: Optional[int] = Field(default=None, description="Number of Time2Graze images averaged (None for annual data).")

    days_covered: int = Field(..., description="Number of days of the accumulation.", examples=[365])

    data_coverage: Optional[float] = Field(
        default=None,
        description="Share (0-1) of the grassland area of the registered area that has productivity data (Time2Graze only; None for annual data).",
        examples=[0.19],
    )

    @property
    def pasture_percent(self) -> float:
        """Share of the analyzed area that is productive pasture, in percent (0 when the area is empty)."""
        return round(100 * self.pasture_ha / self.area_ha, 1) if self.area_ha else 0.0
