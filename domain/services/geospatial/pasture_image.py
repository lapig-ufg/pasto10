"""Pasture map service — renders the pasture classes of an area over a basemap.

The image is the tile basemap of ``basemap.build_basemap`` with the Earth
Engine class image (served by ``getThumbURL`` in the same Web Mercator frame)
composited on top, plus the buffer outline, a scale bar, the year and pasture
area label, a legend and the attributions of the basemap and of the pasture
data. When the basemap tiles are unavailable the map is drawn over the
neutral background, like the confirmation image. The overlay is mandatory:
if Earth Engine cannot deliver it the error propagates to the tool.
"""

import math
from io import BytesIO
from typing import Optional, Sequence, Tuple

import ee
import requests
from PIL import Image, ImageDraw

from domain.schemas.feature import Feature
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.basemap import (
    ESRI_WORLD_IMAGERY,
    TILE_SIZE,
    WEB_MERCATOR_RADIUS_METERS,
    TileProvider,
    build_basemap,
    choose_zoom,
    latlon_to_pixel,
    meters_per_pixel,
    resolve_providers,
)
from domain.services.geospatial.image import (
    BUFFER_OUTLINE,
    IMAGE_SIZE,
    NEUTRAL_BACKGROUND,
    RenderedImage,
    encode,
    load_font,
    draw_attribution,
    draw_center_marker,
    draw_corner_label,
    draw_scale_bar,
    parse_center,
    project_ring,
    scale_bar_box,
)

CULTIVATED_COLOR = "7fc241"
NATURAL_COLOR = "1b6b2f"
OVERLAY_OPACITY = 190
THUMBNAIL_TIMEOUT = (5, 60)
_LEGEND_ENTRIES = (("Cultivated grassland", CULTIVATED_COLOR), ("Natural/semi-natural grassland", NATURAL_COLOR))
_DATA_ATTRIBUTION = {
    PastureSource.GPW: "Pasture: Global Pasture Watch",
    PastureSource.CLASSIFICATION: "Pasture: experimental estimate, not an official map\nGoogle Satellite Embedding (AlphaEarth) + Global Pasture Watch",
}


def mercator_bounds(center_pixel: Tuple[float, float], zoom: int, size: Tuple[int, int]) -> Tuple[float, float, float, float]:
    """Returns the EPSG:3857 bounds (xmin, ymin, xmax, ymax, meters) of an image of ``size`` pixels."""
    world_size = TILE_SIZE * 2 ** zoom
    half_world = math.pi * WEB_MERCATOR_RADIUS_METERS
    to_x = lambda px: (px / world_size - 0.5) * 2 * half_world
    to_y = lambda py: (0.5 - py / world_size) * 2 * half_world
    cx, cy = center_pixel
    return to_x(cx - size[0] / 2), to_y(cy + size[1] / 2), to_x(cx + size[0] / 2), to_y(cy - size[1] / 2)


def _fetch_overlay(
    classification_image: "ee.Image",
    bounds: Tuple[float, float, float, float],
    size: Tuple[int, int],
    scale: float,
) -> Image.Image:
    """Downloads the class image as a transparent RGBA overlay (network, Earth Engine).

    Class 0 is masked out; class 1 and 2 use the legend colors.

    Args:
        classification_image (ee.Image): Image with band ``cls``.
        bounds: EPSG:3857 bounds of the view in meters.
        size (Tuple[int, int]): Output size in pixels.
        scale (float): Native resolution of the classes in meters. The thumbnail
            is requested at this resolution (far cheaper than classifying at
            screen resolution) and enlarged to ``size`` by nearest neighbor,
            which keeps the class pixels exactly as measured.

    Returns:
        PIL.Image.Image: RGBA overlay with exactly ``size`` pixels.

    Raises:
        Exception: Earth Engine or HTTP failure.
    """
    visual = classification_image.updateMask(classification_image.gt(0)).visualize(
        min=1, max=2, palette=[CULTIVATED_COLOR, NATURAL_COLOR],
    )
    return download_thumbnail(visual, bounds, size, scale)


def download_thumbnail(visual: "ee.Image", bounds: Tuple[float, float, float, float], size: Tuple[int, int], scale: float) -> Image.Image:
    """Downloads a visualized Earth Engine image as an RGBA overlay of the view (network).

    Args:
        visual (ee.Image): Image already visualized (RGB(A) bands).
        bounds: EPSG:3857 bounds of the view in meters.
        size (Tuple[int, int]): Output size in pixels.
        scale (float): Native resolution of the data in meters. The thumbnail
            is requested at this resolution (never above ``size``) and enlarged
            by nearest neighbor.

    Returns:
        PIL.Image.Image: RGBA overlay with exactly ``size`` pixels.

    Raises:
        Exception: Earth Engine or HTTP failure.
    """
    region = ee.Geometry.Rectangle(list(bounds), proj="EPSG:3857", geodesic=False)
    dimensions = (
        max(1, min(size[0], round((bounds[2] - bounds[0]) / scale))),
        max(1, min(size[1], round((bounds[3] - bounds[1]) / scale))),
    )
    url = visual.getThumbURL({
        "region": region, "dimensions": f"{dimensions[0]}x{dimensions[1]}", "format": "png", "crs": "EPSG:3857",
    })
    response = requests.get(url, timeout=THUMBNAIL_TIMEOUT)
    response.raise_for_status()
    overlay = Image.open(BytesIO(response.content)).convert("RGBA")
    return overlay.resize(size, Image.NEAREST) if overlay.size != tuple(size) else overlay


def blend_overlay(background: Image.Image, overlay: Image.Image) -> Image.Image:
    """Composites the overlay on the background, scaling its alpha to ``OVERLAY_OPACITY``."""
    alpha = overlay.getchannel("A").point(lambda value: min(value, OVERLAY_OPACITY))
    overlay = overlay.copy()
    overlay.putalpha(alpha)
    return Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB")


def draw_outline(image: Image.Image, points: list) -> Image.Image:
    """Draws the buffer outline (no fill, so the pasture stays visible)."""
    outlined = image.copy()
    ImageDraw.Draw(outlined).line(points + [points[0]], fill=BUFFER_OUTLINE, width=3, joint="curve")
    return outlined


def _draw_legend(image: Image.Image, bottom: int) -> Image.Image:
    """Draws the two-class legend on the bottom-left corner, ending above ``bottom``.

    Args:
        image (PIL.Image.Image): Image to draw on (a copy is returned).
        bottom (int): Pixel row the legend must stay above.

    Returns:
        PIL.Image.Image: New image with the legend.
    """
    font = load_font(max(12, int(image.size[1] * 0.026)))
    swatch, gap, padding = font.size, 6, 6
    height = len(_LEGEND_ENTRIES) * (swatch + gap) - gap + 2 * padding
    width = padding * 3 + swatch + int(max(font.getlength(label) for label, _ in _LEGEND_ENTRIES))
    left, top = max(10, int(image.size[0] * 0.03)), bottom - height

    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([left, top, left + width, top + height], fill=(0, 0, 0, 150))
    for index, (label, color) in enumerate(_LEGEND_ENTRIES):
        y = top + padding + index * (swatch + gap)
        draw.rectangle([left + padding, y, left + padding + swatch, y + swatch], fill=f"#{color}", outline=(255, 255, 255, 255))
        draw.text((left + 2 * padding + swatch, y), label, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, layer).convert("RGB")


def render_pasture_map(
    feature: Feature,
    classification_image: "ee.Image",
    stats: PastureAreaStats,
    scale: float,
    size: Tuple[int, int] = IMAGE_SIZE,
    providers: Optional[Sequence[TileProvider]] = None,
) -> RenderedImage:
    """Renders the pasture map of a registered area.

    Args:
        feature (Feature): Registered buffer area (metadata ``center``, ``radius``).
        classification_image (ee.Image): Class image (band ``cls``) of ``stats.year``.
        stats (PastureAreaStats): Measured areas, year and source for the labels.
        scale (float): Native resolution of the classes in meters (30 for Global Pasture Watch, 10 for the classification).
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider], optional): Tile providers; defaults
            to ``resolve_providers()``.

    Returns:
        RenderedImage: JPEG over a tile basemap, PNG over the neutral background.

    Raises:
        ValueError: If the feature lacks valid ``center``/``radius`` metadata.
        Exception: Earth Engine or HTTP failure while fetching the overlay.
    """
    latitude, longitude = parse_center(feature)
    radius = float(feature.get_metadata("radius", 0))
    if radius <= 0:
        raise ValueError(f"Feature {feature.id} has no valid 'radius' metadata.")

    if providers is None:
        providers = resolve_providers()
    max_zoom = min((p.max_zoom for p in providers), default=ESRI_WORLD_IMAGERY.max_zoom)
    zoom = choose_zoom(latitude, radius, size, max_zoom=max_zoom)
    background, provider = build_basemap(latitude, longitude, zoom, size, providers=providers)
    if background is None:
        background = Image.new("RGB", size, NEUTRAL_BACKGROUND)

    bounds = mercator_bounds(latlon_to_pixel(latitude, longitude, zoom), zoom, size)
    image = blend_overlay(background, _fetch_overlay(classification_image, bounds, size, scale))
    image = draw_outline(image, project_ring(feature.get_coords()[0][0], (latitude, longitude), zoom, size))
    image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
    mpp = meters_per_pixel(latitude, zoom)
    image = draw_scale_bar(image, mpp)
    image = _draw_legend(image, bottom=scale_bar_box(size, mpp)[1] - 8)
    estimate = " · Estimate" if stats.source is PastureSource.CLASSIFICATION else ""
    image = draw_corner_label(image, f"{stats.year}{estimate} · Pasture {stats.pasture_ha:g} ha of {stats.area_ha:g} ha")
    lines = [_DATA_ATTRIBUTION[stats.source]] + ([provider.attribution] if provider is not None else [])
    image = draw_attribution(image, "\n".join(lines))
    return encode(image, with_basemap=provider is not None)
