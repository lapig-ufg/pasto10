"""Tests of the buffer confirmation image (domain/services/geospatial/image.py).

No network: the autouse ``offline_tiles`` fixture makes every tile download
fail; ``online_tiles`` serves solid green fake tiles.
"""

import time
from io import BytesIO

import pytest
import requests
from PIL import Image

from domain.services.geospatial import basemap, image
from domain.services.geospatial.geometry import build_buffered_area

PNG_SIGNATURE = b"\x89PNG"
FAKE_TILE_GREEN = (34, 139, 34)
# Captured at import, before the autouse offline_tiles fixture replaces it.
REAL_FETCH_TILE = basemap.fetch_tile


def _decode(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGB")


@pytest.fixture
def attribution_spy(monkeypatch):
    """Records the attribution texts drawn on the image."""
    calls = []
    original = image.draw_attribution

    def spy(img, text):
        calls.append(text)
        return original(img, text)

    monkeypatch.setattr(image, "draw_attribution", spy)
    return calls


def test_renders_png_over_basemap_with_attribution(online_tiles, attribution_spy):
    feature = build_buffered_area(-15.5, -47.5, 200)

    png = image.render_buffer_image(feature)

    assert png.startswith(PNG_SIGNATURE)
    decoded = _decode(png)
    assert decoded.size == image.IMAGE_SIZE
    assert decoded.getpixel((decoded.width - 5, decoded.height // 2)) == FAKE_TILE_GREEN
    assert attribution_spy == [basemap.ESRI_WORLD_IMAGERY.attribution]


def test_falls_back_to_osm_when_esri_fails(monkeypatch, attribution_spy):
    def esri_down(provider, zoom, x, y):
        if provider is basemap.ESRI_WORLD_IMAGERY:
            raise requests.Timeout("esri timeout")
        return Image.new("RGB", (256, 256), FAKE_TILE_GREEN)

    monkeypatch.setattr(basemap, "fetch_tile", esri_down)

    png = image.render_buffer_image(build_buffered_area(51.0, 10.0, 500))

    assert png.startswith(PNG_SIGNATURE)
    assert attribution_spy == [basemap.OPENSTREETMAP.attribution]


def test_network_failure_renders_neutral_background_without_attribution(attribution_spy):
    feature = build_buffered_area(0.0, 0.0, 1000)

    png = image.render_buffer_image(feature)

    assert png.startswith(PNG_SIGNATURE)
    decoded = _decode(png)
    assert decoded.getpixel((decoded.width - 5, decoded.height // 2)) == image.NEUTRAL_BACKGROUND
    assert attribution_spy == []


def test_timeout_renders_neutral_background(monkeypatch):
    def timeout(provider, zoom, x, y):
        raise requests.Timeout("slow network")

    monkeypatch.setattr(basemap, "fetch_tile", timeout)

    png = image.render_buffer_image(build_buffered_area(-33.9, 18.4, 50))

    assert png.startswith(PNG_SIGNATURE)


def test_buffer_is_drawn_around_image_center():
    png = image.render_buffer_image(build_buffered_area(10.0, 20.0, 300))
    decoded = _decode(png)

    center = (decoded.width // 2, decoded.height // 2)
    assert decoded.getpixel(center) != image.NEUTRAL_BACKGROUND
    inside = (decoded.width // 2 + 40, decoded.height // 2 + 40)
    assert decoded.getpixel(inside) != image.NEUTRAL_BACKGROUND


def test_choose_zoom_fits_buffer_in_image():
    for radius in (50, 200, 1000):
        zoom = basemap.choose_zoom(0.0, radius, image.IMAGE_SIZE)
        diameter_px = 2 * radius / basemap.meters_per_pixel(0.0, zoom)
        assert diameter_px <= min(image.IMAGE_SIZE)


def test_fetch_tile_uses_session_with_user_agent_and_timeout(monkeypatch):
    captured = {}
    tile = BytesIO()
    Image.new("RGB", (256, 256), FAKE_TILE_GREEN).save(tile, format="PNG")

    class FakeResponse:
        content = tile.getvalue()

        def raise_for_status(self):
            pass

    def fake_get(url, timeout):
        captured.update(url=url, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(basemap._SESSION, "get", fake_get)

    result = REAL_FETCH_TILE(basemap.ESRI_WORLD_IMAGERY, 3, 5, 2)

    assert result.size == (256, 256)
    assert captured["url"].endswith("/tile/3/2/5")
    assert captured["timeout"] == (3, 4)
    assert basemap._SESSION.headers["User-Agent"] == basemap.USER_AGENT
    assert basemap.USER_AGENT == "pasto10 (LAPIG/UFG; +https://github.com/lapig-ufg/pasto10)"


def test_esri_attribution_fits_inside_image():
    text = basemap.ESRI_WORLD_IMAGERY.attribution
    assert text == "Esri, Maxar, Earthstar Geographics, and the GIS User Community"

    font = image._font(max(10, int(image.IMAGE_SIZE[1] * 0.02)))
    left, _, right, _ = font.getbbox(text)

    assert right - left + 8 <= image.IMAGE_SIZE[0]


def test_slow_tiles_hit_deadline_and_image_is_still_rendered(monkeypatch):
    def slow(provider, zoom, x, y):
        time.sleep(1)
        return Image.new("RGB", (256, 256), FAKE_TILE_GREEN)

    monkeypatch.setattr(basemap, "BASEMAP_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(basemap, "fetch_tile", slow)

    started = time.monotonic()
    background, provider = basemap.build_basemap(0.0, 0.0, 15, image.IMAGE_SIZE)
    elapsed = time.monotonic() - started

    assert (background, provider) == (None, None)
    assert elapsed < 0.5

    png = image.render_buffer_image(build_buffered_area(0.0, 0.0, 200))
    assert png.startswith(PNG_SIGNATURE)
