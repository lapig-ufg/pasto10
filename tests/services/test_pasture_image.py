"""Tests of the pasture map renderer (domain/services/geospatial/pasture_image.py).

No network and no Earth Engine: the overlay download is replaced by a fake.
"""

import math
from io import BytesIO

import pytest
from PIL import Image

from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial import pasture_image
from domain.services.geospatial.basemap import latlon_to_pixel
from domain.services.geospatial.geometry import build_buffered_area

SIZE = (640, 640)
OVERLAY_GREEN = (0, 200, 0, 255)


def _stats(source=PastureSource.GPW):
    return PastureAreaStats(
        year=2020, source=source, pasture_ha=18.4, cultivated_ha=12.1, natural_ha=6.3, area_ha=28.27,
    )


@pytest.fixture
def fake_overlay(monkeypatch):
    seen = {}

    def fetch(image, bounds, size, scale):
        seen.update(image=image, bounds=bounds, size=size, scale=scale)
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        overlay.paste(Image.new("RGBA", (100, 100), OVERLAY_GREEN), (20, 300))
        return overlay

    monkeypatch.setattr(pasture_image, "_fetch_overlay", fetch)
    return seen


def test_mercator_bounds_are_square_and_centered():
    center = latlon_to_pixel(-15.5, -47.5, 15)

    xmin, ymin, xmax, ymax = pasture_image._mercator_bounds(center, 15, SIZE)

    assert xmax > xmin and ymax > ymin
    assert math.isclose(xmax - xmin, ymax - ymin, rel_tol=1e-9)
    assert xmin < -5_287_000 < xmax


def test_render_over_neutral_background_is_png_with_overlay(fake_overlay):
    feature = build_buffered_area(-15.5, -47.5, 500)

    rendered = pasture_image.render_pasture_map(feature, "ee-image", _stats(), 30)

    assert rendered.format == "png" and rendered.content.startswith(b"\x89PNG")
    image = Image.open(BytesIO(rendered.content)).convert("RGB")
    assert image.size == SIZE
    assert fake_overlay["scale"] == 30
    assert fake_overlay["image"] == "ee-image" and fake_overlay["size"] == SIZE
    r, g, b = image.getpixel((70, 350))
    assert g > r and g > b


def test_render_over_basemap_is_jpeg(fake_overlay, online_tiles):
    feature = build_buffered_area(-15.5, -47.5, 500)

    rendered = pasture_image.render_pasture_map(feature, "ee-image", _stats(PastureSource.CLASSIFICATION), 10)

    assert rendered.format == "jpeg" and rendered.content.startswith(b"\xff\xd8\xff")


def test_overlay_failure_propagates(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ee down")

    monkeypatch.setattr(pasture_image, "_fetch_overlay", boom)

    with pytest.raises(RuntimeError):
        pasture_image.render_pasture_map(build_buffered_area(-15.5, -47.5, 500), "x", _stats(), 30)


def test_invalid_radius_metadata_raises(fake_overlay):
    feature = build_buffered_area(-15.5, -47.5, 500)
    feature.metadata = [entry for entry in feature.metadata if entry.key != "radius"]

    with pytest.raises(ValueError):
        pasture_image.render_pasture_map(feature, "x", _stats(), 30)


@pytest.mark.parametrize(
    "source, expected_label, expected_attribution",
    [
        (PastureSource.GPW, False, "Pasture: Global Pasture Watch"),
        (PastureSource.CLASSIFICATION, True, "experimental estimate, not an official map"),
    ],
)
def test_estimate_is_labeled_only_for_classification(monkeypatch, fake_overlay, source, expected_label, expected_attribution):
    texts = {}
    monkeypatch.setattr(pasture_image, "draw_corner_label", lambda image, text: texts.setdefault("label", text) and image)
    monkeypatch.setattr(pasture_image, "draw_attribution", lambda image, text: texts.setdefault("attribution", text) and image)

    pasture_image.render_pasture_map(build_buffered_area(-15.5, -47.5, 500), "x", _stats(source), 10)

    assert ("Estimate" in texts["label"]) is expected_label
    assert texts["label"].startswith("2020")
    assert expected_attribution in texts["attribution"]


def test_overlay_request_never_exceeds_the_output_size(monkeypatch):
    seen = {}

    class FakeVisual:
        def getThumbURL(self, params):
            seen.update(params)
            return "http://thumb"

    class FakeImage:
        def updateMask(self, _):
            return self

        def gt(self, _):
            return self

        def visualize(self, **kwargs):
            return FakeVisual()

    class Response:
        content = b""

        def raise_for_status(self):
            pass

    buffer = BytesIO()
    Image.new("RGBA", (10, 10)).save(buffer, format="PNG")
    Response.content = buffer.getvalue()
    monkeypatch.setattr(pasture_image.ee.Geometry, "Rectangle", staticmethod(lambda *a, **k: "rect"))
    monkeypatch.setattr(pasture_image.requests, "get", lambda *a, **k: Response())

    pasture_image._fetch_overlay(FakeImage(), (0, 0, 100000, 100000), (640, 640), 10)

    assert seen["dimensions"] == "640x640"
