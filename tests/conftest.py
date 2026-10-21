"""Shared test setup.

Domain tool modules resolve their descriptions at import time via the
semente prompts loader, which needs the app's prompts dir. At runtime
``semente.build.build_app`` applies it from the manifest; in tests, apply it
here before any domain import.

Tests never touch the network: the autouse ``offline_tiles`` fixture makes
every basemap tile download fail, blocks any HTTP request made through
``requests`` (``HTTPAdapter.send`` raises ``requests.ConnectionError``) and
clears the basemap environment variables, so the default providers apply.
Tests that need a basemap request the ``online_tiles`` fixture, which serves
fake tiles instead.
"""

import os

import pytest
import requests
from PIL import Image

from semente.configs.prompts import set_prompts_dir

set_prompts_dir(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "domain", "prompts")
)


def _offline_fetch(provider, zoom, x, y):
    raise ConnectionError("network disabled in tests")


def _blocked_send(self, request, *args, **kwargs):
    raise requests.ConnectionError(f"network disabled in tests: {request.url}")


def _fake_tile(provider, zoom, x, y) -> Image.Image:
    return Image.new("RGB", (256, 256), (34, 139, 34))


@pytest.fixture(autouse=True)
def offline_tiles(monkeypatch):
    """Makes every tile download and HTTP request fail; resets basemap env vars."""
    from domain.services.geospatial import basemap

    monkeypatch.setattr(basemap, "fetch_tile", _offline_fetch)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_send)
    monkeypatch.delenv(basemap.PROVIDERS_ENV, raising=False)
    monkeypatch.delenv(basemap.ARCGIS_API_KEY_ENV, raising=False)


@pytest.fixture
def online_tiles(monkeypatch):
    """Serves solid green fake tiles instead of downloading them."""
    from domain.services.geospatial import basemap

    monkeypatch.setattr(basemap, "fetch_tile", _fake_tile)

