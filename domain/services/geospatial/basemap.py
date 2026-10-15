import math
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from io import BytesIO
from typing import Callable, Optional, Sequence, Tuple

import requests
from PIL import Image

from semente.logging import log_debug, log_warning

TILE_SIZE = 256
# Equatorial radius of the WGS84 ellipsoid, the sphere used by Web Mercator.
WEB_MERCATOR_RADIUS_METERS = 6_378_137.0
WEB_MERCATOR_MAX_LATITUDE = 85.05112878
USER_AGENT = "pasto10 (LAPIG/UFG; +https://github.com/lapig-ufg/pasto10)"
# (connect, read) timeouts of a single tile request, in seconds.
REQUEST_TIMEOUT = (3, 4)
# Total time budget of build_basemap, shared by every provider tried.
BASEMAP_DEADLINE_SECONDS = 6.0
MAX_TILE_WORKERS = 8

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": USER_AGENT})


@dataclass(frozen=True)
class TileProvider:
    """A keyless XYZ raster tile service usable as a basemap.

    Attributes:
        name (str): Short identifier of the provider (e.g. "esri").
        url_template (str): Tile URL with ``{z}``, ``{x}`` and ``{y}`` placeholders.
        attribution (str): Attribution text that must be drawn on the image.
        max_zoom (int): Highest zoom level the provider serves.
    """

    name: str
    url_template: str
    attribution: str
    max_zoom: int


ESRI_WORLD_IMAGERY = TileProvider(
    name="esri",
    url_template="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution="Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    max_zoom=18,
)

OPENSTREETMAP = TileProvider(
    name="osm",
    url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution="© OpenStreetMap contributors",
    max_zoom=19,
)

DEFAULT_PROVIDERS: Tuple[TileProvider, ...] = (ESRI_WORLD_IMAGERY, OPENSTREETMAP)


def latlon_to_pixel(latitude: float, longitude: float, zoom: int) -> Tuple[float, float]:
    """Projects a coordinate to global Web Mercator pixel coordinates.

    Longitudes outside [-180, 180) are projected linearly (no wrapping), so a
    ring crossing the antimeridian stays continuous once its longitudes are
    unwrapped around its center.

    Args:
        latitude (float): Latitude in decimal degrees (clamped to the Web Mercator limit).
        longitude (float): Longitude in decimal degrees.
        zoom (int): Tile zoom level.

    Returns:
        Tuple[float, float]: (x, y) pixel position in the world image at ``zoom``.
    """
    lat = max(-WEB_MERCATOR_MAX_LATITUDE, min(WEB_MERCATOR_MAX_LATITUDE, latitude))
    world_size = TILE_SIZE * (2 ** zoom)
    x = (longitude + 180.0) / 360.0 * world_size
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * world_size
    return x, y


def meters_per_pixel(latitude: float, zoom: int) -> float:
    """Returns the ground resolution of a Web Mercator image at a latitude.

    Args:
        latitude (float): Latitude in decimal degrees.
        zoom (int): Tile zoom level.

    Returns:
        float: Meters on the ground covered by one pixel.
    """
    circumference = 2 * math.pi * WEB_MERCATOR_RADIUS_METERS
    return math.cos(math.radians(latitude)) * circumference / (TILE_SIZE * 2 ** zoom)


def choose_zoom(
    latitude: float,
    radius: float,
    image_size: Tuple[int, int],
    fill_ratio: float = 0.6,
    max_zoom: int = ESRI_WORLD_IMAGERY.max_zoom,
) -> int:
    """Picks the highest zoom at which the buffer fits comfortably in the image.

    Args:
        latitude (float): Center latitude in decimal degrees.
        radius (float): Buffer radius in meters.
        image_size (Tuple[int, int]): Output image (width, height) in pixels.
        fill_ratio (float): Maximum fraction of the smaller image side the
            buffer diameter may occupy.
        max_zoom (int): Upper bound for the zoom level.

    Returns:
        int: Zoom level in [1, max_zoom].
    """
    available_pixels = min(image_size) * fill_ratio
    for zoom in range(max_zoom, 0, -1):
        if 2 * radius / meters_per_pixel(latitude, zoom) <= available_pixels:
            return zoom
    return 1


def fetch_tile(provider: TileProvider, zoom: int, x: int, y: int) -> Image.Image:
    """Downloads one raster tile from a provider.

    Uses the module-level ``requests.Session`` (connection reuse, with the
    ``USER_AGENT`` header required by the tile usage policies) and the
    ``REQUEST_TIMEOUT`` (connect, read) timeouts.

    Args:
        provider (TileProvider): The tile service.
        zoom (int): Tile zoom level.
        x (int): Tile column (already wrapped into the valid range).
        y (int): Tile row.

    Returns:
        PIL.Image.Image: The decoded tile, converted to RGB.

    Raises:
        requests.RequestException: On network errors, timeouts or HTTP error statuses.
        PIL.UnidentifiedImageError: If the response body is not an image.
    """
    url = provider.url_template.format(z=zoom, x=x, y=y)
    response = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


def _build_mosaic(
    provider: TileProvider,
    origin: Tuple[float, float],
    zoom: int,
    size: Tuple[int, int],
    fetch: Callable[[TileProvider, int, int, int], Image.Image],
    timeout: float,
) -> Image.Image:
    """Stitches the tiles covering a pixel window into a single image.

    Tiles are downloaded in parallel (up to ``MAX_TILE_WORKERS`` threads).
    The function never blocks past ``timeout``: on return or failure the
    pool is shut down without waiting, and pending downloads are cancelled
    (the ones already running finish in the background and are discarded).
    Rows outside the world are left blank.

    Args:
        provider (TileProvider): The tile service.
        origin (Tuple[float, float]): Global pixel position of the top-left corner.
        zoom (int): Tile zoom level.
        size (Tuple[int, int]): Output (width, height) in pixels.
        fetch (Callable): Tile fetcher ``(provider, z, x, y) -> Image``.
        timeout (float): Maximum seconds to wait for all tiles.

    Returns:
        PIL.Image.Image: The RGB mosaic.

    Raises:
        TimeoutError: If the tiles did not all arrive within ``timeout``.
        Exception: The first error raised by ``fetch``.
    """
    width, height = size
    origin_x, origin_y = origin
    tiles_per_axis = 2 ** zoom

    first_tx, last_tx = math.floor(origin_x / TILE_SIZE), math.floor((origin_x + width - 1) / TILE_SIZE)
    first_ty, last_ty = math.floor(origin_y / TILE_SIZE), math.floor((origin_y + height - 1) / TILE_SIZE)
    positions = [
        (tx, ty)
        for ty in range(first_ty, last_ty + 1) if 0 <= ty < tiles_per_axis
        for tx in range(first_tx, last_tx + 1)
    ]

    executor = ThreadPoolExecutor(max_workers=MAX_TILE_WORKERS)
    try:
        futures = {
            executor.submit(fetch, provider, zoom, tx % tiles_per_axis, ty): (tx, ty)
            for tx, ty in positions
        }
        done, not_done = wait(futures, timeout=timeout, return_when=FIRST_EXCEPTION)
        errors = [future.exception() for future in done if future.exception() is not None]
        if errors:
            raise errors[0]
        if not_done:
            raise TimeoutError(f"{len(not_done)} tile(s) not downloaded within {timeout:.1f} s")

        mosaic = Image.new("RGB", size)
        for future, (tx, ty) in futures.items():
            tile = future.result()
            if tile.size != (TILE_SIZE, TILE_SIZE):
                tile = tile.resize((TILE_SIZE, TILE_SIZE))
            mosaic.paste(tile, (round(tx * TILE_SIZE - origin_x), round(ty * TILE_SIZE - origin_y)))
        return mosaic
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def build_basemap(
    latitude: float,
    longitude: float,
    zoom: int,
    size: Tuple[int, int],
    providers: Sequence[TileProvider] = DEFAULT_PROVIDERS,
    fetch: Optional[Callable[[TileProvider, int, int, int], Image.Image]] = None,
) -> Tuple[Optional[Image.Image], Optional[TileProvider]]:
    """Builds a basemap image centered on a coordinate from keyless tile services.

    Providers are tried in order (Esri World Imagery first, OpenStreetMap as
    the alternative); the first one whose tiles all download successfully is
    used. The whole call is bounded by ``BASEMAP_DEADLINE_SECONDS`` (read at
    call time), shared by all providers: each provider only gets the time
    left, and once it runs out no further provider is tried. Network
    failures and timeouts never propagate: the function returns
    ``(None, None)`` so the caller can fall back to a neutral background.

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        zoom (int): Tile zoom level; callers must pick one that every
            provider in ``providers`` serves (see ``choose_zoom``).
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider]): Providers to try, in order.
        fetch (Callable, optional): Tile fetcher ``(provider, z, x, y) -> Image``;
            defaults to :func:`fetch_tile` (resolved at call time so tests can
            patch it).

    Returns:
        Tuple of (RGB basemap image, provider used), or ``(None, None)`` when
        no provider could serve the tiles within the deadline.
    """
    fetcher = fetch or fetch_tile
    center_x, center_y = latlon_to_pixel(latitude, longitude, zoom)
    origin = (center_x - size[0] / 2, center_y - size[1] / 2)
    deadline = time.monotonic() + BASEMAP_DEADLINE_SECONDS

    for provider in providers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log_warning("build_basemap: deadline exceeded, using neutral background")
            break
        try:
            mosaic = _build_mosaic(provider, origin, zoom, size, fetcher, timeout=remaining)
            log_debug(f"build_basemap: tiles from {provider.name} at z={zoom}")
            return mosaic, provider
        except Exception as e:
            log_warning(f"build_basemap: provider {provider.name} failed: {e}")
    return None, None
