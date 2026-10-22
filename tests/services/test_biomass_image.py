"""Tests of the biomass map renderer (domain/services/geospatial/biomass_image.py). No network, no Earth Engine."""

from io import BytesIO

import pytest
from PIL import Image

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.services.geospatial import biomass_image
from domain.services.geospatial.geometry import build_buffered_area


def _stats(source=BiomassSource.GPW_ANNUAL, label="2025"):
    return BiomassStats(
        source=source, period_label=label, start="2025-01-01", end="2025-12-31", mean_t_ha=17.4, total_t=3571.3,
        p02_t_ha=12.1, p98_t_ha=23.2, pasture_ha=205.4, area_ha=312.5, scale_m=30, mask_year=2022, year=2025,
        days_covered=365,
    )


@pytest.fixture
def fake_overlay(monkeypatch):
    def fetch(image, stats, bounds, size, value_range=None):
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        overlay.paste(Image.new("RGBA", (100, 100), (0, 200, 0, 255)), (20, 300))
        return overlay

    monkeypatch.setattr(biomass_image, "fetch_overlay", fetch)


def test_render_over_neutral_background_is_png(fake_overlay):
    rendered = biomass_image.render_biomass_map(build_buffered_area(-15.5, -47.5, 1000), "x", _stats())

    image = Image.open(BytesIO(rendered.content)).convert("RGB")
    assert rendered.format == "png" and image.size == (640, 640)
    r, g, b = image.getpixel((70, 350))
    assert g > r and g > b


def test_render_over_basemap_is_jpeg(fake_overlay, online_tiles):
    rendered = biomass_image.render_biomass_map(build_buffered_area(-15.5, -47.5, 1000), "x", _stats())

    assert rendered.format == "jpeg"


@pytest.mark.parametrize(
    "source, expected",
    [(BiomassSource.GPW_ANNUAL, "Global Pasture Watch"), (BiomassSource.TIME2GRAZE, "Time2Graze")],
)
def test_label_and_attribution_state_period_source_and_estimate(monkeypatch, fake_overlay, source, expected):
    texts = {}
    monkeypatch.setattr(biomass_image, "draw_corner_label", lambda image, text: texts.setdefault("label", text) and image)
    monkeypatch.setattr(biomass_image, "draw_attribution", lambda image, text: texts.setdefault("attribution", text) and image)

    biomass_image.render_biomass_map(build_buffered_area(-15.5, -47.5, 1000), "x", _stats(source, "2026-07"))

    assert texts["label"].startswith("2026-07") and "t/ha" in texts["label"]
    assert expected in texts["attribution"] and "estimate" in texts["attribution"]


def test_colorbar_is_drawn_with_the_value_range():
    image = Image.new("RGB", (640, 640), (10, 10, 10))

    drawn = biomass_image.draw_colorbar(image, _stats(), bottom=600)

    assert drawn.size == image.size and drawn.tobytes() != image.tobytes()


def test_overlay_failure_propagates(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ee down")

    monkeypatch.setattr(biomass_image, "fetch_overlay", boom)

    with pytest.raises(RuntimeError):
        biomass_image.render_biomass_map(build_buffered_area(-15.5, -47.5, 1000), "x", _stats())
