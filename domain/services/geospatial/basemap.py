"""Basemap service — keyless (or API-key) XYZ raster tiles behind the confirmation image.

Builds a Web Mercator mosaic centered on a coordinate from public tile
services (Esri World Imagery, then OpenStreetMap). Which providers are tried
is read at call time from the environment (see ``resolve_providers``):

- ``PASTO10_BASEMAP_PROVIDERS``: comma-separated provider names, in order
  (default ``"esri,osm"``; ``""`` or ``"none"`` disables the basemap).
- ``ARCGIS_API_KEY``: when set, Esri tiles come from the ArcGIS Location
  Platform endpoint authenticated with the key (``token`` query parameter).
  The key is never written to the logs by this module. Note that urllib3
  logs every request URL (token included) at DEBUG level; this module does
  not change any global logger, so keep ``urllib3`` above DEBUG when a key
  is configured.

Downloads share one ``requests.Session`` (closed at interpreter exit) and are
bounded by a total deadline; network failures never propagate, the caller
falls back to a neutral background. A timed-out call may leave up to
``MAX_TILE_WORKERS`` download threads running in the background for at most
about the per-request timeout (~7 s); their results are discarded.
"""

import atexit
import math
import os
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from io import BytesIO
from typing import Callable, Optional, Sequence, Tuple
from urllib.parse import quote

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

PROVIDERS_ENV = "PASTO10_BASEMAP_PROVIDERS"
ARCGIS_API_KEY_ENV = "ARCGIS_API_KEY"
DEFAULT_PROVIDER_NAMES = "esri,osm"
_DISABLED_PROVIDER_NAMES = ("", "none")
_MASK = "***"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": USER_AGENT})
atexit.register(_SESSION.close)


@dataclass(frozen=True)
class TileProvider:
    """An XYZ raster tile service usable as a basemap.

    Attributes:
        name (str): Short identifier of the provider (e.g. "esri").
        url_template (str): Tile URL with ``{z}``, ``{x}`` and ``{y}`` placeholders.
        attribution (str): Attribution text that must be drawn on the image
            (may span several lines, separated by ``"\\n"``).
        max_zoom (int): Highest zoom level the provider serves.
        token (str, optional): API key appended as the ``token`` query
            parameter of every tile URL. Excluded from ``repr`` so it never leaks into logs.
    """

    name: str
    url_template: str
    attribution: str
    max_zoom: int
    token: Optional[str] = field(default=None, repr=False)

    def tile_url(self, zoom: int, x: int, y: int) -> str:
        """Builds the URL of one tile.

        Args:
            zoom (int): Tile zoom level.
            x (int): Tile column.
            y (int): Tile row.

        Returns:
            str: The tile URL, with ``token=<key>`` appended (``?`` or ``&``,
            depending on whether the template already has a query string)
            when the provider has a token.
        """
        url = self.url_template.format(z=zoom, x=x, y=y)
        if self.token:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}token={quote(self.token, safe='')}"
        return url

    def mask(self, text: str) -> str:
        """Replaces every occurrence of the provider token in a text.

        Args:
            text (str): Any text that may contain the token (e.g. an error message with the URL).

        Returns:
            str: The text with the token (raw and URL-quoted) replaced by ``"***"``.
        """
        if not self.token:
            return text
        return text.replace(quote(self.token, safe=""), _MASK).replace(self.token, _MASK)


ESRI_WORLD_IMAGERY = TileProvider(
    name="esri",
    url_template="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution="Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community\nPowered by Esri",
    max_zoom=18,
)

ESRI_KEYED_URL_TEMPLATE = "https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

OPENSTREETMAP = TileProvider(
    name="osm",
    url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution="© OpenStreetMap contributors",
    max_zoom=19,
)

DEFAULT_PROVIDERS: Tuple[TileProvider, ...] = (ESRI_WORLD_IMAGERY, OPENSTREETMAP)
_PROVIDERS_BY_NAME = {provider.name: provider for provider in DEFAULT_PROVIDERS}


def resolve_providers() -> Tuple[TileProvider, ...]:
    """Reads the basemap providers to try from the environment, at call time.

    ``PASTO10_BASEMAP_PROVIDERS`` lists provider names in order (default
    ``"esri,osm"``); unknown names are skipped with a warning, and ``""`` or
    ``"none"`` disables the basemap (neutral background, no download). When
    ``ARCGIS_API_KEY`` is set, the Esri provider switches to the ArcGIS
    Location Platform endpoint authenticated with that key.

    Returns:
        Tuple[TileProvider, ...]: Providers in the configured order (possibly empty).
    """
    raw = os.environ.get(PROVIDERS_ENV, DEFAULT_PROVIDER_NAMES).strip().lower()
    if raw in _DISABLED_PROVIDER_NAMES:
        return ()

    api_key = os.environ.get(ARCGIS_API_KEY_ENV, "").strip()
    providers = []
    for name in (part.strip() for part in raw.split(",")):
        provider = _PROVIDERS_BY_NAME.get(name)
        if provider is None:
            log_warning(f"resolve_providers: unknown basemap provider '{name}' ignored")
            continue
        if provider is ESRI_WORLD_IMAGERY and api_key:
            provider = replace(provider, url_template=ESRI_KEYED_URL_TEMPLATE, token=api_key)
        providers.append(provider)
    return tuple(providers)


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

    The URL comes from ``TileProvider.tile_url`` (token included when the
    provider has one). Uses the module-level ``requests.Session``
    (connection reuse, with the ``USER_AGENT`` header required by the tile
    usage policies) and the ``REQUEST_TIMEOUT`` (connect, read) timeouts.

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
    url = provider.tile_url(zoom, x, y)
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

    Tiles are downloaded in parallel by a pool created per call (up to
    ``MAX_TILE_WORKERS`` threads). The function never blocks past
    ``timeout``: on return or failure the pool is shut down without waiting
    and queued downloads are cancelled. Residual threads: downloads already
    running cannot be interrupted, so up to ``MAX_TILE_WORKERS`` threads may
    keep running in the background until their request ends — at most the
    connect + read timeout of ``REQUEST_TIMEOUT`` (~7 s) — and their results
    are discarded. Rows outside the world are left blank.

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
    providers: Optional[Sequence[TileProvider]] = None,
    fetch: Optional[Callable[[TileProvider, int, int, int], Image.Image]] = None,
) -> Tuple[Optional[Image.Image], Optional[TileProvider]]:
    """Builds a basemap image centered on a coordinate from tile services.

    Providers are tried in order (by default Esri World Imagery first,
    OpenStreetMap as the alternative); the first one whose tiles all download successfully is
    used. The whole call is bounded by ``BASEMAP_DEADLINE_SECONDS`` (read at
    call time), shared by all providers: each provider only gets the time
    left, and once it runs out no further provider is tried. Network
    failures and timeouts never propagate: the function returns
    ``(None, None)`` so the caller can fall back to a neutral background.
    Warnings never contain a provider token (see ``TileProvider.mask``).

    Args:
        latitude (float): Center latitude in decimal degrees.
        longitude (float): Center longitude in decimal degrees.
        zoom (int): Tile zoom level; callers must pick one that every
            provider in ``providers`` serves (see ``choose_zoom``).
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider], optional): Providers to try, in
            order; defaults to :func:`resolve_providers` (environment). An
            empty sequence returns ``(None, None)`` without any download.
        fetch (Callable, optional): Tile fetcher ``(provider, z, x, y) -> Image``;
            defaults to :func:`fetch_tile` (resolved at call time so tests can
            patch it).

    Returns:
        Tuple of (RGB basemap image, provider used), or ``(None, None)`` when
        no provider could serve the tiles within the deadline.
    """
    fetcher = fetch or fetch_tile
    if providers is None:
        providers = resolve_providers()
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
            log_warning(f"build_basemap: provider {provider.name} failed: {provider.mask(str(e))}")
    return None, None
