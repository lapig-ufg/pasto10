"""End-to-end tests of the fallback for pasture and biomass requests outside the available coverage.

Real Earth Engine (needs ``GEE_*``; skipped otherwise), on the ``goias`` and ``pampas_uy``
test properties. A year or month outside the coverage must not fail: the tools return the
oldest available data for earlier requests and the most recent for later ones, and the text
says which period was actually used, that it differs from the request and the coverage.
Weather is never affected. Run with ``uv run pytest -m e2e``.
"""

import os
from datetime import date

import pytest
from semente.backends.toolkit import StateContext

from domain.services.geospatial import biomass, pasture
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import biomass_tools, pasture_tools, property_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties

pytestmark = pytest.mark.e2e

_REQUIRED_ENV = ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")
PROPERTIES = [item for item in load_global_properties() if item.id in ("goias", "pampas_uy")]
IDS = [item.id for item in PROPERTIES]


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing environment variables: {', '.join(missing)}")


def _register(item: GlobalProperty) -> StateContext:
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=item.latitude, longitude=item.longitude, radius=item.radius_m,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=item.name)
    return context


def _month_of(day: date) -> str:
    return f"{day.year}-{day.month:02d}"


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_pasture_year_before_the_coverage_gives_the_oldest_year(item):
    context = _register(item)

    result = pasture_tools.get_pasture_area(run_context=context, year=1990)

    assert "for 2000 (source: Global Pasture Watch)" in result.content
    assert "asked for 1990" in result.content and "not for 1990" in result.content and "2000 to " in result.content


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_pasture_year_after_the_coverage_gives_the_latest_year(item):
    context = _register(item)
    first, latest = pasture.pasture_coverage(_feature_of(item))

    result = pasture_tools.get_pasture_area(run_context=context, year=2100)

    assert f"for {latest} (source: " in result.content and first == 2000
    assert "asked for 2100" in result.content and "not for 2100" in result.content and f"2000 to {latest}" in result.content


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_pasture_map_outside_the_coverage_is_labeled_with_the_effective_year(item):
    context = _register(item)

    result = pasture_tools.generate_pasture_map(run_context=context, year=1990)

    assert result.images and "2000" in result.content and "not for 1990" in result.content


def _feature_of(item: GlobalProperty):
    return build_buffered_area(item.latitude, item.longitude, item.radius_m)


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_biomass_year_before_the_coverage_gives_the_oldest_year(item):
    context = _register(item)

    result = biomass_tools.get_biomass_total(run_context=context, year=1990)

    assert "period 2000 " in result.content and "Global Pasture Watch" in result.content
    assert "asked for 1990" in result.content and "not for 1990" in result.content and "Mean:" in result.content


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_biomass_year_after_the_coverage_gives_the_latest_year(item):
    context = _register(item)
    coverage = biomass.biomass_coverage()

    result = biomass_tools.get_biomass_total(run_context=context, year=2100)

    assert f"period {coverage.last_year} " in result.content
    assert "asked for 2100" in result.content and "not for 2100" in result.content and "2000 to 2025" in result.content and coverage.t2g_end in result.content


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_biomass_month_after_the_coverage_gives_the_last_month(item):
    context = _register(item)
    available = biomass.t2g_date_range()

    result = biomass_tools.get_biomass_total(run_context=context, year=2030, month=1)

    assert available is not None
    if "Mean:" in result.content:
        assert f"period {_month_of(available[1])} " in result.content
        assert "asked for 2030-01" in result.content and "not for 2030-01" in result.content
        assert available[0].isoformat() in result.content and available[1].isoformat() in result.content
    else:
        assert "no Time2Graze image over this area" in result.content


@pytest.mark.parametrize("item", PROPERTIES, ids=IDS)
def test_biomass_month_before_the_coverage_gives_the_first_month(item):
    context = _register(item)
    available = biomass.t2g_date_range()

    result = biomass_tools.get_biomass_total(run_context=context, year=2020, month=1)

    if "Mean:" in result.content:
        assert f"period {_month_of(available[0])} " in result.content
        assert "asked for 2020-01" in result.content and "not for 2020-01" in result.content
        assert available[0].isoformat() in result.content and available[1].isoformat() in result.content
    else:
        assert "no Time2Graze image over this area" in result.content


def test_biomass_map_outside_the_coverage_is_labeled_with_the_effective_period():
    context = _register(PROPERTIES[0])

    result = biomass_tools.generate_biomass_map(run_context=context, year=1990)

    assert result.images and "period 2000 " in result.content and "not for 1990" in result.content


def test_zero_year_counts_as_not_given_and_is_not_adjusted():
    context = _register(PROPERTIES[0])

    result = biomass_tools.get_biomass_total(run_context=context, year=0)

    assert "Mean:" in result.content and "outside the available coverage" not in result.content


def test_requests_inside_the_coverage_are_not_adjusted():
    context = _register(PROPERTIES[0])

    pasture_result = pasture_tools.get_pasture_area(run_context=context, year=2000)
    biomass_result = biomass_tools.get_biomass_total(run_context=context, year=2025)

    assert "closest available year" not in pasture_result.content
    assert "outside the available coverage" not in biomass_result.content
