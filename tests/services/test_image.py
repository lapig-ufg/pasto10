"""Tests of the buffer confirmation image (domain/services/geospatial/image.py).

No network: the autouse ``offline_tiles`` fixture makes every tile download
fail; ``online_tiles`` serves solid green fake tiles.
"""

import inspect
import time
from io import BytesIO

import pytest
import requests
from PIL import Image

from domain.services.geospatial import basemap, image
from domain.services.geospatial.geometry import build_buffered_area

PNG_SIGNATURE = b"\x89PNG"
JPEG_SIGNATURE = b"\xff\xd8\xff"
FAKE_TILE_GREEN = (34, 139, 34)
ESRI_ATTRIBUTION_LINES = [
    "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community",
    "Powered by Esri",
]
# Captured at import, before the autouse offline_tiles fixture replaces it.
REAL_FETCH_TILE = basemap.fetch_tile


def _decode(content: bytes) -> Image.Image:
    return Image.open(BytesIO(content)).convert("RGB")


def _assert_close(pixel, expected, tolerance=6):
    """JPEG is lossy: compare colors channel by channel within a tolerance."""
    assert all(abs(a - b) <= tolerance for a, b in zip(pixel, expected)), (pixel, expected)


def _boxes_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


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


@pytest.fixture
def label_spy(monkeypatch):
    """Records the corner label texts drawn on the image."""
    calls = []
    original = image.draw_corner_label

    def spy(img, text):
        calls.append(text)
        return original(img, text)

    monkeypatch.setattr(image, "draw_corner_label", spy)
    return calls


def test_renders_jpeg_over_basemap_with_attribution(online_tiles, attribution_spy):
    feature = build_buffered_area(-15.5, -47.5, 200)

    rendered = image.render_buffer_image(feature)

    assert rendered.content.startswith(JPEG_SIGNATURE)
    assert (rendered.format, rendered.mime_type) == ("jpeg", "image/jpeg")
    decoded = _decode(rendered.content)
    assert decoded.size == image.IMAGE_SIZE
    _assert_close(decoded.getpixel((decoded.width - 5, decoded.height // 2)), FAKE_TILE_GREEN)
    assert attribution_spy == [basemap.ESRI_WORLD_IMAGERY.attribution]


def test_falls_back_to_osm_when_esri_fails(monkeypatch, attribution_spy):
    def esri_down(provider, zoom, x, y):
        if provider is basemap.ESRI_WORLD_IMAGERY:
            raise requests.Timeout("esri timeout")
        return Image.new("RGB", (256, 256), FAKE_TILE_GREEN)

    monkeypatch.setattr(basemap, "fetch_tile", esri_down)

    rendered = image.render_buffer_image(build_buffered_area(51.0, 10.0, 500))

    assert rendered.content.startswith(JPEG_SIGNATURE)
    assert attribution_spy == [basemap.OPENSTREETMAP.attribution]


def test_network_failure_renders_neutral_background_without_attribution(attribution_spy):
    feature = build_buffered_area(0.0, 0.0, 1000)

    rendered = image.render_buffer_image(feature)

    assert rendered.content.startswith(PNG_SIGNATURE)
    assert (rendered.format, rendered.mime_type) == ("png", "image/png")
    decoded = _decode(rendered.content)
    assert decoded.getpixel((decoded.width - 5, decoded.height // 2)) == image.NEUTRAL_BACKGROUND
    assert attribution_spy == []


def test_timeout_renders_neutral_background(monkeypatch):
    def timeout(provider, zoom, x, y):
        raise requests.Timeout("slow network")

    monkeypatch.setattr(basemap, "fetch_tile", timeout)

    rendered = image.render_buffer_image(build_buffered_area(-33.9, 18.4, 50))

    assert rendered.content.startswith(PNG_SIGNATURE)


def test_buffer_is_drawn_around_image_center():
    rendered = image.render_buffer_image(build_buffered_area(10.0, 20.0, 300))
    decoded = _decode(rendered.content)

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


def test_esri_attribution_text_is_the_official_two_lines():
    assert basemap.ESRI_WORLD_IMAGERY.attribution.splitlines() == ESRI_ATTRIBUTION_LINES
    assert basemap.OPENSTREETMAP.attribution == "© OpenStreetMap contributors"


@pytest.mark.parametrize("provider", [basemap.ESRI_WORLD_IMAGERY, basemap.OPENSTREETMAP], ids=["esri", "osm"])
@pytest.mark.parametrize("radius", [50, 200, 1000])
def test_attribution_fits_and_does_not_overlap_scale_bar(provider, radius):
    size = image.IMAGE_SIZE
    zoom = basemap.choose_zoom(0.0, radius, size, max_zoom=provider.max_zoom)
    attribution = image.attribution_box(size, provider.attribution)
    scale_bar = image.scale_bar_box(size, basemap.meters_per_pixel(0.0, zoom))

    assert image.attribution_font_size(size) >= 12
    assert 0 <= attribution[0] and 0 <= attribution[1]
    assert attribution[2] <= size[0] and attribution[3] <= size[1]
    assert not _boxes_overlap(attribution, scale_bar)


def test_label_second_line_shows_radius_and_area(label_spy):
    image.render_buffer_image(build_buffered_area(-15.5, -47.5, 300))

    first_line, second_line = label_spy[0].splitlines()
    assert first_line == "-15.50000, -47.50000"
    assert second_line == "Radius 300 m · 28.27 ha"


def test_middle_dot_glyph_is_rendered_by_the_label_font():
    font = image.load_font(17)
    missing_glyph = font.getmask("\ue000").getbbox()

    assert font.getmask("·").getbbox() not in (None, missing_glyph)


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
    assert elapsed < 0.9

    rendered = image.render_buffer_image(build_buffered_area(0.0, 0.0, 200))
    assert rendered.content.startswith(PNG_SIGNATURE)


@pytest.mark.parametrize("value", ["", "none", "NONE"])
def test_disabled_basemap_renders_neutral_png_without_fetching(monkeypatch, value):
    def must_not_fetch(provider, zoom, x, y):
        raise AssertionError("tile fetched with the basemap disabled")

    monkeypatch.setenv(basemap.PROVIDERS_ENV, value)
    monkeypatch.setattr(basemap, "fetch_tile", must_not_fetch)

    assert basemap.resolve_providers() == ()
    rendered = image.render_buffer_image(build_buffered_area(0.0, 0.0, 200))
    assert rendered.content.startswith(PNG_SIGNATURE)
    assert rendered.format == "png"


def test_providers_default_to_esri_then_osm():
    assert basemap.resolve_providers() == (basemap.ESRI_WORLD_IMAGERY, basemap.OPENSTREETMAP)


def test_providers_env_selects_osm_only(monkeypatch, attribution_spy):
    used = []

    def record(provider, zoom, x, y):
        used.append(provider.name)
        return Image.new("RGB", (256, 256), FAKE_TILE_GREEN)

    monkeypatch.setenv(basemap.PROVIDERS_ENV, "osm")
    monkeypatch.setattr(basemap, "fetch_tile", record)

    assert basemap.resolve_providers() == (basemap.OPENSTREETMAP,)
    image.render_buffer_image(build_buffered_area(0.0, 0.0, 200))
    assert set(used) == {"osm"}
    assert attribution_spy == [basemap.OPENSTREETMAP.attribution]


def test_providers_env_keeps_order(monkeypatch):
    monkeypatch.setenv(basemap.PROVIDERS_ENV, "osm, esri")

    assert [p.name for p in basemap.resolve_providers()] == ["osm", "esri"]


def test_unknown_provider_is_ignored_with_warning(monkeypatch, caplog):
    monkeypatch.setenv(basemap.PROVIDERS_ENV, "bing,osm")

    with caplog.at_level("WARNING"):
        providers = basemap.resolve_providers()

    assert providers == (basemap.OPENSTREETMAP,)
    assert "bing" in caplog.text


def test_arcgis_api_key_switches_esri_to_keyed_endpoint(monkeypatch):
    monkeypatch.setenv(basemap.ARCGIS_API_KEY_ENV, "abc123KEY")

    esri = basemap.resolve_providers()[0]
    url = esri.tile_url(3, 5, 2)

    assert url.startswith("https://ibasemaps-api.arcgis.com/")
    assert "token=abc123KEY" in url
    assert "abc123KEY" not in repr(esri)
    assert esri.attribution == basemap.ESRI_WORLD_IMAGERY.attribution


def test_token_is_appended_with_ampersand_when_template_has_query():
    provider = basemap.TileProvider(
        name="custom", url_template="https://tiles.example/{z}/{x}/{y}?style=sat",
        attribution="x", max_zoom=18, token="k/1",
    )

    assert provider.tile_url(3, 5, 2) == "https://tiles.example/3/5/2?style=sat&token=k%2F1"


def test_token_is_appended_with_question_mark_without_query():
    provider = basemap.TileProvider(
        name="custom", url_template="https://tiles.example/{z}/{x}/{y}",
        attribution="x", max_zoom=18, token="k",
    )

    assert provider.tile_url(3, 5, 2) == "https://tiles.example/3/5/2?token=k"


def test_without_api_key_esri_url_has_no_token():
    esri = basemap.resolve_providers()[0]

    assert esri is basemap.ESRI_WORLD_IMAGERY
    assert "token" not in esri.tile_url(3, 5, 2)


def test_api_key_never_appears_in_logs(monkeypatch, caplog):
    key = "s3cr3t-arcgis-key"

    def failing(provider, zoom, x, y):
        raise requests.HTTPError(f"403 Client Error for url: {provider.tile_url(zoom, x, y)}")

    monkeypatch.setenv(basemap.ARCGIS_API_KEY_ENV, key)
    monkeypatch.setattr(basemap, "fetch_tile", failing)

    with caplog.at_level("DEBUG"):
        rendered = image.render_buffer_image(build_buffered_area(0.0, 0.0, 200))

    assert rendered.format == "png"
    assert "403 Client Error" in caplog.text
    assert key not in caplog.text


def test_requests_are_blocked_inside_tests():
    with pytest.raises(requests.ConnectionError):
        requests.get("https://example.com")


def test_session_is_closed_at_exit():
    source = inspect.getsource(basemap)

    assert "atexit.register(_SESSION.close)" in source
