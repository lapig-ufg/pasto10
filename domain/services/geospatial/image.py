"""Confirmation image service — renders a buffer area over a basemap.

Draws the buffer circle, its center, a scale bar, a two-line label
(coordinate; radius and area) and the basemap attribution on top of the
tiles from ``basemap.build_basemap``. Over a tile basemap the image is
encoded as JPEG (photographic content, much smaller); over the neutral
fallback background it is encoded as PNG. The result is returned as a
``RenderedImage`` (bytes plus format and MIME type) so tools can pass it on
unchanged.
"""

from dataclasses import dataclass
from io import BytesIO
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

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
from domain.services.geospatial.geometry import haversine_distance, normalize_longitude

IMAGE_SIZE = (640, 640)
NEUTRAL_BACKGROUND = (226, 226, 220)
BUFFER_OUTLINE = (255, 214, 0, 255)
BUFFER_FILL = (255, 214, 0, 70)
JPEG_QUALITY = 80
MIN_ATTRIBUTION_FONT_PX = 12
_ATTRIBUTION_PADDING = 4
_SCALE_BAR_STEPS_METERS = (10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000)


@dataclass(frozen=True)
class RenderedImage:
    """An encoded confirmation image ready to be attached to a tool result.

    Attributes:
        content (bytes): The encoded image bytes.
        format (str): Image format name (``"jpeg"`` or ``"png"``).
        mime_type (str): Matching MIME type (``"image/jpeg"`` or ``"image/png"``).
    """

    content: bytes
    format: str
    mime_type: str


def load_font(size: int) -> ImageFont.ImageFont:
    """Returns Pillow's bundled scalable font.

    Args:
        size (int): Font size in pixels.

    Returns:
        ImageFont.ImageFont: The font, shared by every image renderer.
    """
    return ImageFont.load_default(size=size)


def parse_center(feature: Feature) -> Tuple[float, float]:
    """Reads the buffer center from the feature's ``center`` metadata.

    Args:
        feature (Feature): A buffer area feature (metadata ``center`` = ``"lat, lon"``).

    Returns:
        Tuple[float, float]: (latitude, longitude) of the center.

    Raises:
        ValueError: If the ``center`` metadata is missing or malformed.
    """
    center = feature.get_metadata("center")
    if center is None:
        raise ValueError(f"Feature {feature.id} has no 'center' metadata.")
    latitude, longitude = (float(part) for part in str(center).split(","))
    return latitude, longitude


def project_ring(
    ring: Sequence[Sequence[float]],
    center: Tuple[float, float],
    zoom: int,
    size: Tuple[int, int],
) -> List[Tuple[float, float]]:
    """Projects a [lon, lat] ring to pixel positions in an image centered on ``center``.

    Longitudes are unwrapped around the center so rings crossing the
    antimeridian are drawn as one continuous shape.

    Args:
        ring: Sequence of [lon, lat] pairs.
        center (Tuple[float, float]): (latitude, longitude) at the image center.
        zoom (int): Web Mercator zoom level of the image.
        size (Tuple[int, int]): Image (width, height) in pixels.

    Returns:
        List of (x, y) pixel positions.
    """
    center_lat, center_lon = center
    center_x, center_y = latlon_to_pixel(center_lat, center_lon, zoom)
    origin_x, origin_y = center_x - size[0] / 2, center_y - size[1] / 2

    points = []
    for lon, lat in ring:
        unwrapped_lon = center_lon + normalize_longitude(lon - center_lon)
        x, y = latlon_to_pixel(lat, unwrapped_lon, zoom)
        points.append((x - origin_x, y - origin_y))
    return points


def draw_buffer_overlay(image: Image.Image, points: List[Tuple[float, float]]) -> Image.Image:
    """Draws the buffer polygon (semi-transparent fill + solid outline) on the image.

    Args:
        image (PIL.Image.Image): Background image (any mode).
        points: Polygon vertices in pixel coordinates.

    Returns:
        PIL.Image.Image: New RGB image with the overlay composited.
    """
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.polygon(points, fill=BUFFER_FILL)
    draw.line(points + [points[0]], fill=BUFFER_OUTLINE, width=3, joint="curve")
    return Image.alpha_composite(base, overlay).convert("RGB")


def draw_center_marker(image: Image.Image, position: Tuple[float, float], radius: int = 6) -> Image.Image:
    """Draws a red dot with a white ring marking the buffer center.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        position (Tuple[float, float]): Marker position in pixels.
        radius (int): Marker radius in pixels.

    Returns:
        PIL.Image.Image: New image with the marker drawn.
    """
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    x, y = position
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(220, 30, 30), outline=(255, 255, 255), width=2)
    return marked


def _scale_bar_length(meters_per_px: float, max_pixels: float) -> int:
    """Returns the largest round scale-bar length (m) that fits in ``max_pixels``."""
    fitting = [step for step in _SCALE_BAR_STEPS_METERS if step / meters_per_px <= max_pixels]
    return fitting[-1] if fitting else _SCALE_BAR_STEPS_METERS[0]


def _format_distance(meters: int) -> str:
    """Formats a distance as ``"<n> m"`` or ``"<n> km"``."""
    return f"{meters / 1000:g} km" if meters >= 1000 else f"{meters} m"


def _scale_bar_layout(size: Tuple[int, int], meters_per_px: float):
    """Computes the scale bar geometry shared by ``draw_scale_bar`` and ``scale_bar_box``.

    Args:
        size (Tuple[int, int]): Image (width, height) in pixels.
        meters_per_px (float): Ground resolution of the image at its center.

    Returns:
        Tuple (x0, x1, y, length_m, font): bar start/end x, bar top y, bar
        length in meters and the label font.
    """
    width, height = size
    margin = max(10, int(width * 0.03))
    length_m = _scale_bar_length(meters_per_px, width * 0.3)
    length_px = round(length_m / meters_per_px)
    font = load_font(max(12, int(height * 0.028)))
    return margin, margin + length_px, height - margin - 6, length_m, font


def scale_bar_box(size: Tuple[int, int], meters_per_px: float) -> Tuple[int, int, int, int]:
    """Returns the pixel box covered by the scale bar and its label.

    Args:
        size (Tuple[int, int]): Image (width, height) in pixels.
        meters_per_px (float): Ground resolution of the image at its center.

    Returns:
        Tuple[int, int, int, int]: (left, top, right, bottom), including the
        black frame of the bar and the stroke of the label.
    """
    x0, x1, y, length_m, font = _scale_bar_layout(size, meters_per_px)
    stroke = 2
    left, top, right, _ = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
        (x0, y - 6), _format_distance(length_m), font=font, anchor="ls", stroke_width=stroke,
    )
    return min(left, x0 - 2), top, max(right, x1 + 2), y + 8


def draw_scale_bar(image: Image.Image, meters_per_px: float) -> Image.Image:
    """Draws a round-length scale bar on the bottom-left corner of the image.

    Its footprint is given by ``scale_bar_box``.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        meters_per_px (float): Ground resolution of the image at its center.

    Returns:
        PIL.Image.Image: New image with the scale bar and its label.
    """
    x0, x1, y, length_m, font = _scale_bar_layout(image.size, meters_per_px)

    scaled = image.copy()
    draw = ImageDraw.Draw(scaled)
    draw.rectangle([x0 - 2, y - 2, x1 + 2, y + 8], fill=(0, 0, 0))
    draw.rectangle([x0, y, x1, y + 6], fill=(255, 255, 255))
    draw.text(
        (x0, y - 6), _format_distance(length_m), font=font, anchor="ls",
        fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0),
    )
    return scaled


def draw_corner_label(image: Image.Image, text: str) -> Image.Image:
    """Draws a label (white text with black stroke) on the top-left corner.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        text (str): Label text.

    Returns:
        PIL.Image.Image: New image with the label drawn.
    """
    width, height = image.size
    margin = max(8, int(width * 0.02))
    labeled = image.copy()
    draw = ImageDraw.Draw(labeled)
    draw.text(
        (margin, margin), text, font=load_font(max(14, int(height * 0.032))),
        fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0),
    )
    return labeled


def attribution_font_size(size: Tuple[int, int]) -> int:
    """Returns the attribution font size (px) for an image size (never below 12 px).

    Args:
        size (Tuple[int, int]): Image (width, height) in pixels.

    Returns:
        int: Font size in pixels.
    """
    return max(MIN_ATTRIBUTION_FONT_PX, int(size[1] * 0.02))


def _attribution_text_bbox(size: Tuple[int, int], text: str) -> Tuple[int, int, int, int]:
    """Returns the bbox of the (possibly multi-line) attribution text drawn at (0, 0)."""
    font = load_font(attribution_font_size(size))
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).multiline_textbbox((0, 0), text, font=font)


def attribution_box(size: Tuple[int, int], text: str) -> Tuple[int, int, int, int]:
    """Returns the pixel box of the attribution strip (bottom-right corner).

    Args:
        size (Tuple[int, int]): Image (width, height) in pixels.
        text (str): Attribution text (lines separated by ``"\\n"``).

    Returns:
        Tuple[int, int, int, int]: (left, top, right, bottom) of the strip,
        flush with the bottom-right corner of the image.
    """
    width, height = size
    left, top, right, bottom = _attribution_text_bbox(size, text)
    return (
        width - (right - left) - 2 * _ATTRIBUTION_PADDING,
        height - (bottom - top) - 2 * _ATTRIBUTION_PADDING,
        width,
        height,
    )


def draw_attribution(image: Image.Image, text: str) -> Image.Image:
    """Draws the basemap attribution on a translucent strip at the bottom-right.

    The strip footprint is given by ``attribution_box``; multi-line texts
    are right-aligned.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        text (str): Attribution required by the tile provider.

    Returns:
        PIL.Image.Image: New RGB image with the attribution drawn.
    """
    font = load_font(attribution_font_size(image.size))
    left, top, _, _ = _attribution_text_bbox(image.size, text)
    box = attribution_box(image.size, text)

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle(box, fill=(255, 255, 255, 180))
    draw.multiline_text(
        (box[0] + _ATTRIBUTION_PADDING - left, box[1] + _ATTRIBUTION_PADDING - top),
        text, font=font, fill=(40, 40, 40, 255), align="right",
    )
    return Image.alpha_composite(base, overlay).convert("RGB")


def encode(image: Image.Image, with_basemap: bool) -> RenderedImage:
    """Encodes the image as JPEG (over tiles) or PNG (over the neutral background)."""
    stream = BytesIO()
    if with_basemap:
        image.save(stream, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return RenderedImage(content=stream.getvalue(), format="jpeg", mime_type="image/jpeg")
    image.save(stream, format="PNG")
    return RenderedImage(content=stream.getvalue(), format="png", mime_type="image/png")


def render_buffer_image(
    feature: Feature,
    size: Tuple[int, int] = IMAGE_SIZE,
    providers: Optional[Sequence[TileProvider]] = None,
    origin_label: Optional[str] = None,
) -> RenderedImage:
    """Renders the confirmation image of a buffer area.

    The image is centered on the buffer center at the highest zoom where the
    circle fits comfortably. The background comes from tile services (by
    default Esri World Imagery, then OpenStreetMap; see
    ``basemap.resolve_providers``); when the network fails or the basemap is
    disabled the image is still produced over a neutral background, so the
    registration never breaks because of the basemap. On top of the
    background it draws the buffer (semi-transparent fill and outline), the
    center marker, a scale bar, a two-line label (``"lat, lon"`` and
    ``"Radius <r> m · <a> ha"``) and — only when a basemap was used — the
    provider attribution.

    Args:
        feature (Feature): A buffer area feature (metadata ``center`` and ``radius``).
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider], optional): Tile providers to try,
            in order; defaults to ``resolve_providers()`` (read at call time).
        origin_label (str, optional): Text added as a third label line (e.g. "approximate area, not a real
            property boundary").

    Returns:
        RenderedImage: JPEG (quality 80) over a tile basemap, PNG over the
        neutral background.

    Raises:
        ValueError: If the feature lacks valid ``center``/``radius`` metadata.
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

    points = project_ring(feature.get_coords()[0][0], (latitude, longitude), zoom, size)
    image = draw_buffer_overlay(background, points)
    image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
    image = draw_scale_bar(image, meters_per_pixel(latitude, zoom))
    image = draw_corner_label(
        image,
        f"{latitude:.5f}, {longitude:.5f}\nRadius {radius:g} m · {feature.total_area:g} ha" + (f"\n{origin_label}" if origin_label else ""),
    )
    if provider is not None:
        image = draw_attribution(image, provider.attribution)
    return encode(image, with_basemap=provider is not None)


CANDIDATE_COLORS = ((255, 214, 0), (0, 200, 255), (255, 105, 180), (140, 255, 90), (255, 140, 0))
_CANDIDATE_FILL_ALPHA = 55
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


def _polygon_rings(candidate) -> list:
    """Returns the outer rings (lon, lat lists) of a candidate geometry."""
    from domain.services.property_sources import geometry_utils as geo

    shape = geo.polygonal_part(geo.from_geojson_geometry(candidate.geometry))
    polygons = shape.geoms if hasattr(shape, "geoms") else [shape]
    return [list(polygon.exterior.coords) for polygon in polygons]


def _basemap_for(latitude: float, longitude: float, radius: float, size: Tuple[int, int], providers):
    """Builds the basemap fitting a circle of ``radius`` meters; returns (zoom, background, provider)."""
    if providers is None:
        providers = resolve_providers()
    max_zoom = min((p.max_zoom for p in providers), default=ESRI_WORLD_IMAGERY.max_zoom)
    zoom = choose_zoom(latitude, radius, size, max_zoom=max_zoom)
    background, provider = build_basemap(latitude, longitude, zoom, size, providers=providers)
    if background is None:
        background = Image.new("RGB", size, NEUTRAL_BACKGROUND)
    return zoom, background, provider


def _draw_polygons(image: Image.Image, rings_by_color: Sequence, center: Tuple[float, float], zoom: int, size: Tuple[int, int]) -> Image.Image:
    """Draws filled, outlined rings (each ``(rings, rgb)``) on the image."""
    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for rings, color in rings_by_color:
        for ring in rings:
            points = project_ring(ring, center, zoom, size)
            draw.polygon(points, fill=color + (_CANDIDATE_FILL_ALPHA,))
            draw.line(points + [points[0]], fill=color + (255,), width=3, joint="curve")
    return Image.alpha_composite(base, layer).convert("RGB")


def _draw_badge(image: Image.Image, position: Tuple[float, float], text: str, color: Tuple[int, int, int]) -> Image.Image:
    """Draws a numbered round badge centered on ``position``."""
    badged = image.copy()
    draw = ImageDraw.Draw(badged)
    x, y = position
    radius = 15
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color, outline=(0, 0, 0), width=2)
    draw.text((x, y), text, font=load_font(20), fill=(0, 0, 0), anchor="mm")
    return badged


DEMO_MARK = "DEMO - synthetic parcels, not real boundaries"


def draw_demo_mark(image: Image.Image) -> Image.Image:
    """Draws the "DEMO - synthetic parcels" banner along the top-right edge (demo registry candidates).

    Args:
        image (PIL.Image.Image): Image to draw on (a copy is returned).

    Returns:
        PIL.Image.Image: New image with the banner.
    """
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    font = load_font(max(13, int(image.size[1] * 0.026)))
    width = int(font.getlength(DEMO_MARK)) + 14
    left = image.size[0] - width
    draw.rectangle([left, 0, image.size[0], font.size + 10], fill=(170, 0, 0))
    draw.text((left + 7, 5), DEMO_MARK, font=font, fill=(255, 255, 255))
    return marked


def _draw_legend(image: Image.Image, lines: Sequence[str]) -> Image.Image:
    """Draws a caption block (one line per candidate: number, id, area, status) on the top-left corner.

    Args:
        image (PIL.Image.Image): Image to draw on (a copy is returned).
        lines (Sequence[str]): Caption lines, already numbered.

    Returns:
        PIL.Image.Image: New image with the translucent caption block.
    """
    font = load_font(max(13, int(image.size[1] * 0.026)))
    margin = 8
    width = max(int(font.getlength(line)) for line in lines) + 12
    height = len(lines) * (font.size + 4) + 8
    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([margin, margin, margin + width, margin + height], fill=(0, 0, 0, 150))
    for index, line in enumerate(lines):
        draw.text((margin + 6, margin + 6 + index * (font.size + 4)), line, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, layer).convert("RGB")


def _badge_anchor(candidate, center: Tuple[float, float], zoom: int, size: Tuple[int, int]) -> Tuple[float, float]:
    """Pixel position of a candidate's number badge: a point guaranteed to lie inside its polygon."""
    from domain.services.property_sources import geometry_utils as geo

    point = geo.polygonal_part(geo.from_geojson_geometry(candidate.geometry)).representative_point()
    return project_ring([(point.x, point.y)], center, zoom, size)[0]


def _is_demo(candidates) -> bool:
    """True when any candidate is a synthetic demo registry parcel."""
    return any("synthetic_demo" in candidate.warnings for candidate in candidates)


def render_candidates_image(
    candidates: Sequence, latitude: float, longitude: float, size: Tuple[int, int] = IMAGE_SIZE,
    providers: Optional[Sequence[TileProvider]] = None, captions: Optional[Sequence[str]] = None,
) -> RenderedImage:
    """Renders the numbered candidate polygons around a searched point over the basemap.

    Each candidate gets a color, an outline, a translucent fill and a numbered badge (1 = first of the list);
    the searched point is a red marker. The attribution of the basemap and, when a candidate comes from
    OpenStreetMap, the OpenStreetMap credit are drawn.

    Args:
        candidates (Sequence[PropertyCandidate]): The candidates, in the order shown to the user.
        latitude (float): Latitude of the searched point.
        longitude (float): Longitude of the searched point.
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider], optional): Tile providers; defaults to ``resolve_providers()``.
        captions (Sequence[str], optional): One caption per candidate (e.g. id, area and status), drawn as a
            numbered legend block so the number on each polygon can be matched to its data.

    Returns:
        RenderedImage: JPEG over a tile basemap, PNG over the neutral background.
    """
    rings = [_polygon_rings(candidate) for candidate in candidates]
    radius = max(
        [50.0] + [haversine_distance(latitude, longitude, lat, lon) for group in rings for ring in group for lon, lat in ring]
    ) * 1.15
    zoom, background, provider = _basemap_for(latitude, longitude, radius, size, providers)
    center = (latitude, longitude)
    image = _draw_polygons(background, [(group, CANDIDATE_COLORS[i % len(CANDIDATE_COLORS)]) for i, group in enumerate(rings)], center, zoom, size)
    for index, candidate in enumerate(candidates):
        image = _draw_badge(image, _badge_anchor(candidate, center, zoom, size), str(index + 1), CANDIDATE_COLORS[index % len(CANDIDATE_COLORS)])
    if captions:
        image = _draw_legend(image, [f"{index + 1}  {text}" for index, text in enumerate(captions)])
    image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
    image = draw_scale_bar(image, meters_per_pixel(latitude, zoom))
    if _is_demo(candidates):
        image = draw_demo_mark(image)
    credits = [provider.attribution] if provider is not None else []
    if any(candidate.source == "osm" for candidate in candidates):
        credits.append(OSM_ATTRIBUTION)
    if credits:
        image = draw_attribution(image, "\n".join(credits))
    return encode(image, with_basemap=provider is not None)


def render_candidate_image(
    candidate, origin_label: str, size: Tuple[int, int] = IMAGE_SIZE, providers: Optional[Sequence[TileProvider]] = None,
) -> RenderedImage:
    """Renders one candidate polygon with its name, area and origin label over the basemap.

    Args:
        candidate (PropertyCandidate): The candidate to show.
        origin_label (str): Text describing the origin (from ``agents.yml``), printed on the image.
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider], optional): Tile providers; defaults to ``resolve_providers()``.

    Returns:
        RenderedImage: JPEG over a tile basemap, PNG over the neutral background.
    """
    from domain.services.property_sources import geometry_utils as geo

    shape = geo.polygonal_part(geo.from_geojson_geometry(candidate.geometry))
    latitude, longitude = geo.center_of(shape)
    radius = max(50.0, geo.enclosing_radius_m(shape, (latitude, longitude))) * 1.25
    zoom, background, provider = _basemap_for(latitude, longitude, radius, size, providers)
    image = _draw_polygons(background, [(_polygon_rings(candidate), CANDIDATE_COLORS[0])], (latitude, longitude), zoom, size)
    image = draw_scale_bar(image, meters_per_pixel(latitude, zoom))
    title = candidate.name or ""
    image = draw_corner_label(image, f"{title}\n{candidate.area_ha:g} ha\n{origin_label}".strip())
    if _is_demo([candidate]):
        image = draw_demo_mark(image)
    credits = [provider.attribution] if provider is not None else []
    if candidate.source == "osm":
        credits.append(OSM_ATTRIBUTION)
    if credits:
        image = draw_attribution(image, "\n".join(credits))
    return encode(image, with_basemap=provider is not None)
