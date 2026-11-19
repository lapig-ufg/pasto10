"""Geometry service — coordinate validation, geodesic distance and the buffer area Feature.

The circle itself is built by Semente's ``BufferSource`` (64-vertex ring on a
sphere, area π·r²/10⁴ ha); this module validates coordinates and wraps the
result as a Feature.
"""

import math
from typing import Tuple

from semente.logging import log_error
from semente.property_sources import BufferSource

from domain.schemas.feature import Feature
from domain.services.geospatial.candidate import candidate_to_feature

# Mean Earth radius (IUGG), used for the spherical geodesic computations.
EARTH_RADIUS_METERS = 6_371_008.8
# Web Mercator basemaps do not cover the poles; ±85° keeps the image meaningful.
MAX_ABS_LATITUDE = 85
MAX_ABS_LONGITUDE = 180


def normalize_longitude(longitude: float) -> float:
    """Wraps a longitude into the [-180, 180) degree range.

    Args:
        longitude (float): Longitude in decimal degrees (any value).

    Returns:
        float: The equivalent longitude in [-180, 180).
    """
    return (longitude + 180.0) % 360.0 - 180.0


def is_valid_coordinate(latitude: float, longitude: float) -> bool:
    """Checks that a coordinate is finite and inside the supported range.

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.

    Returns:
        bool: True when both values are finite numbers with
        ``|latitude| <= MAX_ABS_LATITUDE`` and ``|longitude| <= MAX_ABS_LONGITUDE``;
        False otherwise (including non-numeric values).
    """
    try:
        return (
            math.isfinite(latitude) and math.isfinite(longitude)
            and abs(latitude) <= MAX_ABS_LATITUDE
            and abs(longitude) <= MAX_ABS_LONGITUDE
        )
    except TypeError:
        return False


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes the great-circle distance between two coordinates.

    Args:
        lat1 (float): Latitude of the first point in decimal degrees.
        lon1 (float): Longitude of the first point in decimal degrees.
        lat2 (float): Latitude of the second point in decimal degrees.
        lon2 (float): Longitude of the second point in decimal degrees.

    Returns:
        float: Distance in meters on a sphere of radius ``EARTH_RADIUS_METERS``.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes the initial bearing of the great-circle path between two points.

    Args:
        lat1 (float): Start latitude in decimal degrees.
        lon1 (float): Start longitude in decimal degrees.
        lat2 (float): End latitude in decimal degrees.
        lon2 (float): End longitude in decimal degrees.

    Returns:
        float: Bearing in degrees clockwise from north, in [0, 360).
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(
    latitude: float,
    longitude: float,
    bearing: float,
    distance: float,
) -> Tuple[float, float]:
    """Computes the point reached from a start coordinate along a bearing.

    Uses the spherical "direct geodesic" formula, which is accurate to well
    under 1% for the short distances (≤ 1 km) used by buffer areas.

    Args:
        latitude (float): Start latitude in decimal degrees.
        longitude (float): Start longitude in decimal degrees.
        bearing (float): Initial bearing in degrees clockwise from north.
        distance (float): Distance to travel in meters.

    Returns:
        Tuple[float, float]: (latitude, longitude) of the destination, with
        the longitude normalized to [-180, 180).
    """
    phi1 = math.radians(latitude)
    lambda1 = math.radians(longitude)
    theta = math.radians(bearing)
    delta = distance / EARTH_RADIUS_METERS

    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(sin_phi2)
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * sin_phi2,
    )
    return math.degrees(phi2), normalize_longitude(math.degrees(lambda2))


def build_buffered_area(
    latitude: float,
    longitude: float,
    radius: float,
) -> Feature:
    """Builds a buffer area Feature from a coordinate and a radius in meters.

    Asks ``BufferSource`` for the circle and converts the candidate with
    ``candidate_to_feature`` (``feature_type='buffer_area'``, ``radius`` in
    meters and ``center`` as metadata, a generated ``feature_id``).

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius (float): Buffer radius in meters.

    Returns:
        Feature: The buffer area feature (not yet stored anywhere).

    Raises:
        ValueError: If the radius or the coordinate is rejected by ``BufferSource``.
    """
    try:
        candidate = BufferSource(radius_m=radius).find_by_point(latitude, longitude)[0]
        return candidate_to_feature(candidate, latitude, longitude)
    except Exception as e:
        log_error(f"build_buffered_area: {e}")
        raise
