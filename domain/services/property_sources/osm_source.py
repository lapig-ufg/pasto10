"""OpenStreetMap property source (Overpass API).

Looks for land-use polygons around a coordinate: ``landuse`` farmland, meadow,
farmyard, grass, orchard or vineyard and ``place=farm`` (ways and
multipolygon relations). A land-use polygon is NOT proof that the place is a
farm, so every candidate carries the origin ``osm_landuse`` and a warning.
Coverage varies a lot between regions; when nothing is found the app offers an
approximate circle or a polygon provided by the user.

Network: ``get_overpass`` is the only point that touches the network (two
public mirrors, timeout 30 s each, identifiable User-Agent). Answers are kept
in memory for an hour and, when ``PASTO10_CACHE_DIR`` is set, on disk, keyed by
the rounded coordinate. Data © OpenStreetMap contributors, ODbL 1.0.
"""

import os
import re
from typing import Iterable, Optional

import requests
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from semente.logging import log_warning

from domain.schemas.candidate import GeometryOrigin, PropertyCandidate
from domain.schemas.feature import FeatureMetadata
from domain.services.property_sources import geometry_utils as geo
from domain.services.property_sources.base import PropertySourceError
from domain.utils.cache_utils import DiskCache, SingleFlightCache

OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter")
URLS_ENV = "PASTO10_OVERPASS_URLS"
CACHE_DIR_ENV = "PASTO10_CACHE_DIR"
CACHE_HOURS_ENV = "PASTO10_OVERPASS_CACHE_HOURS"
DEFAULT_CACHE_HOURS = 24.0
USER_AGENT = "pasto10 (LAPIG/UFG; +https://github.com/lapig-ufg/pasto10)"
TIMEOUT_ENV = "PASTO10_OVERPASS_TIMEOUT"
TIMEOUT_SECONDS = 30
QUERY_TIMEOUT_SECONDS = 25
FARM_LANDUSE = ("farmland", "meadow", "farmyard", "grass", "orchard", "vineyard")
URBAN_LANDUSE = ("residential", "commercial", "industrial", "retail")
MIN_AREA_HA = 0.5
MAX_AREA_HA = 50_000.0
MAX_CANDIDATES = 5
SIMPLIFY_METERS = 5.0
COORDINATE_DECIMALS = 3
LICENSE = "ODbL 1.0 - © OpenStreetMap contributors"
_QUERY_VERSION = "osm-1"
_cache = SingleFlightCache(64, ttl=3600)


def _cache_ttl_seconds() -> float:
    """Seconds an Overpass answer stays valid on disk (``PASTO10_OVERPASS_CACHE_HOURS``, default 24 h; invalid values use the default)."""
    try:
        hours = float(os.getenv(CACHE_HOURS_ENV) or DEFAULT_CACHE_HOURS)
    except ValueError:
        hours = DEFAULT_CACHE_HOURS
    return (hours if hours > 0 else DEFAULT_CACHE_HOURS) * 3600


def _urls() -> tuple[str, ...]:
    """Returns the Overpass mirrors to try, in order (``PASTO10_OVERPASS_URLS`` or the two public defaults)."""
    configured = os.getenv(URLS_ENV)
    return tuple(url.strip() for url in configured.split(",") if url.strip()) if configured else OVERPASS_URLS


def _timeout() -> float:
    """Read timeout in seconds of one Overpass request (``PASTO10_OVERPASS_TIMEOUT`` or ``TIMEOUT_SECONDS``).

    The public instances often queue a request for 25-50 seconds, so the default is longer than a usual API call.
    """
    try:
        return float(os.getenv(TIMEOUT_ENV) or TIMEOUT_SECONDS)
    except ValueError:
        return float(TIMEOUT_SECONDS)


def get_overpass(query: str) -> dict:
    """Runs an Overpass query on the mirrors in order (the only network access of this module).

    Args:
        query (str): Overpass QL text.

    Returns:
        dict: The decoded JSON answer of the first mirror that responds correctly.

    Raises:
        PropertySourceError: ``timeout`` when every failure was a timeout, otherwise ``unavailable``
        (HTTP errors such as 429/502/504, network errors or a body that is not JSON).
    """
    kinds = []
    for url in _urls():
        try:
            response = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=(5, _timeout()))
            response.raise_for_status()
            return response.json()
        except requests.Timeout:
            kinds.append("timeout")
        except (requests.RequestException, ValueError) as error:
            log_warning(f"osm: {url} failed: {type(error).__name__}")
            kinds.append("unavailable")
    raise PropertySourceError("timeout" if kinds and all(kind == "timeout" for kind in kinds) else "unavailable", "all Overpass mirrors failed")


def build_query(latitude: float, longitude: float, radius_m: float) -> str:
    """Builds the Overpass query for the farm land-use polygons around a point, plus the urban land use at the point.

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.
        radius_m (float): Search radius in meters.

    Returns:
        str: Overpass QL text returning geometry and tags.
    """
    farm = "|".join(FARM_LANDUSE)
    urban = "|".join(URBAN_LANDUSE)
    around = f"around:{radius_m:g},{latitude},{longitude}"
    here = f"around:5,{latitude},{longitude}"
    return (
        f'[out:json][timeout:{QUERY_TIMEOUT_SECONDS}];('
        f'way({around})["landuse"~"^({farm})$"];relation({around})["landuse"~"^({farm})$"];'
        f'way({around})["place"="farm"];relation({around})["place"="farm"];'
        f'way({here})["landuse"~"^({urban})$"];relation({here})["landuse"~"^({urban})$"];'
        f");out geom tags;"
    )


def _cached(latitude: float, longitude: float, radius_m: float) -> dict:
    """Returns the Overpass answer of a point from memory, disk or the network (single-flight).

    The coordinate is rounded to ``COORDINATE_DECIMALS`` BEFORE the query is built, so the cache key (the rounded point and
    the radius) always describes exactly what was asked, and nearby points share one answer.

    Args:
        latitude (float): Latitude in decimal degrees.
        longitude (float): Longitude in decimal degrees.
        radius_m (float): Search radius in meters.

    Returns:
        dict: The decoded Overpass answer.

    Raises:
        PropertySourceError: If the mirrors fail (see ``get_overpass``).
    """
    lat, lon = round(latitude, COORDINATE_DECIMALS), round(longitude, COORDINATE_DECIMALS)
    key = (_QUERY_VERSION, lat, lon, radius_m)
    disk = DiskCache(os.getenv(CACHE_DIR_ENV), ttl=_cache_ttl_seconds())
    return _cache.get_or_compute(
        key, lambda: get_overpass(build_query(lat, lon, radius_m)),
        load=lambda: disk.load(("overpass",) + key), store=lambda data: disk.store(("overpass",) + key, data),
    )


def _coords(geometry: Iterable[dict]) -> list[tuple[float, float]]:
    """Converts an Overpass ``geometry`` node list to (lon, lat) tuples."""
    return [(node["lon"], node["lat"]) for node in geometry]


def assemble_rings(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Joins way segments into closed rings by matching end points; unclosed chains are discarded.

    Args:
        segments (list): Coordinate lists (lon, lat) of the ways of one role.

    Returns:
        list: Closed rings with at least four points.
    """
    pending = [list(segment) for segment in segments if len(segment) >= 2]
    rings = []
    while pending:
        chain = pending.pop(0)
        progressed = True
        while chain[0] != chain[-1] and progressed:
            progressed = False
            for index, other in enumerate(pending):
                if other[0] == chain[-1]:
                    chain += other[1:]
                elif other[-1] == chain[-1]:
                    chain += other[::-1][1:]
                elif other[-1] == chain[0]:
                    chain = other[:-1] + chain
                elif other[0] == chain[0]:
                    chain = other[::-1][:-1] + chain
                else:
                    continue
                pending.pop(index)
                progressed = True
                break
        if chain[0] == chain[-1] and len(chain) >= 4:
            rings.append(chain)
    return rings


def element_polygon(element: dict):
    """Builds the shapely polygon of an Overpass way or multipolygon relation.

    Args:
        element (dict): One element of an ``out geom`` answer.

    Returns:
        BaseGeometry | None: A valid Polygon/MultiPolygon (relations merge their outer rings and subtract
        the inner ones; rings that do not close are dropped), or None when the element has no usable area.
    """
    if element.get("type") == "way":
        ring = _coords(element.get("geometry", []))
        if len(ring) >= 4 and ring[0] == ring[-1]:
            return geo.polygonal_part(Polygon(ring))
        return None
    if element.get("type") != "relation":
        return None
    members = [member for member in element.get("members", []) if member.get("type") == "way" and member.get("geometry")]
    outer = assemble_rings([_coords(member["geometry"]) for member in members if member.get("role") in ("outer", "")])
    inner = assemble_rings([_coords(member["geometry"]) for member in members if member.get("role") == "inner"])
    if not outer:
        return None
    shape_ = unary_union([Polygon(ring).buffer(0) for ring in outer])
    if inner:
        shape_ = shape_.difference(unary_union([Polygon(ring).buffer(0) for ring in inner]))
    return geo.polygonal_part(shape_)


def _attributes(tags: dict) -> list[FeatureMetadata]:
    """Returns the relevant OpenStreetMap tags (land use, place, crop...) as ``osm_<tag>`` metadata entries."""
    keys = ("landuse", "place", "crop", "meadow", "produce", "farmland", "landuse:note")
    return [FeatureMetadata(key=f"osm_{key.replace(':', '_')}", value=str(tags[key])) for key in keys if key in tags]


class OsmSource:
    """Property source over OpenStreetMap land-use polygons.

    Attributes:
        key (str): ``"osm"``.
        min_area_ha (float): Candidates below this area are discarded.
        max_area_ha (float): Candidates above this area are discarded.
        max_candidates (int): Maximum number of candidates returned.
    """

    key = "osm"
    response_timeout_s = 12.0
    """Seconds the candidate search waits for this source before going on without it (the answer still fills the cache)."""

    def __init__(self, min_area_ha: float = MIN_AREA_HA, max_area_ha: float = MAX_AREA_HA, max_candidates: int = MAX_CANDIDATES):
        """Creates the source with its filters.

        Args:
            min_area_ha (float): Smallest candidate kept, in hectares.
            max_area_ha (float): Largest candidate kept, in hectares.
            max_candidates (int): Maximum number of candidates returned.
        """
        self.min_area_ha = min_area_ha
        self.max_area_ha = max_area_ha
        self.max_candidates = max_candidates

    def _candidate(self, element: dict, latitude: float, longitude: float, source_date: Optional[str]) -> Optional[PropertyCandidate]:
        """Converts a farm element to a candidate, or None when it is filtered out (area limits, no polygon)."""
        shape_ = element_polygon(element)
        if shape_ is None:
            return None
        shape_ = geo.polygonal_part(geo.simplify_meters(shape_, SIMPLIFY_METERS)) or shape_
        area = geo.area_hectares(shape_)
        if not self.min_area_ha <= area <= self.max_area_ha:
            return None
        tags = element.get("tags", {})
        distance = geo.distance_to_point_m(shape_, latitude, longitude)
        warnings = [] if tags.get("place") == "farm" else ["may_not_be_a_farm"]
        if distance > 0:
            warnings.append("does_not_contain_point")
        return PropertyCandidate(
            candidate_id=f"{element['type']}/{element['id']}", name=tags.get("name"), origin=GeometryOrigin.OSM_LANDUSE,
            source=self.key, source_date=source_date, geometry=geo.mapping(shape_), area_ha=round(area, 2),
            distance_m=round(distance, 1), attributes=_attributes(tags), license=LICENSE, warnings=warnings,
        )

    def find_by_point(self, latitude: float, longitude: float, search_radius_m: float = 2000) -> list[PropertyCandidate]:
        """Finds the farm land-use polygons around a coordinate.

        Candidates between ``min_area_ha`` and ``max_area_ha`` are simplified to about 5 m, ordered (containing the
        point first, then by distance, then by area) and limited to ``max_candidates``.

        Args:
            latitude (float): Latitude in decimal degrees.
            longitude (float): Longitude in decimal degrees.
            search_radius_m (float): Search radius in meters.

        Returns:
            list[PropertyCandidate]: Candidates with origin ``osm_landuse`` (possibly empty).

        Raises:
            PropertySourceError: ``unavailable``/``timeout`` when the mirrors fail, or ``urban_area`` when the
            point lies in a residential, commercial, industrial or retail land use and in no farm polygon.
        """
        data = _cached(latitude, longitude, search_radius_m)
        source_date = (data.get("osm3s") or {}).get("timestamp_osm_base")
        point = Point(longitude, latitude)
        candidates, urban = [], False
        for element in data.get("elements", []):
            tags = element.get("tags", {})
            if tags.get("landuse") in URBAN_LANDUSE and tags.get("place") != "farm":
                shape_ = element_polygon(element)
                urban = urban or (shape_ is not None and shape_.contains(point))
                continue
            candidate = self._candidate(element, latitude, longitude, source_date)
            if candidate is not None:
                candidates.append(candidate)
        if urban and not any(candidate.distance_m == 0 for candidate in candidates):
            raise PropertySourceError("urban_area", "the point lies in an urban land use")
        candidates.sort(key=lambda item: (item.distance_m != 0, item.distance_m or 0.0, item.area_ha))
        return candidates[: self.max_candidates]

    def find_by_id(self, identifier: str) -> Optional[PropertyCandidate]:
        """Returns the candidate of an OpenStreetMap element (``way/123`` or ``relation/123``).

        Args:
            identifier (str): Element reference.

        Returns:
            PropertyCandidate | None: The candidate, or None for an unknown or unusable element.

        Raises:
            PropertySourceError: ``unavailable``/``timeout`` when the mirrors fail.
        """
        match = re.fullmatch(r"(way|relation)/(\d+)", identifier.strip())
        if not match:
            return None
        data = get_overpass(f"[out:json][timeout:{QUERY_TIMEOUT_SECONDS}];{match.group(1)}({match.group(2)});out geom tags;")
        source_date = (data.get("osm3s") or {}).get("timestamp_osm_base")
        for element in data.get("elements", []):
            polygon = element_polygon(element)
            if polygon is not None:
                center = geo.center_of(polygon)
                return self._candidate(element, center[0], center[1], source_date)
        return None
