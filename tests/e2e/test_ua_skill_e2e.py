"""Arithmetic check of the ua-calculator skill formulas on real biomass figures (needs ``GEE_*``).

Reproduces the skill's formulas (1 AU = 450 kg, 2.5% of live weight per day,
protection factor 2, annualization with 365 / days covered) on the biomass of
four fixed properties and checks that the resulting carrying capacity is
plausible (0.1 to 10 AU/ha).
"""

import os

import pytest

from domain.schemas.biomass import BiomassSource
from domain.services.geospatial import biomass
from domain.services.geospatial.geometry import build_buffered_area
from tests.fixtures.global_properties import load_global_properties

pytestmark = pytest.mark.e2e

AU_KG = 450
INTAKE_PER_DAY = 0.025
PROTECTION_FACTOR = 2
DEMAND_T_DM_PER_AU_YEAR = AU_KG * INTAKE_PER_DAY * 365 / 1000 * PROTECTION_FACTOR
PROPERTY_IDS = ("goias", "ireland", "kenya", "new_zealand")
_PROPERTIES = {item.id: item for item in load_global_properties()}


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    if any(not os.getenv(name) for name in ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")):
        pytest.skip("missing GEE_* environment variables")


def _capacity(annual_forage_t: float, pasture_ha: float) -> tuple[float, float]:
    total_au = annual_forage_t / DEMAND_T_DM_PER_AU_YEAR
    return total_au, total_au / pasture_ha


def test_demand_with_protection_is_the_skill_value():
    assert round(AU_KG * INTAKE_PER_DAY * 365 / 1000, 1) == 4.1
    assert round(DEMAND_T_DM_PER_AU_YEAR, 1) == 8.2


@pytest.mark.parametrize("property_id", PROPERTY_IDS)
def test_annual_capacity_is_plausible(property_id):
    item = _PROPERTIES[property_id]
    stats = biomass.get_biomass_total(build_buffered_area(item.latitude, item.longitude, item.radius_m))

    total_au, per_ha = _capacity(stats.total_t, stats.pasture_ha)

    print(f"\n{property_id}: {stats.total_t} t DM/year over {stats.pasture_ha} ha -> {total_au:.0f} AU, {per_ha:.2f} AU/ha")
    assert stats.days_covered in (365, 366)
    assert 0.1 <= per_ha <= 10
    assert per_ha == pytest.approx(stats.mean_t_ha / DEMAND_T_DM_PER_AU_YEAR, rel=0.02)


@pytest.mark.parametrize("property_id", ["goias"])
def test_annualized_time2graze_stays_within_an_order_of_magnitude_of_the_annual_value(property_id):
    item = _PROPERTIES[property_id]
    feature = build_buffered_area(item.latitude, item.longitude, item.radius_m)
    annual = biomass.get_biomass_total(feature)
    monthly = biomass.get_biomass_total(feature, month=7)

    annualized = monthly.total_t * 365 / monthly.days_covered
    wrong_times_twelve = monthly.total_t * 12

    print(f"\n{property_id}: annual {annual.total_t} t, annualized July {annualized:.0f} t (x12 would be {wrong_times_twelve:.0f})")
    assert monthly.source is BiomassSource.TIME2GRAZE
    assert 0.1 * annual.total_t <= annualized <= 10 * annual.total_t
    assert annualized == pytest.approx(wrong_times_twelve * 365 / (12 * monthly.days_covered))
