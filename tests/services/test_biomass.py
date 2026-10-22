"""Tests of the biomass service (domain/services/geospatial/biomass.py).

No Earth Engine: the small ``ee``-coupled functions are replaced by fakes.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from domain.schemas.biomass import BiomassSource
from domain.services.geospatial import biomass
from domain.services.geospatial.geometry import build_buffered_area

T2G_RANGE = (date(2026, 6, 27), date(2026, 8, 21))


class FakeImage:
    """Records the calls made on an ee.Image chain and returns itself."""

    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, args))
            return self

        return method


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(biomass.pasture.CACHE_DIR_ENV, raising=False)
    for cache in (biomass._stats_cache, biomass._range_cache):
        cache.clear()
    yield
    for cache in (biomass._stats_cache, biomass._range_cache):
        cache.clear()


@pytest.fixture
def fake_ee(monkeypatch):
    state = {"range": T2G_RANGE, "range_calls": 0, "images": 3, "reduce": [], "reduced": {
        "total": 3500.0, "valid": 200.0, "area": 312.5, "p2": 13.0, "p98": 20.0,
    }}

    def range_from_ee():
        state["range_calls"] += 1
        return state["range"]

    def reduce(image, roi, scale):
        state["reduce"].append(scale)
        return state["reduced"]

    monkeypatch.setattr(biomass, "_t2g_range_from_ee", range_from_ee)
    monkeypatch.setattr(biomass.pasture, "feature_geometry", lambda feature: "roi")
    monkeypatch.setattr(biomass, "initialize_ee", lambda: None)
    monkeypatch.setattr(biomass, "_mask_image", lambda mask_year: FakeImage())
    monkeypatch.setattr(biomass, "_gpp_biomass", lambda year: FakeImage())
    monkeypatch.setattr(biomass, "_t2g_biomass", lambda roi, period: FakeImage())
    monkeypatch.setattr(biomass, "_t2g_count", lambda roi, start, end: state["images"])
    monkeypatch.setattr(biomass, "_reduce_biomass", reduce)
    return state


def _feature():
    return build_buffered_area(-15.5, -47.5, 1000)


def test_constants_are_the_documented_ones():
    assert biomass.DRY_MATTER_PER_GC_M2 == 0.0135
    assert round(0.50 * 2.7 * 0.01, 6) == biomass.DRY_MATTER_PER_GC_M2
    assert (biomass.GPP_FIRST_YEAR, biomass.GPP_LAST_YEAR, biomass.MASK_LAST_YEAR) == (2000, 2025, 2022)


def test_default_is_latest_annual_gpw_year_with_older_mask(fake_ee):
    period = biomass.resolve_biomass_period(None, None)

    assert period.source is BiomassSource.GPW_ANNUAL and period.year == 2025
    assert period.mask_year == 2022 and period.scale == 30 and not period.partial
    assert (period.start, period.end, period.days) == (date(2025, 1, 1), date(2025, 12, 31), 365)
    assert fake_ee["range_calls"] == 0


@pytest.mark.parametrize("year, mask_year", [(2000, 2000), (2010, 2010), (2022, 2022), (2023, 2022), (2025, 2022)])
def test_mask_year_is_capped_at_the_last_grassland_map(fake_ee, year, mask_year):
    assert biomass.resolve_biomass_period(year, None).mask_year == mask_year


def test_month_uses_time2graze_in_the_latest_year(fake_ee):
    period = biomass.resolve_biomass_period(None, 7)

    assert period.source is BiomassSource.TIME2GRAZE and period.year == 2026 and period.month == 7
    assert (period.start, period.end, period.days) == (date(2026, 7, 1), date(2026, 7, 31), 31)
    assert period.scale == 10 and not period.partial and period.label == "2026-07"


@pytest.mark.parametrize("month, start, end", [(6, date(2026, 6, 27), date(2026, 6, 30)), (8, date(2026, 8, 1), date(2026, 8, 21))])
def test_month_at_the_edge_of_the_coverage_is_partial(fake_ee, month, start, end):
    period = biomass.resolve_biomass_period(2026, month)

    assert (period.start, period.end, period.partial) == (start, end, True)


def test_year_after_gpw_accumulates_the_covered_part_of_the_year(fake_ee):
    period = biomass.resolve_biomass_period(2026, None)

    assert period.source is BiomassSource.TIME2GRAZE and period.partial and period.month is None
    assert (period.start, period.end, period.days) == (T2G_RANGE[0], T2G_RANGE[1], 56)
    assert period.mask_year == 2022


@pytest.mark.parametrize("year, month, kind", [(2025, 7, "month"), (None, 13, "month"), (None, 0, "month"), (1999, None, "year"), (2030, None, "year")])
def test_unavailable_periods_never_fall_back_to_gpw(fake_ee, year, month, kind):
    with pytest.raises(biomass.BiomassPeriodUnavailable) as error:
        biomass.resolve_biomass_period(year, month)

    assert error.value.kind == kind


def test_month_unavailable_reports_the_time2graze_range(fake_ee):
    with pytest.raises(biomass.BiomassPeriodUnavailable) as error:
        biomass.resolve_biomass_period(2025, 7)

    assert (error.value.first, error.value.last) == ("2026-06-27", "2026-08-21")


def test_without_time2graze_data_months_are_unavailable(fake_ee):
    fake_ee["range"] = None

    with pytest.raises(biomass.BiomassPeriodUnavailable) as error:
        biomass.resolve_biomass_period(None, 7)

    assert error.value.kind == "month" and error.value.first == "none"


def test_time2graze_range_is_cached(fake_ee):
    for _ in range(3):
        biomass.t2g_date_range()

    assert fake_ee["range_calls"] == 1 and biomass._range_cache.ttl == 86_400


def test_time2graze_period_without_images_for_the_area_is_unavailable(fake_ee):
    fake_ee["images"] = 0

    with pytest.raises(biomass.BiomassPeriodUnavailable) as error:
        biomass.build_biomass(_feature(), None, 7)

    assert error.value.kind == "month"


def test_build_masks_with_grassland_and_positive_productivity(fake_ee, monkeypatch):
    calls = []
    monkeypatch.setattr(biomass, "_gpp_biomass", lambda year: FakeImage(calls))
    monkeypatch.setattr(biomass, "_mask_image", lambda mask_year: FakeImage(calls))

    image, _roi = biomass.build_biomass(_feature(), 2010, None)

    assert any(name == "updateMask" for name, _ in image.image.calls)
    assert any(name == "gt" and args == (0,) for name, args in calls)
    assert image.period.mask_year == 2010 and image.n_images is None


def test_gpp_biomass_applies_the_conversion_factor(monkeypatch):
    calls = []

    class Collection:
        def filter(self, _):
            return self

        def size(self):
            return type("Size", (), {"getInfo": staticmethod(lambda: 1)})()

        def first(self):
            return FakeImage(calls)

    monkeypatch.setattr(biomass.ee, "ImageCollection", lambda name: Collection())
    monkeypatch.setattr(biomass.ee.Filter, "eq", staticmethod(lambda *args: None))

    biomass._gpp_biomass(2020)

    assert ("multiply", (0.0135,)) in calls


def test_time2graze_biomass_scales_by_days_and_unit(monkeypatch):
    calls = []

    class Collection(FakeImage):
        pass

    monkeypatch.setattr(biomass, "_t2g_collection", lambda roi, start, end: Collection(calls))
    period = biomass.resolve_biomass_period.__globals__["BiomassPeriod"](
        BiomassSource.TIME2GRAZE, 2026, 7, date(2026, 7, 1), date(2026, 7, 31), 2022, 10, False, "2026-07",
    )

    biomass._t2g_biomass("roi", period)

    factor = [args[0] for name, args in calls if name == "multiply"][0]
    assert factor == pytest.approx(0.1 * 31 * 0.0135)


def test_measure_returns_weighted_mean_total_and_percentiles(fake_ee):
    image, roi = biomass.build_biomass(_feature(), 2020, None)

    stats = biomass.measure_biomass(image, roi)

    assert stats.total_t == 3500.0 and stats.pasture_ha == 200.0 and stats.area_ha == 312.5
    assert stats.mean_t_ha == 17.5
    assert stats.mean_t_ha * stats.pasture_ha == pytest.approx(stats.total_t)
    assert (stats.p02_t_ha, stats.p98_t_ha) == (13.0, 20.0)
    assert stats.pasture_percent == 64.0
    assert stats.year == 2020 and stats.mask_year == 2020 and stats.source is BiomassSource.GPW_ANNUAL


@pytest.mark.parametrize("valid", [0.0, None, 1.0])
def test_no_productive_pasture_raises(fake_ee, valid):
    fake_ee["reduced"] = {"total": 5.0, "valid": valid, "area": 312.5, "p2": None, "p98": None}
    image, roi = biomass.build_biomass(_feature(), 2020, None)

    with pytest.raises(biomass.NoPastureBiomass):
        biomass.measure_biomass(image, roi)


def test_time2graze_stats_carry_period_and_image_count(fake_ee):
    stats = biomass.get_biomass_total(_feature(), None, 6)

    assert stats.source is BiomassSource.TIME2GRAZE and stats.partial and stats.n_images == 3
    assert (stats.start, stats.end, stats.days_covered, stats.scale_m) == ("2026-06-27", "2026-06-30", 4, 10)
    assert fake_ee["reduce"] == [10]


def test_results_are_cached_per_geometry_period_and_method(fake_ee):
    feature = _feature()

    first = biomass.get_biomass_total(feature, 2020, None)
    second = biomass.get_biomass_total(feature, 2020, None)
    biomass.get_biomass_total(feature, None, 7)
    moved = build_buffered_area(-16.5, -47.5, 1000).model_copy(update={"feature_id": feature.feature_id})
    biomass.get_biomass_total(moved, 2020, None)

    assert first is second and len(fake_ee["reduce"]) == 3
    keys = biomass._stats_cache.keys()
    assert all(key[-1] == biomass.METHOD_VERSION for key in keys)
    assert {key[4] for key in keys} >= {"2020-12-31", "2026-07-31"}


def test_concurrent_requests_measure_once(fake_ee, monkeypatch):
    def slow(image, roi, scale):
        fake_ee["reduce"].append(scale)
        time.sleep(0.2)
        return fake_ee["reduced"]

    monkeypatch.setattr(biomass, "_reduce_biomass", slow)
    feature = _feature()

    with ThreadPoolExecutor(8) as executor:
        results = list(executor.map(lambda _: biomass.get_biomass_total(feature, 2020, None), range(8)))

    assert len({id(result) for result in results}) == 1 and len(fake_ee["reduce"]) == 1


def test_disk_cache_skips_earth_engine(fake_ee, monkeypatch, tmp_path):
    monkeypatch.setenv(biomass.pasture.CACHE_DIR_ENV, str(tmp_path))
    feature = _feature()

    first = biomass.get_biomass_total(feature, 2020, None)
    biomass._stats_cache.clear()
    second = biomass.get_biomass_total(feature, 2020, None)

    assert second == first and len(fake_ee["reduce"]) == 1 and len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize("year, month", [(0, 7), (-5, None), (10000, 7), (0, None)])
def test_invalid_calendar_years_are_unavailable_not_value_errors(fake_ee, year, month):
    with pytest.raises(biomass.BiomassPeriodUnavailable) as error:
        biomass.resolve_biomass_period(year, month)

    assert error.value.kind == "year"


def test_time2graze_end_day_is_inclusive_in_the_day_count(fake_ee):
    period = biomass.resolve_biomass_period(2026, None)

    assert (period.start, period.end) == (date(2026, 6, 27), date(2026, 8, 21))
    assert period.days == 56
    assert biomass.BiomassPeriod.days.fget(
        biomass.BiomassPeriod(BiomassSource.GPW_ANNUAL, 2024, None, date(2024, 1, 1), date(2024, 12, 31), 2022, 30, False, "2024")
    ) == 366
