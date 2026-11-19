"""Transect service — a straight sampling line between two coordinates.

A transect is a ``Feature`` with ``geometry_type="LineString"`` and
``feature_type="transect"``. Its ``total_area`` is the user-provided total area
of the property the line crosses (a transect itself has no area); the line
length is stored in the ``length_m`` metadata. Pasture and biomass amounts are
estimated from the mean measured along the line and extrapolated to that total
area.
"""

from typing import Tuple

from semente.logging import log_warning

from domain.schemas.feature import Feature, FeatureMetadata
from domain.services.geospatial.geometry import (
    destination_point,
    haversine_distance,
    initial_bearing,
)

# Accepted distance between the two coordinates (meters).
MIN_LENGTH_METERS = 100
MAX_LENGTH_METERS = 100_000
# Accepted total area of the property crossed by the line (hectares).
MIN_TOTAL_AREA_HA = 0.01
MAX_TOTAL_AREA_HA = 50_000.0


class InvalidTransect(Exception):
    """The transect line is too short or too long.

    Attributes:
        length_m (float): The distance between the two coordinates, in meters.
    """

    def __init__(self, length_m: float):
        """Creates the error.

        Args:
            length_m (float): The distance between the two coordinates, in meters.
        """
        super().__init__(
            f"Transect length {length_m:.1f} m is outside "
            f"{MIN_LENGTH_METERS}-{MAX_LENGTH_METERS} m"
        )
        self.length_m = length_m


def transect_midpoint(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    length_m: float,
) -> Tuple[float, float]:
    """Returns the midpoint of the great-circle path between two coordinates.

    Args:
        start_latitude (float): Start latitude in decimal degrees.
        start_longitude (float): Start longitude in decimal degrees.
        end_latitude (float): End latitude in decimal degrees.
        end_longitude (float): End longitude in decimal degrees.
        length_m (float): Distance between the points in meters.

    Returns:
        Tuple[float, float]: (latitude, longitude) of the midpoint.
    """
    bearing = initial_bearing(start_latitude, start_longitude, end_latitude, end_longitude)
    return destination_point(start_latitude, start_longitude, bearing, length_m / 2.0)


def build_transect(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> Feature:
    """Builds the transect Feature between two coordinates.

    The coordinates are stored as a GeoJSON LineString (``[[lon, lat], ...]``).
    The midpoint, the enclosing radius (half the length, used by the image
    renderer) and the length are stored as metadata; ``total_area`` starts at
    0 until the user provides the property's total area in the final
    registration step.

    Args:
        start_latitude (float): Start latitude in decimal degrees.
        start_longitude (float): Start longitude in decimal degrees.
        end_latitude (float): End latitude in decimal degrees.
        end_longitude (float): End longitude in decimal degrees.

    Returns:
        Feature: The transect feature (not yet stored anywhere).

    Raises:
        InvalidTransect: If the distance between the coordinates is outside
            ``MIN_LENGTH_METERS``-``MAX_LENGTH_METERS``.
    """
    length_m = haversine_distance(start_latitude, start_longitude, end_latitude, end_longitude)
    if not MIN_LENGTH_METERS <= length_m <= MAX_LENGTH_METERS:
        log_warning(f"build_transect: invalid length {length_m:.1f} m")
        raise InvalidTransect(length_m)

    center_latitude, center_longitude = transect_midpoint(
        start_latitude, start_longitude, end_latitude, end_longitude, length_m,
    )
    start = f"{start_latitude}, {start_longitude}"
    end = f"{end_latitude}, {end_longitude}"
    return Feature(
        feature_id=Feature.generate_id(),
        coords=[[start_longitude, start_latitude], [end_longitude, end_latitude]],
        geometry_type="LineString",
        metadata=[
            FeatureMetadata(key="origin", value="transect"),
            FeatureMetadata(key="start", value=start),
            FeatureMetadata(key="end", value=end),
            FeatureMetadata(key="center", value=f"{center_latitude}, {center_longitude}"),
            FeatureMetadata(key="radius", value=round(length_m / 2.0, 1), unit="m"),
            FeatureMetadata(key="length_m", value=round(length_m, 1), unit="m"),
        ],
        total_area=0.0,
        feature_type="transect",
    )
