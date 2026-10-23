"""Optional network check of the OpenStreetMap candidate coverage over the 15 fixed test properties.

Needs only network access (no Earth Engine). Overpass mirrors are often busy, so an unavailable service skips
the property instead of failing; the test only requires that the source never crashes, that every candidate is a
valid polygon within the configured area limits, and it prints a summary table (candidates, latency, tags).
Run with ``uv run pytest -m e2e -s tests/e2e/test_osm_coverage_e2e.py``.
"""

import time

import pytest

from domain.services.property_sources import osm_source
from domain.services.property_sources.base import PropertySourceError
from tests.fixtures.global_properties import load_global_properties

pytestmark = pytest.mark.e2e

PROPERTIES = load_global_properties()
_summary: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(18) + "result".ljust(14) + "cand".rjust(5) + "inside".rjust(7) + "secs".rjust(7) + "  tags")
        for row in _summary:
            print(row["id"].ljust(18) + row["result"].ljust(14) + str(row["n"]).rjust(5) + str(row["inside"]).rjust(7) + row["secs"].rjust(7) + "  " + row["tags"])


@pytest.mark.parametrize("item", PROPERTIES, ids=[item.id for item in PROPERTIES])
def test_osm_candidates_around_each_property(item):
    source = osm_source.OsmSource()
    start = time.perf_counter()
    try:
        candidates, result = source.find_by_point(item.latitude, item.longitude), "ok"
    except PropertySourceError as error:
        candidates, result = [], error.kind
    seconds = time.perf_counter() - start
    _summary.append({
        "id": item.id, "result": result, "n": len(candidates), "inside": sum(1 for c in candidates if c.distance_m == 0),
        "secs": f"{seconds:.1f}", "tags": ",".join(sorted({a.value for c in candidates for a in c.attributes if a.key in ("osm_landuse", "osm_place")})),
    })
    if result in ("unavailable", "timeout"):
        pytest.skip(f"Overpass {result}")
    assert result in ("ok", "urban_area")
    for candidate in candidates:
        assert osm_source.MIN_AREA_HA <= candidate.area_ha <= osm_source.MAX_AREA_HA
        assert candidate.to_feature().total_area == pytest.approx(candidate.area_ha, abs=0.01)
    assert len(candidates) <= osm_source.MAX_CANDIDATES
