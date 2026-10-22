"""Biomass map service — renders the dry-matter production of an area over a basemap.

Same composition as the pasture map (``pasture_image``): tile basemap, the
Earth Engine image served by ``getThumbURL`` in the same Web Mercator frame,
the buffer outline, a scale bar and the attributions, with a continuous color
scale (colorbar in t DM/ha stretched between the 2nd and 98th percentile of
the pixels) instead of a class legend. The overlay is mandatory: if Earth
Engine cannot deliver it the error propagates to the tool.
"""

from typing import Optional, Sequence, Tuple

import ee
from PIL import Image, ImageDraw

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import Feature
from domain.services.geospatial.basemap import (
    ESRI_WORLD_IMAGERY,
    TileProvider,
    build_basemap,
    choose_zoom,
    latlon_to_pixel,
    meters_per_pixel,
    resolve_providers,
)
from domain.services.geospatial.image import (
    IMAGE_SIZE,
    NEUTRAL_BACKGROUND,
    RenderedImage,
    draw_attribution,
    draw_center_marker,
    draw_corner_label,
    draw_scale_bar,
    encode,
    load_font,
    parse_center,
    project_ring,
    scale_bar_box,
)
from domain.services.geospatial.pasture_image import (
    blend_overlay,
    download_thumbnail,
    draw_outline,
    mercator_bounds,
)

PALETTE = ["f7fcb9", "addd8e", "78c679", "31a354", "006837"]
_ATTRIBUTION = {
    BiomassSource.GPW_ANNUAL: "Dry matter: Global Pasture Watch (estimate)",
    BiomassSource.TIME2GRAZE: "Dry matter: Time2Graze + Global Pasture Watch mask (estimate)",
}
_BAR_SIZE = (150, 14)


def _fetch_overlay(
    image: "ee.Image", stats: BiomassStats, bounds: Tuple[float, float, float, float], size: Tuple[int, int],
) -> Image.Image:
    """Downloads the biomass image as a colored transparent overlay (network, Earth Engine).

    Args:
        image (ee.Image): Band ``biomass`` (t DM/ha), masked to productive pasture.
        stats (BiomassStats): Gives the color stretch (2nd to 98th percentile) and the resolution.
        bounds: EPSG:3857 bounds of the view in meters.
        size (Tuple[int, int]): Output size in pixels.

    Returns:
        PIL.Image.Image: RGBA overlay with exactly ``size`` pixels.

    Raises:
        Exception: Earth Engine or HTTP failure.
    """
    low, high = stats.p02_t_ha, max(stats.p98_t_ha, stats.p02_t_ha + 0.1)
    visual = image.visualize(min=low, max=high, palette=PALETTE)
    return download_thumbnail(visual, bounds, size, stats.scale_m)


def _draw_colorbar(image: Image.Image, stats: BiomassStats, bottom: int) -> Image.Image:
    """Draws the colorbar (t DM/ha) on the bottom-left corner, ending above ``bottom``.

    Args:
        image (PIL.Image.Image): Image to draw on (a copy is returned).
        stats (BiomassStats): Gives the value range of the bar.
        bottom (int): Pixel row the colorbar must stay above.

    Returns:
        PIL.Image.Image: New image with the colorbar.
    """
    font = load_font(max(12, int(image.size[1] * 0.026)))
    padding = 6
    width, height = _BAR_SIZE
    low, high = stats.p02_t_ha, max(stats.p98_t_ha, stats.p02_t_ha + 0.1)
    title, low_text, high_text = "Dry matter (t DM/ha)", f"{low:.1f}", f"{high:.1f}"
    box_width = max(width + 2 * padding, int(font.getlength(title)) + 2 * padding)
    box_height = height + 2 * padding + 2 * font.size + 4
    left, top = max(10, int(image.size[0] * 0.03)), bottom - box_height

    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([left, top, left + box_width, top + box_height], fill=(0, 0, 0, 150))
    draw.text((left + padding, top + padding), title, font=font, fill=(255, 255, 255, 255))
    bar_top = top + padding + font.size + 2
    colors = [tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) for color in PALETTE]
    for x in range(width):
        position = x / max(1, width - 1) * (len(colors) - 1)
        index = min(int(position), len(colors) - 2)
        mix = position - index
        pixel = tuple(round(colors[index][c] * (1 - mix) + colors[index + 1][c] * mix) for c in range(3))
        draw.line([(left + padding + x, bar_top), (left + padding + x, bar_top + height)], fill=pixel + (255,))
    label_top = bar_top + height + 2
    draw.text((left + padding, label_top), low_text, font=font, fill=(255, 255, 255, 255))
    draw.text((left + padding + width - font.getlength(high_text), label_top), high_text, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, layer).convert("RGB")


def render_biomass_map(
    feature: Feature,
    biomass_image: "ee.Image",
    stats: BiomassStats,
    size: Tuple[int, int] = IMAGE_SIZE,
    providers: Optional[Sequence[TileProvider]] = None,
) -> RenderedImage:
    """Renders the biomass map of a registered area.

    Args:
        feature (Feature): Registered buffer area (metadata ``center``, ``radius``).
        biomass_image (ee.Image): Biomass image (band ``biomass``, t DM/ha) of ``stats``.
        stats (BiomassStats): Measured figures, period and source for the labels.
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
    image = blend_overlay(background, _fetch_overlay(biomass_image, stats, bounds, size))
    image = draw_outline(image, project_ring(feature.get_coords()[0][0], (latitude, longitude), zoom, size))
    image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
    mpp = meters_per_pixel(latitude, zoom)
    image = draw_scale_bar(image, mpp)
    image = _draw_colorbar(image, stats, bottom=scale_bar_box(size, mpp)[1] - 8)
    image = draw_corner_label(
        image, f"{stats.period_label} · Dry matter {stats.mean_t_ha:g} t/ha · {stats.total_t:g} t",
    )
    lines = [_ATTRIBUTION[stats.source]] + ([provider.attribution] if provider is not None else [])
    image = draw_attribution(image, "\n".join(lines))
    return encode(image, with_basemap=provider is not None)
