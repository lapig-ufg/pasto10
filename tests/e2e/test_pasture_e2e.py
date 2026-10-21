"""End-to-end test of the pasture tools against the real Earth Engine.

Needs ``GEE_PROJECT``, ``GEE_SERVICE_ACCOUNT`` and ``GEE_KEY_FILE`` in the
environment (skipped otherwise) and network access. Run with
``uv run pytest -m e2e -s``. Buffer of 500 m at (-15.5, -47.5): 2020 comes
from Global Pasture Watch, 2024 from the Random Forest classification.
"""

import os
import time
from io import BytesIO

import pytest
from PIL import Image
from semente.backends.toolkit import StateContext

from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial import pasture
from domain.tools import pasture_tools
from domain.utils.feature_utils import set_registered_features
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures

pytestmark = pytest.mark.e2e

_REQUIRED_ENV = ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")
BUFFER_AREA_HA = 78.54  # 500 m radius


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing environment variables: {', '.join(missing)}")


@pytest.fixture
def run_context():
    area = build_buffered_area(-15.5, -47.5, 500)
    area = area.model_copy(update={"metadata": [*area.metadata, FeatureMetadata(key="name", value="Test paddock")]})
    state = {}
    set_registered_features(state, RegisteredFeatures(features=[area]))
    return StateContext(session_state=state)


def _check_stats(text: str):
    assert "ha" in text and "Pasture" in text


def test_pasture_area_gpw_2020(run_context):
    start = time.perf_counter()
    result = pasture_tools.get_pasture_area(run_context=run_context, year=2020)
    print(f"\nget_pasture_area 2020 (GPW): {time.perf_counter() - start:.1f}s\n{result.content}")
    _check_stats(result.content)
    assert "Global Pasture Watch" in result.content


def test_pasture_area_classification_2024(run_context):
    start = time.perf_counter()
    result = pasture_tools.get_pasture_area(run_context=run_context, year=2024)
    print(f"\nget_pasture_area 2024 (RF): {time.perf_counter() - start:.1f}s\n{result.content}")
    _check_stats(result.content)
    assert "AlphaEarth" in result.content


@pytest.mark.parametrize("year", [2020, 2024])
def test_pasture_map(run_context, year, tmp_path):
    start = time.perf_counter()
    result = pasture_tools.generate_pasture_map(run_context=run_context, year=year)
    print(f"\ngenerate_pasture_map {year}: {time.perf_counter() - start:.1f}s")
    assert result.images
    image = Image.open(BytesIO(result.images[0].content))
    assert image.size == (640, 640)
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        image.save(os.path.join(out, f"pasture_map_{year}.png"))


def _assert_consistent(stats):
    assert stats.pasture_ha == pytest.approx(stats.cultivated_ha + stats.natural_ha, abs=0.02)
    assert 0 <= stats.pasture_ha <= stats.area_ha
    assert stats.area_ha == pytest.approx(BUFFER_AREA_HA, rel=0.1)


@pytest.mark.parametrize("year", [2020, 2024])
def test_stats_are_consistent(run_context, year):
    from domain.utils.feature_utils import find_registered_feature

    stats = pasture.get_pasture_area(find_registered_feature(run_context.session_state), year)

    print(f"\nstats {year}: {stats}")
    _assert_consistent(stats)
    assert stats.pasture_ha > 0


def test_natural_grassland_in_mongolia_2020():
    feature = build_buffered_area(47.9, 106.9, 1000)

    stats = pasture.get_pasture_area(feature, 2020)

    print(f"\nMongolia 2020: {stats}")
    assert stats.pasture_ha == pytest.approx(stats.cultivated_ha + stats.natural_ha, abs=0.02)
    assert 0 <= stats.pasture_ha <= stats.area_ha
    assert stats.natural_ha > 0


_SITES = {
    "mongolia": (47.9, 106.9, 1000),
    "sahel": (13.5, 2.1, 1000),
    "cerrado": (-15.5, -47.5, 500),
}


@pytest.mark.parametrize("site", list(_SITES))
@pytest.mark.parametrize("year", [2022, 2023])
def test_area_ha_covers_the_whole_buffer(site, year):
    """Masked GPW pixels count as non-grassland, so area_ha is the buffer area in every source."""
    latitude, longitude, radius = _SITES[site]
    feature = build_buffered_area(latitude, longitude, radius)

    stats = pasture.get_pasture_area(feature, year)

    print(f"\n{site} {year}: {stats}")
    assert stats.area_ha == pytest.approx(feature.total_area, rel=0.10)
    assert 0 <= stats.pasture_ha <= stats.area_ha


def test_city_is_not_painted_as_natural_grassland_2023():
    """Ulaanbaatar: the classifier must not mark the built-up area as grassland."""
    feature = build_buffered_area(47.9, 106.9, 1000)

    stats = pasture.get_pasture_area(feature, 2023)

    print(f"\nUlaanbaatar 2023: {stats}")
    assert stats.source is pasture.PastureSource.CLASSIFICATION
    assert stats.natural_ha < 0.5 * feature.total_area


@pytest.mark.parametrize("site", ["cerrado", "pampas"])
def test_2022_to_2023_change_stays_bounded(site):
    """The two sources differ (30 m GPW vs 10 m estimate); only gross failures are caught.

    Observed jumps: Cerrado 62.0 -> 45.1 ha (22% of the buffer), Pampas
    43.3 -> 59.6 ha (5%). The margin of 30% of the buffer leaves room for that
    and still fails when a land cover is painted wholesale.
    """
    latitude, longitude, radius = {**_SITES, "pampas": (-34.0, -59.0, 1000)}[site]
    feature = build_buffered_area(latitude, longitude, radius)

    before = pasture.get_pasture_area(feature, 2022)
    after = pasture.get_pasture_area(feature, 2023)

    print(f"\n{site}: 2022={before.pasture_ha} 2023={after.pasture_ha} of {before.area_ha}")
    assert abs(after.pasture_ha - before.pasture_ha) <= 0.30 * before.area_ha


def test_default_year_is_the_last_gpw_year(run_context):
    from domain.utils.feature_utils import find_registered_feature

    stats = pasture.get_pasture_area(find_registered_feature(run_context.session_state), None)

    assert stats.year == 2022 and stats.source is pasture.PastureSource.GPW


def test_explicit_later_year_uses_the_classification(run_context):
    from domain.utils.feature_utils import find_registered_feature

    stats = pasture.get_pasture_area(find_registered_feature(run_context.session_state), 2024)

    assert stats.year == 2024 and stats.source is pasture.PastureSource.CLASSIFICATION


def test_repeated_call_reuses_cached_stats_without_retraining(run_context):
    from domain.utils.feature_utils import find_registered_feature

    feature = find_registered_feature(run_context.session_state)
    pasture.get_pasture_area(feature, 2024)
    start = time.perf_counter()
    again = pasture.get_pasture_area(feature, 2024)
    elapsed = time.perf_counter() - start

    print(f"\ncached repeat: {elapsed:.3f}s")
    assert again is pasture.get_pasture_area(feature, 2024)
    assert elapsed < 3
