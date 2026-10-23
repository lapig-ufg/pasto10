"""Property candidate schema — a polygon proposed to the user as "their area".

A ``PropertyCandidate`` comes from a property source (a national registry, an
OpenStreetMap land-use polygon, a polygon the user provides, or an approximate
circle around a coordinate). Once the user picks and confirms it, ``to_feature``
turns it into the ``Feature`` the rest of the app already analyzes, so the
pasture, biomass, weather, bulletin and video tools do not change.
"""

import json
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from domain.schemas.feature import Feature, FeatureMetadata


class GeometryOrigin(str, Enum):
    """Where the geometry of a candidate comes from (never shown as a raw key to the user)."""

    REGISTERED_BOUNDARY = "registered_boundary"
    OSM_LANDUSE = "osm_landuse"
    USER_POLYGON = "user_polygon"
    APPROXIMATE_CIRCLE = "approximate_circle"


class PropertyCandidate(BaseModel):
    """A candidate boundary for the area the user wants to register.

    ``warnings`` holds neutral keys (for example ``may_not_be_a_farm``) that the
    tools translate into texts from ``tools.yml``.
    """

    candidate_id: str = Field(..., description="Stable identifier of the candidate inside its source.")
    name: Optional[str] = Field(default=None, description="Name given by the source, if any.")
    origin: GeometryOrigin = Field(..., description="Kind of geometry.")
    source: str = Field(..., description="Key of the property source (e.g. osm).")
    source_date: Optional[str] = Field(default=None, description="Date of the source data (ISO), if known.")
    geometry: dict = Field(..., description="GeoJSON Polygon or MultiPolygon (lon, lat).")
    area_ha: float = Field(..., description="Area in hectares.")
    distance_m: Optional[float] = Field(default=None, description="Distance from the searched point in meters (0 when inside).")
    attributes: list[FeatureMetadata] = Field(default_factory=list, description="Source attributes (tags).")
    license: Optional[str] = Field(default=None, description="License and attribution of the data.")
    warnings: list[str] = Field(default_factory=list, description="Neutral warning keys for the tools to explain.")

    @property
    def type_key(self) -> str:
        """Neutral key of the geometry kind: ``demo_registry`` for synthetic demo parcels, otherwise the origin value."""
        return "demo_registry" if "synthetic_demo" in self.warnings else self.origin.value

    def to_feature(self) -> Feature:
        """Builds the ``Feature`` of this candidate.

        The feature keeps the MultiPolygon coordinates, the area, and the
        metadata ``origin``, ``source``, ``center`` (``"lat, lon"``) and
        ``radius`` (the radius in meters of the circle enclosing the polygon,
        used by the map renderers to choose their zoom), plus the attributes of
        the candidate and ``source_name`` when the source gave a name.

        Returns:
            Feature: A new feature (generated id) with ``feature_type`` equal to ``type_key``.

        Raises:
            ValueError: If the geometry is not a valid Polygon or MultiPolygon.
        """
        from domain.services.property_sources import geometry_utils as geo

        shape_ = geo.polygonal_part(geo.from_geojson_geometry(self.geometry))
        if shape_ is None:
            raise ValueError("Candidate geometry has no polygon.")
        latitude, longitude = geo.center_of(shape_)
        metadata = [
            FeatureMetadata(key="origin", value=self.origin.value),
            FeatureMetadata(key="source", value=self.source),
            FeatureMetadata(key="center", value=f"{latitude:.6f}, {longitude:.6f}"),
            FeatureMetadata(key="radius", value=round(geo.enclosing_radius_m(shape_, (latitude, longitude)), 1), unit="m"),
        ]
        if self.name:
            metadata.append(FeatureMetadata(key="source_name", value=self.name))
        if self.source_date:
            metadata.append(FeatureMetadata(key="source_date", value=self.source_date))
        metadata += [entry for entry in self.attributes if entry.key not in {m.key for m in metadata}]
        return Feature(
            feature_id=Feature.generate_id(),
            coords=geo.to_multipolygon_coords(shape_),
            metadata=metadata,
            total_area=round(self.area_ha, 2),
            feature_type=self.type_key,
        )

    def to_session(self) -> dict:
        """Returns the JSON-serializable form stored in ``session_state``.

        Returns:
            dict: The candidate as plain JSON types (enums as their values).
        """
        return json.loads(self.model_dump_json())
