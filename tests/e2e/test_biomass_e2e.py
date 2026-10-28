"""End-to-end tests of the biomass tools over the 15 fixed global properties.

Real Earth Engine (needs ``GEE_*``; skipped otherwise). For each property it
runs the chat flow (register, then measure and map the registered area) with
the default (the most recent data available for the area), the latest annual year, an old year and a Time2Graze
month, checks plausibility (mean between 0 and 25 t DM/ha, total about
mean x pasture hectares (within the rounding of the mean)), that unavailable Time2Graze months are reported
without crashing or falling back, that the negative controls have no
pasture, and prints a summary table. Run with ``uv run pytest -m e2e -s``;
set ``PASTO10_E2E_OUT`` to keep the maps.
"""

import os
import time
from io import BytesIO

import pytest
from PIL import Image
from semente.backends.toolkit import StateContext

from domain.schemas.biomass import BiomassSource
from domain.services.geospatial import biomass
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import biomass_tools, property_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties

pytestmark = pytest.mark.e2e

_REQUIRED_ENV = ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")
PROPERTIES = load_global_properties()
PROPERTY_IDS = [item.id for item in PROPERTIES]
MAX_PLAUSIBLE_MEAN_T_HA = 25.0
OLD_YEAR = 2005
_summary: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing environment variables: {', '.join(missing)}")


@pytest.fixture(scope="module", autouse=True)
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(15) + "kind".ljust(15) + "default".ljust(10) + "source".ljust(12) + "mean".rjust(8) + "total t".rjust(10)
              + "partial".rjust(9) + "2025 t/ha".rjust(10) + "2005 t/ha".rjust(10) + "  Jul-2026".ljust(24) + "map(s)")
        for row in _summary:
            print(row["id"].ljust(15) + row["kind"].ljust(15) + row["period"].ljust(10) + row["source"].ljust(12) + row["mean"].rjust(8)
                  + row["total"].rjust(10) + row["partial"].rjust(9) + row["annual"].rjust(10)
                  + row["old"].rjust(10) + ("  " + row["month"]).ljust(24) + row["seconds"])


def _register(item: GlobalProperty) -> StateContext:
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=item.latitude, longitude=item.longitude, radius=item.radius_m,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=item.name)
    return context


def _feature(item: GlobalProperty):
    return build_buffered_area(item.latitude, item.longitude, item.radius_m)


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_biomass_tools_over_global_properties(item):
    context = _register(item)
    registered = dict(context.session_state)
    feature = _feature(item)
    row = {"id": item.id, "kind": item.kind, "mean": "-", "total": "-", "old": "-", "month": "-", "seconds": "-",
           "period": "-", "source": "-", "partial": "-", "annual": "-"}
    _summary.append(row)

    if item.is_control:
        for kwargs in ({}, {"year": OLD_YEAR}):
            with pytest.raises(biomass.NoPastureBiomass):
                biomass.get_biomass_total(feature, **kwargs)
            result = biomass_tools.get_biomass_total(run_context=context, **kwargs)
            assert "no productive pasture" in result.content and "Mean:" not in result.content
        assert not biomass_tools.generate_biomass_map(run_context=context).images
        row["mean"] = "no_pasture"
        assert context.session_state == registered
        return

    latest = biomass.get_biomass_total(feature)
    stats = biomass.get_biomass_total(feature, year=2025)
    old = biomass.get_biomass_total(feature, year=OLD_YEAR)
    assert latest.source in (BiomassSource.TIME2GRAZE, BiomassSource.GPW_ANNUAL)
    if latest.source is BiomassSource.GPW_ANNUAL:
        assert latest == stats
    else:
        assert latest.scale_m == 10 and latest.n_images > 0 and latest.period_label == f"{latest.year}-{int(latest.end[5:7]):02d}"
        assert 0 < latest.mean_t_ha <= MAX_PLAUSIBLE_MEAN_T_HA / 12
        assert latest.total_t == pytest.approx(latest.mean_t_ha * latest.pasture_ha, rel=0.01, abs=0.006 * latest.pasture_ha)
    for current in (stats, old):
        assert current.source is BiomassSource.GPW_ANNUAL and not current.partial
        assert 0 < current.mean_t_ha <= MAX_PLAUSIBLE_MEAN_T_HA
        assert current.total_t == pytest.approx(current.mean_t_ha * current.pasture_ha, rel=0.01, abs=0.006 * current.pasture_ha)
        assert 0 < current.pasture_ha <= current.area_ha == pytest.approx(feature.total_area, rel=0.10)
        assert current.p02_t_ha <= current.mean_t_ha <= current.p98_t_ha * 1.05
    assert (stats.year, stats.mask_year, old.year, old.mask_year) == (2025, 2022, OLD_YEAR, OLD_YEAR)

    text = biomass_tools.get_biomass_total(run_context=context, area_name=item.name.lower())
    assert f"{latest.mean_t_ha} t DM/ha" in text.content and latest.period_label in text.content
    assert ("most recent data available" in text.content) == (latest.source is BiomassSource.TIME2GRAZE)
    assert ("no productive pasture over this area" in text.content) == (latest.source is BiomassSource.GPW_ANNUAL)
    assert "Global Pasture Watch grassland map of 2022" in text.content
    explicit = biomass_tools.get_biomass_total(run_context=context, area_name=item.name.lower(), year=2025)
    assert f"{stats.mean_t_ha} t DM/ha" in explicit.content and "gave no period" not in explicit.content

    start = time.perf_counter()
    result = biomass_tools.generate_biomass_map(run_context=context)
    seconds = time.perf_counter() - start
    assert result.images and Image.open(BytesIO(result.images[0].content)).size == (640, 640)
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        Image.open(BytesIO(result.images[0].content)).save(os.path.join(out, f"biomass_{item.id}_latest.png"))

    month_text = biomass_tools.get_biomass_total(run_context=context, month=7)
    if "Time2Graze: it covers" in month_text.content or "comes from Time2Graze" in month_text.content:
        monthly = biomass.get_biomass_total(feature, month=7)
        assert monthly.source is BiomassSource.TIME2GRAZE and monthly.scale_m == 10 and monthly.n_images > 0
        assert 0 < monthly.mean_t_ha <= MAX_PLAUSIBLE_MEAN_T_HA / 12
        assert monthly.total_t == pytest.approx(monthly.mean_t_ha * monthly.pasture_ha, rel=0.01, abs=0.006 * monthly.pasture_ha)
        month_map = biomass_tools.generate_biomass_map(run_context=context, month=7)
        assert month_map.images
        if out:
            Image.open(BytesIO(month_map.images[0].content)).save(os.path.join(out, f"biomass_{item.id}_2026-07.png"))
        row["month"] = f"{monthly.mean_t_ha} t/ha ({monthly.n_images} img)"
    else:
        assert "no Time2Graze data" in month_text.content and "Mean:" not in month_text.content
        row["month"] = "month_unavailable"

    assert context.session_state == registered
    row.update(
        mean=str(latest.mean_t_ha), total=str(latest.total_t), old=str(old.mean_t_ha), seconds=f"{seconds:.1f}",
        period=latest.period_label, source="T2G" if latest.source is BiomassSource.TIME2GRAZE else "GPW(2025)",
        partial=str(latest.partial), annual=str(stats.mean_t_ha),
    )


def test_requests_that_cannot_be_served_never_fall_back_to_another_source():
    context = _register(PROPERTIES[0])

    for kwargs, key in (({"year": 1990}, "from 2000 to 2025"), ({"year": 2025, "month": 7}, "no Time2Graze data"), ({"year": 0, "month": 7}, "from 2000 to 2025")):
        result = biomass_tools.get_biomass_total(run_context=context, **kwargs)
        assert key in result.content and "Mean:" not in result.content


def test_partial_year_from_time2graze_is_reported_as_partial():
    context = _register(PROPERTIES[0])

    result = biomass_tools.get_biomass_total(run_context=context, year=2026)

    assert "NOT for the whole year" in result.content and "Time2Graze" in result.content
    assert "2026-06-27 to 2026-08-21, 56 days" in result.content
