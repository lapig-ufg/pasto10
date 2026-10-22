"""Generalist end-to-end tests of the pasture tools over several continents.

Real Earth Engine (needs ``GEE_*``; skipped otherwise). Covers the whole chat
flow through ``session_state`` (register by coordinate, confirm, name, then
measure and map the registered area), area consistency in many biomes, the
concurrent single-flight behavior and a validation of the post-2022
classifier against the Global Pasture Watch v1-1 maps of 2023 and 2024
(``tools/benchmark_pasture.py``). Run with ``uv run pytest -m e2e -s``; set
``PASTO10_E2E_OUT`` to a directory to keep the generated maps.
"""

import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from PIL import Image
from semente.backends.toolkit import StateContext

from domain.services.geospatial import pasture
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import pasture_tools, property_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties
from tools import benchmark_pasture as benchmark

pytestmark = pytest.mark.e2e

_REQUIRED_ENV = ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")

PROPERTIES = load_global_properties()
PROPERTY_IDS = [item.id for item in PROPERTIES]
# Observed on the 15 properties (2023 and 2024): mean accuracy 0.884, median relative error 0.086.
# The margins absorb the run-to-run variation of the forest while still failing on a broken classifier.
MIN_MEAN_ACCURACY = 0.85
MAX_MEDIAN_RELATIVE_ERROR = 0.20


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing environment variables: {', '.join(missing)}")


def _register(name: str, latitude: float, longitude: float, radius: int) -> StateContext:
    """Registers an area through the real registration tools and returns the context."""
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=latitude, longitude=longitude, radius=radius,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=name)
    return context


_WORLDCOVER = {
    10: "tree", 20: "shrub", 30: "grass", 40: "crop", 50: "built", 60: "bare",
    70: "snow", 80: "water", 90: "wetland", 95: "mangrove", 100: "moss",
}
_summary: list[dict] = []


def _feature_of(item: GlobalProperty):
    return build_buffered_area(item.latitude, item.longitude, item.radius_m)


def _independent_profile(item: GlobalProperty) -> dict:
    """Measures the property with assets independent of the app: WorldCover shares and mean elevation."""
    import ee

    roi = pasture.feature_geometry(_feature_of(item))
    cover = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    groups = ee.Image.pixelArea().addBands(cover.rename("c")).reduceRegion(
        ee.Reducer.sum().group(1, "c"), roi, 30, maxPixels=1e9).getInfo()["groups"]
    total = sum(group["sum"] for group in groups)
    shares: dict = {}
    for group in groups:
        name = _WORLDCOVER.get(int(group["c"]), str(group["c"]))
        shares[name] = shares.get(name, 0.0) + group["sum"] / total
    elevation = ee.Image("USGS/SRTMGL1_003").reduceRegion(ee.Reducer.mean(), roi, 90).getInfo().get("elevation")
    return {"worldcover": shares, "elevation": elevation}


@pytest.fixture(scope="module", autouse=True)
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(15) + "continent".ljust(15) + "kind".ljust(15) + "area".rjust(8) + "GPW2022 ha".rjust(12)
              + "(cult/nat)".rjust(16) + "est.2024 ha".rjust(13) + "  map(s)")
        for row in _summary:
            print(row["id"].ljust(15) + row["continent"].ljust(15) + row["kind"].ljust(15) + f"{row['area']:8.1f}"
                  + f"{row['gpw']:12.1f}" + f"({row['cult']:.0f}/{row['nat']:.0f})".rjust(16)
                  + f"{row['est']:13.1f}" + f"  {row['seconds']:.1f}")


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_property_profile_matches_its_description(item):
    """Each fixed property really has the profile it claims (GPW v1 2022, WorldCover, SRTM)."""
    expect = item.expect
    stats = pasture.get_pasture_area(_feature_of(item), 2022)
    independent = _independent_profile(item)
    fraction = stats.pasture_ha / stats.area_ha

    print(f"\n{item.id}: pasture {fraction:.2f} cult={stats.cultivated_ha} nat={stats.natural_ha} "
          f"worldcover={ {k: round(v, 2) for k, v in independent['worldcover'].items() if v > 0.03} } "
          f"elevation={independent['elevation']:.0f}")
    low, high = expect["gpw_2022_pasture_fraction"]
    assert low <= fraction <= high
    dominant = expect["dominant_class"]
    if dominant == "cultivated":
        assert stats.cultivated_ha > stats.natural_ha
    elif dominant == "natural":
        assert stats.natural_ha > stats.cultivated_ha
    for cover, (low, high) in expect["worldcover"].items():
        assert low <= independent["worldcover"].get(cover, 0.0) <= high, cover
    if "elevation_min_m" in expect:
        assert independent["elevation"] >= expect["elevation_min_m"]


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_tools_over_global_properties(item):
    """Full chat flow on each property: register, measure (GPW and estimate) and map."""
    context = _register(item.name, item.latitude, item.longitude, item.radius_m)
    registered = dict(context.session_state)
    feature = _feature_of(item)

    official = pasture.get_pasture_area(feature, None)
    estimate = pasture.get_pasture_area(feature, 2024)
    text = pasture_tools.get_pasture_area(run_context=context, area_name=item.name.lower())
    start = time.perf_counter()
    result = pasture_tools.generate_pasture_map(run_context=context)
    seconds = time.perf_counter() - start

    for stats in (official, estimate):
        assert stats.area_ha == pytest.approx(feature.total_area, rel=0.10)
        assert stats.pasture_ha == pytest.approx(stats.cultivated_ha + stats.natural_ha, abs=0.02)
        assert 0 <= stats.pasture_ha <= stats.area_ha
    assert official.year == 2022 and official.source is pasture.PastureSource.GPW
    assert estimate.year == 2024 and estimate.source is pasture.PastureSource.CLASSIFICATION
    if item.is_control:
        assert official.pasture_ha <= 0.02 * official.area_ha
        assert estimate.pasture_ha <= 0.10 * estimate.area_ha
    assert f"{official.pasture_ha} ha" in text.content and "Global Pasture Watch" in text.content
    assert f"{official.pasture_ha} ha" in result.content
    assert result.images and Image.open(BytesIO(result.images[0].content)).size == (640, 640)
    assert context.session_state == registered
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        Image.open(BytesIO(result.images[0].content)).save(os.path.join(out, f"property_{item.id}_2022.png"))
    _summary.append({
        "id": item.id, "continent": item.continent, "kind": item.kind, "area": official.area_ha,
        "gpw": official.pasture_ha, "cult": official.cultivated_ha, "nat": official.natural_ha,
        "est": estimate.pasture_ha, "seconds": seconds,
    })


def test_estimate_map_carries_the_warning():
    item = next(item for item in PROPERTIES if item.id == "goias")
    context = _register(item.name, item.latitude, item.longitude, item.radius_m)

    result = pasture_tools.generate_pasture_map(run_context=context, year=2024)

    assert "experimental estimate" in result.content and result.images
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        Image.open(BytesIO(result.images[0].content)).save(os.path.join(out, "property_goias_2024.png"))


def test_concurrent_requests_share_one_measurement():
    feature = build_buffered_area(-33.0, -56.0, 800)

    start = time.perf_counter()
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: pasture.get_pasture_area(feature, 2024), range(4)))
    elapsed = time.perf_counter() - start

    print(f"\n4 concurrent calls: {elapsed:.1f}s")
    assert len({id(result) for result in results}) == 1


def test_classifier_agrees_with_global_pasture_watch_v1_1():
    """Validates the on-the-fly classification of 2023 and 2024 against v1-1 on many biomes."""
    accuracies, errors = [], []
    for item in PROPERTIES:
        site = item.id
        feature = build_buffered_area(item.latitude, item.longitude, benchmark.RADIUS_METERS)
        roi = pasture.feature_geometry(feature)
        key = pasture.geometry_key(feature)
        for year in benchmark.EVAL_YEARS:
            predicted = pasture._predicted_image(roi, year, key)
            result = benchmark.metrics(benchmark.confusion(roi, predicted, year))
            print(f"\n{site} {year}: acc={result['accuracy']:.3f} f1={result['f1']} "
                  f"truth={result['truth_ha']:.0f} pred={result['pred_ha']:.0f} ha")
            accuracies.append(result["accuracy"])
            if result["truth_ha"] > benchmark.MIN_TRUTH_FRACTION * result["area_ha"]:
                errors.append(result["rel_err"])

    print(f"\nmean accuracy={statistics.mean(accuracies):.3f} median relative error={statistics.median(errors):.3f}")
    assert statistics.mean(accuracies) >= MIN_MEAN_ACCURACY
    assert statistics.median(errors) <= MAX_MEDIAN_RELATIVE_ERROR
