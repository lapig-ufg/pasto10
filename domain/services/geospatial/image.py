from io import BytesIO
from typing import List, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from domain.schemas.feature import Feature
from domain.services.geospatial.basemap import (
    DEFAULT_PROVIDERS,
    TileProvider,
    build_basemap,
    choose_zoom,
    latlon_to_pixel,
    meters_per_pixel,
)
from domain.services.geospatial.geometry import normalize_longitude

IMAGE_SIZE = (640, 640)
NEUTRAL_BACKGROUND = (226, 226, 220)
BUFFER_OUTLINE = (255, 214, 0, 255)
BUFFER_FILL = (255, 214, 0, 70)
_SCALE_BAR_STEPS_METERS = (10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000)


def _font(size: int) -> ImageFont.ImageFont:
    """Returns Pillow's bundled scalable font at the given pixel size."""
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


def draw_scale_bar(image: Image.Image, meters_per_px: float) -> Image.Image:
    """Draws a round-length scale bar on the bottom-left corner of the image.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        meters_per_px (float): Ground resolution of the image at its center.

    Returns:
        PIL.Image.Image: New image with the scale bar and its label.
    """
    width, height = image.size
    margin = max(10, int(width * 0.03))
    length_m = _scale_bar_length(meters_per_px, width * 0.3)
    length_px = round(length_m / meters_per_px)
    font = _font(max(12, int(height * 0.028)))

    scaled = image.copy()
    draw = ImageDraw.Draw(scaled)
    y = height - margin - 6
    x0, x1 = margin, margin + length_px
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
        (margin, margin), text, font=_font(max(14, int(height * 0.032))),
        fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0),
    )
    return labeled


def draw_attribution(image: Image.Image, text: str) -> Image.Image:
    """Draws the basemap attribution on a translucent strip at the bottom-right.

    Args:
        image (PIL.Image.Image): Image to draw on (modified copy is returned).
        text (str): Attribution required by the tile provider.

    Returns:
        PIL.Image.Image: New RGB image with the attribution drawn.
    """
    width, height = image.size
    font = _font(max(10, int(height * 0.02)))
    padding = 4
    left, top, right, bottom = ImageDraw.Draw(image).textbbox((0, 0), text, font=font)
    box = [width - (right - left) - 2 * padding, height - (bottom - top) - 2 * padding, width, height]

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle(box, fill=(255, 255, 255, 180))
    draw.text((box[0] + padding - left, box[1] + padding - top), text, font=font, fill=(40, 40, 40, 255))
    return Image.alpha_composite(base, overlay).convert("RGB")


def render_buffer_image(
    feature: Feature,
    size: Tuple[int, int] = IMAGE_SIZE,
    providers: Sequence[TileProvider] = DEFAULT_PROVIDERS,
) -> bytes:
    """Renders the confirmation image of a buffer area as PNG bytes.

    The image is centered on the buffer center at the highest zoom where the
    circle fits comfortably. The background comes from keyless tile services
    (Esri World Imagery, then OpenStreetMap); when the network fails the
    image is still produced over a neutral background, so the registration
    never breaks because of the basemap. On top of the background it draws
    the buffer (semi-transparent fill and outline), the center marker, a
    scale bar, a label with center/radius/area and — only when a basemap was
    used — the provider attribution.

    Args:
        feature (Feature): A buffer area feature (metadata ``center`` and ``radius``).
        size (Tuple[int, int]): Output (width, height) in pixels.
        providers (Sequence[TileProvider]): Tile providers to try, in order.

    Returns:
        bytes: The PNG-encoded image.

    Raises:
        ValueError: If the feature lacks valid ``center``/``radius`` metadata.
    """
    latitude, longitude = parse_center(feature)
    radius = float(feature.get_metadata("radius", 0))
    if radius <= 0:
        raise ValueError(f"Feature {feature.id} has no valid 'radius' metadata.")

    zoom = choose_zoom(latitude, radius, size, max_zoom=min(p.max_zoom for p in providers))
    background, provider = build_basemap(latitude, longitude, zoom, size, providers=providers)
    if background is None:
        background = Image.new("RGB", size, NEUTRAL_BACKGROUND)

    points = project_ring(feature.get_coords()[0][0], (latitude, longitude), zoom, size)
    image = draw_buffer_overlay(background, points)
    image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
    image = draw_scale_bar(image, meters_per_pixel(latitude, zoom))
    image = draw_corner_label(
        image,
        f"{latitude:.5f}, {longitude:.5f}\nr = {radius:g} m | {feature.total_area:g} ha",
    )
    if provider is not None:
        image = draw_attribution(image, provider.attribution)

    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()
