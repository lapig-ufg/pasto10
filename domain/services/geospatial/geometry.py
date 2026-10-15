import math
from typing import List, Tuple

from semente.logging import log_error

from domain.schemas.feature import Feature, FeatureMetadata

# Mean Earth radius (IUGG), used for the spherical geodesic computations.
EARTH_RADIUS_METERS = 6_371_008.8
BUFFER_VERTICES = 64
_SQUARE_METERS_PER_HECTARE = 10_000.0


def normalize_longitude(longitude: float) -> float:
    """Wraps a longitude into the [-180, 180) degree range.

    Args:
        longitude (float): Longitude in decimal degrees (any value).

    Returns:
        float: The equivalent longitude in [-180, 180).
    """
    return (longitude + 180.0) % 360.0 - 180.0


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


def circle_area_hectares(radius: float) -> float:
    """Returns the area of a circle of the given radius in hectares (π·r²/10⁴).

    Args:
        radius (float): Circle radius in meters.

    Returns:
        float: Area in hectares.
    """
    return math.pi * radius ** 2 / _SQUARE_METERS_PER_HECTARE


def create_buffer_polygon(
    latitude: float,
    longitude: float,
    radius: float,
    vertices: int = BUFFER_VERTICES,
) -> List[List[List[List[float]]]]:
    """Creates a circular buffer polygon around a geographic coordinate.

    Each vertex is the geodesic destination point at ``radius`` meters from
    the center, at evenly spaced bearings starting from north. The ring is
    closed (first vertex repeated at the end), as GeoJSON requires.

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius (float): Buffer radius in meters (must be > 0).
        vertices (int): Number of distinct vertices of the ring (≥ 3).

    Returns:
        GeoJSON MultiPolygon nested coordinates (``[[[ [lon, lat], ... ]]]``).

    Raises:
        ValueError: If ``radius`` is not positive or ``vertices`` < 3.
    """
    if radius <= 0:
        raise ValueError(f"Buffer radius must be positive, got {radius}.")
    if vertices < 3:
        raise ValueError(f"A buffer ring needs at least 3 vertices, got {vertices}.")

    ring = []
    for i in range(vertices):
        lat, lon = destination_point(latitude, longitude, 360.0 * i / vertices, radius)
        ring.append([lon, lat])
    ring.append(list(ring[0]))
    return [[ring]]


def build_buffered_area(
    latitude: float,
    longitude: float,
    radius: float,
) -> Feature:
    """Builds a buffer area Feature from a coordinate and a radius in meters.

    Generates the circular polygon in pure Python (no projection library),
    computes its area as π·r²/10⁴ hectares and packages everything into the
    general Feature model with ``feature_type='buffer_area'``. The radius and
    the center (``"lat, lon"``) are stored as metadata; a generated id
    becomes the stable ``feature_id``.

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        radius (float): Buffer radius in meters.

    Returns:
        Feature: The buffer area feature (not yet stored anywhere).

    Raises:
        ValueError: If the polygon cannot be built (see ``create_buffer_polygon``).
    """
    try:
        coordinates = create_buffer_polygon(latitude=latitude, longitude=longitude, radius=radius)
    except Exception as e:
        log_error(f"build_buffered_area: {e}")
        raise

    return Feature(
        feature_id=Feature.generate_id(),
        coords=coordinates,
        metadata=[
            FeatureMetadata(key="radius", value=radius),
            FeatureMetadata(key="center", value=f"{latitude}, {longitude}"),
        ],
        total_area=round(circle_area_hectares(radius), 2),
        feature_type="buffer_area",
    )
