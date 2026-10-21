"""Pasture schemas — data source and area statistics of a pasture analysis.

``PastureSource`` tells which dataset produced the pasture classes of a year;
``PastureAreaStats`` is the result of measuring the pasture inside a
registered area. Both are plain models: the Earth Engine calls that fill them
live in ``domain/services/geospatial/pasture.py``.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PastureSource(str, Enum):
    """Dataset that produced the pasture classes of a year.

    The value is the human-readable provenance name shown to the user and
    drawn on the map attribution.
    """

    GPW = "Global Pasture Watch"
    CLASSIFICATION = "Pasto10 classification of Google Satellite Embedding (AlphaEarth), trained on Global Pasture Watch"


class PastureAreaStats(BaseModel):
    """Pasture area measured inside a registered area for one year.

    ``pasture_ha`` is always ``cultivated_ha + natural_ha`` (rounded values
    are summed before rounding is applied again, so the figures stay
    consistent when displayed). Immutable, so cached instances can be shared.
    """

    model_config = ConfigDict(frozen=True)

    year: int = Field(..., description="Reference year of the classes.", examples=[2020])

    source: PastureSource = Field(..., description="Dataset that produced the classes.")

    pasture_ha: float = Field(..., description="Total pasture (cultivated + natural/semi-natural) in hectares.", examples=[18.4])

    cultivated_ha: float = Field(..., description="Cultivated grassland in hectares.", examples=[12.1])

    natural_ha: float = Field(..., description="Natural and semi-natural grassland in hectares.", examples=[6.3])

    area_ha: float = Field(..., description="Total area covered by the analysis in hectares.", examples=[28.27])

    @property
    def pasture_percent(self) -> float:
        """Share of the analyzed area that is pasture, in percent (0 when the area is empty)."""
        return round(100 * self.pasture_ha / self.area_ha, 1) if self.area_ha else 0.0
