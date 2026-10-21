"""Tests of the pasture tools (domain/tools/pasture_tools.py).

No network and no Earth Engine: the service functions are replaced by fakes.
Both tools must leave ``session_state`` untouched.
"""

import copy

import pytest
from semente.backends.toolkit import StateContext

from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial.image import RenderedImage
from domain.services.geospatial.pasture import PastureClassification, YearUnavailable
from domain.tools import pasture_tools
from domain.utils.feature_utils import set_registered_features

STATS = PastureAreaStats(
    year=2020, source=PastureSource.GPW, pasture_ha=18.4, cultivated_ha=12.1, natural_ha=6.3, area_ha=28.27,
)


def _context(*names) -> StateContext:
    features = []
    for index, name in enumerate(names):
        feature = build_buffered_area(-15.5 + index, -47.5, 300)
        if name:
            feature.metadata.append(FeatureMetadata(key="name", value=name))
        features.append(feature)
    state = {}
    set_registered_features(state, RegisteredFeatures(features=features))
    return StateContext(session_state=state)


@pytest.fixture
def fake_service(monkeypatch):
    calls = []

    def classify(feature, year):
        calls.append((feature.get_metadata("center"), year))
        return PastureClassification("image", year or 2020, PastureSource.GPW, 30), "roi"

    def measure_area(feature, year):
        calls.append((feature.get_metadata("center"), year))
        return STATS

    monkeypatch.setattr(pasture_tools, "classify_pasture", classify)
    monkeypatch.setattr(pasture_tools, "measure_area", measure_area)
    monkeypatch.setattr(pasture_tools, "measure_pasture_area", lambda classification, roi: STATS)
    monkeypatch.setattr(
        pasture_tools, "render_pasture_map",
        lambda feature, image, stats, scale: RenderedImage(b"\xff\xd8\xffjpeg", "jpeg", "image/jpeg"),
    )
    return calls


@pytest.mark.parametrize("tool_name", ["get_pasture_area", "generate_pasture_map"])
def test_success_reports_figures_and_keeps_state(fake_service, tool_name):
    run_context = _context("North Paddock")
    before = copy.deepcopy(run_context.session_state)

    result = getattr(pasture_tools, tool_name)(run_context=run_context, year=2020)

    for expected in ("North Paddock", "2020", "Global Pasture Watch", "18.4 ha", "12.1 ha", "6.3 ha"):
        assert expected in result.content
    assert bool(result.images) is (tool_name == "generate_pasture_map")
    assert run_context.session_state == before


def test_map_image_is_attached(fake_service):
    result = pasture_tools.generate_pasture_map(run_context=_context("A"))

    assert result.images[0].content.startswith(b"\xff\xd8\xff")
    assert result.images[0].mime_type == "image/jpeg"


def test_name_selects_area_and_default_is_latest(fake_service):
    run_context = _context("North", "South")

    pasture_tools.get_pasture_area(run_context=run_context, area_name="north")
    pasture_tools.get_pasture_area(run_context=run_context)

    assert fake_service[0][0] == "-15.5, -47.5"
    assert fake_service[1][0] == "-14.5, -47.5"


def test_unnamed_area_does_not_show_empty_name(fake_service):
    result = pasture_tools.get_pasture_area(run_context=_context(None))

    assert '""' not in result.content


@pytest.mark.parametrize("tool_name", ["get_pasture_area", "generate_pasture_map"])
def test_no_registered_area(fake_service, tool_name):
    result = getattr(pasture_tools, tool_name)(run_context=_context())

    assert "no registered area" in result.content.lower() or "has no registered area" in result.content
    assert not result.images and fake_service == []


@pytest.mark.parametrize("tool_name", ["get_pasture_area", "generate_pasture_map"])
def test_area_not_found(fake_service, tool_name):
    result = getattr(pasture_tools, tool_name)(run_context=_context("North"), area_name="Lake")

    assert "Lake" in result.content and not result.images and fake_service == []


@pytest.mark.parametrize(
    "error, expected",
    [
        (YearUnavailable(latest=2025), "2000 to 2025"),
        (EarthEngineUnavailable("no creds"), "temporarily unavailable"),
        (RuntimeError("boom secret"), "unexpected error"),
    ],
)
@pytest.mark.parametrize("tool_name", ["get_pasture_area", "generate_pasture_map"])
def test_failures_use_yaml_texts(monkeypatch, tool_name, error, expected):
    def fail(feature, year):
        raise error

    monkeypatch.setattr(pasture_tools, "classify_pasture", fail)
    monkeypatch.setattr(pasture_tools, "measure_area", fail)

    result = getattr(pasture_tools, tool_name)(run_context=_context("North"), year=2030)

    assert expected in result.content
    assert "secret" not in result.content and not result.images


@pytest.mark.parametrize("tool_name", ["get_pasture_area", "generate_pasture_map"])
def test_estimate_success_carries_the_strong_warning(monkeypatch, fake_service, tool_name):
    estimate = STATS.model_copy(update={"year": 2024, "source": PastureSource.CLASSIFICATION})
    monkeypatch.setattr(pasture_tools, "measure_area", lambda feature, year: estimate)
    monkeypatch.setattr(pasture_tools, "measure_pasture_area", lambda classification, roi: estimate)

    result = getattr(pasture_tools, tool_name)(run_context=_context("North"), year=2024)

    for expected in ("experimental estimate", "order of magnitude", "several times", "2022", "reliable reference"):
        assert expected in result.content


def test_tool_descriptions_state_the_2022_default():
    from semente.configs.prompts import get_tool_description

    for name in ("get_pasture_area", "generate_pasture_map"):
        description = get_tool_description("pasture_tools", name)
        assert "default year is 2022" in description and "experimental" in description
