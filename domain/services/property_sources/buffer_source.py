"""Approximate-circle source: a buffer around the coordinate, honestly labeled.

It is the fallback when no real boundary is available: the circle is NOT a
property boundary, and its candidate says so (origin ``approximate_circle``).
"""

from typing import Optional

from domain.schemas.candidate import GeometryOrigin, PropertyCandidate
from domain.schemas.feature import FeatureMetadata
from domain.services.geospatial.geometry import build_buffered_area

DEFAULT_RADIUS_METERS = 500


class BufferSource:
    """Source that returns one circle around the searched point.

    Attributes:
        key (str): ``"buffer"``.
        radius_m (float): Radius of the circle in meters.
    """

    key = "buffer"

    def __init__(self, radius_m: float = DEFAULT_RADIUS_METERS):
        """Creates the source.

        Args:
            radius_m (float): Radius of the circle in meters.
        """
        self.radius_m = radius_m

    def find_by_point(self, latitude: float, longitude: float, search_radius_m: float = 2000) -> list[PropertyCandidate]:
        """Returns one approximate circle of ``radius_m`` around the point (``search_radius_m`` is ignored).

        Args:
            latitude (float): Latitude in decimal degrees.
            longitude (float): Longitude in decimal degrees.
            search_radius_m (float): Unused; kept for the protocol.

        Returns:
            list[PropertyCandidate]: A single candidate with origin ``approximate_circle``.

        Raises:
            ValueError: If the circle cannot be built (invalid coordinate).
        """
        area = build_buffered_area(latitude, longitude, self.radius_m)
        return [PropertyCandidate(
            candidate_id=f"circle-{latitude:.5f}-{longitude:.5f}-{self.radius_m:g}",
            origin=GeometryOrigin.APPROXIMATE_CIRCLE, source=self.key,
            geometry={"type": "MultiPolygon", "coordinates": area.get_coords()},
            area_ha=area.total_area, distance_m=0.0,
            attributes=[FeatureMetadata(key="radius", value=self.radius_m, unit="m")],
            warnings=["approximate_area"],
        )]

    def find_by_id(self, identifier: str) -> Optional[PropertyCandidate]:
        """Circles have no identifiers: always None."""
        return None
