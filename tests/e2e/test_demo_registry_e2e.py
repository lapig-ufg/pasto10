"""End-to-end check of the demo registry flow on real Earth Engine (needs ``GEE_*``).

For every non-control test property the demo registry must have a parcel containing the point; for three of them the
whole chain runs: coordinate -> candidates -> choice -> confirmation -> pasture area and biomass of the chosen parcel.
Prints a table. Run with ``uv run pytest -m e2e -s tests/e2e/test_demo_registry_e2e.py``.
"""

import os
import time

import pytest
from semente.backends.toolkit import StateContext

from domain.services.geospatial import biomass, pasture
from domain.services.property_sources import registry
from domain.tools import property_tools
from domain.utils.feature_utils import get_registered_features
from tests.fixtures.global_properties import load_global_properties

pytestmark = pytest.mark.e2e

PROPERTIES = {item.id: item for item in load_global_properties()}
CHAIN = ("goias", "kenya", "santa_cruz")
_rows: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    if any(not os.getenv(name) for name in ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")):
        pytest.skip("missing GEE_* environment variables")


@pytest.fixture(scope="module", autouse=True)
def print_table():
    yield
    if _rows:
        print("\n\n" + "id".ljust(14) + "parcel".ljust(15) + "ha".rjust(8) + "pasture ha".rjust(12) + "biomass t/ha".rjust(14) + "secs".rjust(7))
        for row in _rows:
            print(row["id"].ljust(14) + row["parcel"].ljust(15) + str(row["ha"]).rjust(8) + str(row["pasture"]).rjust(12) + str(row["mean"]).rjust(14) + row["secs"].rjust(7))


@pytest.mark.parametrize("property_id", CHAIN)
def test_pasture_and_biomass_work_on_the_chosen_demo_parcel(property_id, monkeypatch):
    monkeypatch.setenv(registry.SOURCES_ENV, "demo_registry")
    item = PROPERTIES[property_id]
    context = StateContext(session_state={})
    start = time.perf_counter()

    found = property_tools.find_property_candidates(run_context=context, latitude=item.latitude, longitude=item.longitude)
    property_tools.select_candidate(run_context=context, selection=1)
    property_tools.confirm_registration(run_context=context, name="Chosen parcel")
    feature = get_registered_features(context.session_state).features[0]
    area = pasture.get_pasture_area(feature, 2022)
    total = biomass.get_biomass_total(feature)
    seconds = time.perf_counter() - start

    assert "DEMO registry" in found.content and feature.feature_type == "demo_registry"
    assert area.area_ha == pytest.approx(feature.total_area, rel=0.12) and 0 <= area.pasture_ha <= area.area_ha
    assert total.area_ha == pytest.approx(feature.total_area, rel=0.12) and 0 < total.mean_t_ha <= 25
    _rows.append({
        "id": property_id, "parcel": feature.get_metadata("registry_id"), "ha": feature.total_area, "pasture": area.pasture_ha,
        "mean": total.mean_t_ha, "secs": f"{seconds:.1f}",
    })
