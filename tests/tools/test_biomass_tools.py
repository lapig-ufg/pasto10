"""Tests of the biomass tools (domain/tools/biomass_tools.py).

No network and no Earth Engine: the service functions are replaced by fakes.
Both tools must leave ``session_state`` untouched.
"""

import copy

import pytest
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_tool_description, get_tool_result_text

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.services.geospatial.biomass import BiomassPeriodUnavailable, BiomassImage, NoPastureBiomass
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial.image import RenderedImage
from domain.tools import biomass_tools
from domain.utils.feature_utils import set_registered_features

ANNUAL = BiomassStats(
    source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01", end="2025-12-31", mean_t_ha=17.4,
    total_t=3571.3, p02_t_ha=12.1, p98_t_ha=23.2, pasture_ha=205.4, area_ha=312.5, scale_m=30, mask_year=2022,
    year=2025, days_covered=365,
)
MONTHLY = ANNUAL.model_copy(update={
    "source": BiomassSource.TIME2GRAZE, "period_label": "2026-07", "start": "2026-07-01", "end": "2026-07-31",
    "scale_m": 10, "mask_year": 2022, "year": 2026, "days_covered": 31, "n_images": 14,
})
BOTH = ["get_biomass_total", "generate_biomass_map"]


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
    stats = {"value": ANNUAL}

    def total(feature, year, month):
        calls.append((feature.get_metadata("center"), year, month))
        return stats["value"]

    monkeypatch.setattr(biomass_tools, "measure_total", total)
    monkeypatch.setattr(biomass_tools, "build_biomass", lambda feature, year, month: (
        calls.append((feature.get_metadata("center"), year, month)) or BiomassImage("image", None, None, ()), "roi"))
    monkeypatch.setattr(biomass_tools, "measure_biomass", lambda biomass, roi: stats["value"])
    monkeypatch.setattr(
        biomass_tools, "render_biomass_map",
        lambda feature, image, stats_: RenderedImage(b"\xff\xd8\xffjpeg", "jpeg", "image/jpeg"),
    )
    return calls, stats


@pytest.mark.parametrize("tool_name", BOTH)
def test_success_reports_period_source_figures_and_keeps_state(fake_service, tool_name):
    run_context = _context("North Paddock")
    before = copy.deepcopy(run_context.session_state)

    result = getattr(biomass_tools, tool_name)(run_context=run_context)

    for expected in ("North Paddock", "2025", "Global Pasture Watch", "17.4 t DM/ha", "3571.3 t DM", "205.4 ha", "stocking capacity"):
        assert expected in result.content
    assert "Global Pasture Watch grassland map of 2022" in result.content
    assert "Time2Graze: it covers" not in result.content
    assert bool(result.images) is (tool_name == "generate_biomass_map")
    assert run_context.session_state == before


@pytest.mark.parametrize("tool_name", BOTH)
def test_time2graze_success_warns_about_short_partial_coverage(fake_service, tool_name):
    fake_service[1]["value"] = MONTHLY

    result = getattr(biomass_tools, tool_name)(run_context=_context("A"), month=7)

    assert "Time2Graze" in result.content and "31 days" in result.content and "14 image" in result.content
    assert "Never compare annual with monthly" in result.content


def test_no_mask_note_when_the_mask_is_current(fake_service):
    fake_service[1]["value"] = ANNUAL.model_copy(update={"year": 2022, "mask_year": 2022})

    assert "grassland map of" not in biomass_tools.get_biomass_total(run_context=_context("A")).content


def test_name_year_and_month_are_forwarded(fake_service):
    run_context = _context("North", "South")

    biomass_tools.get_biomass_total(run_context=run_context, area_name="north", year=2020)
    biomass_tools.get_biomass_total(run_context=run_context, month=7)

    assert fake_service[0] == [("-15.5, -47.5", 2020, None), ("-14.5, -47.5", None, 7)]


@pytest.mark.parametrize("tool_name", BOTH)
def test_no_registered_area_and_unknown_name(fake_service, tool_name):
    tool = getattr(biomass_tools, tool_name)

    empty = tool(run_context=_context())
    unknown = tool(run_context=_context("North"), area_name="Lake")

    assert "no registered area" in empty.content and "Lake" in unknown.content
    assert not empty.images and not unknown.images and fake_service[0] == []


@pytest.mark.parametrize(
    "error, expected",
    [
        (BiomassPeriodUnavailable("year", "2000", "2025"), "from 2000 to 2025"),
        (BiomassPeriodUnavailable("month", "2026-06-27", "2026-08-21"), "2026-06-27 to 2026-08-21"),
        (NoPastureBiomass(), "no productive pasture"),
        (EarthEngineUnavailable("no creds"), "temporarily unavailable"),
        (RuntimeError("boom secret"), "unexpected error"),
    ],
)
@pytest.mark.parametrize("tool_name", BOTH)
def test_failures_use_yaml_texts_and_never_carry_figures(monkeypatch, tool_name, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(biomass_tools, "measure_total", fail)
    monkeypatch.setattr(biomass_tools, "build_biomass", fail)

    result = getattr(biomass_tools, tool_name)(run_context=_context("North"), month=7)

    assert expected in result.content and "secret" not in result.content and not result.images
    assert "Mean:" not in result.content


@pytest.mark.parametrize("tool_name", BOTH)
def test_descriptions_explain_the_source_rule(tool_name):
    description = get_tool_description("biomass_tools", tool_name)

    for expected in ("2025", "Time2Graze", "NOT standing biomass", "Never pick a month or a year on your own"):
        assert expected in description


def test_every_result_key_exists_for_both_tools():
    keys = [
        "success", "note_mask_year", "note_time2graze", "note_partial_year", "no_registered_area", "area_not_found", "year_unavailable",
        "month_unavailable", "no_pasture", "earth_engine_unavailable", "error",
    ]
    placeholders = dict(
        area_name="a", period="p", source="s", start="x", end="y", days=1, mean_t_ha=1, total_t=1, pasture_ha=1,
        area_ha=1, pasture_percent=1, scale_m=1, mask_year=1, year=1, n_images=1, partial=False, first_year=1,
        latest_year=1, t2g_start="a", t2g_end="b",
    )
    for tool_name in BOTH:
        for key in keys:
            assert get_tool_result_text("biomass_tools", tool_name, key, **placeholders)


@pytest.mark.parametrize("tool_name", BOTH)
def test_partial_year_of_time2graze_says_it_is_not_the_whole_year(fake_service, tool_name):
    fake_service[1]["value"] = MONTHLY.model_copy(update={
        "period_label": "2026", "start": "2026-06-27", "end": "2026-08-21", "days_covered": 56, "partial": True,
    })

    result = getattr(biomass_tools, tool_name)(run_context=_context("A"), year=2026)

    assert "NOT for the whole year" in result.content and "56 days" in result.content


def test_monthly_result_has_no_partial_year_note(fake_service):
    fake_service[1]["value"] = MONTHLY

    assert "NOT for the whole year" not in biomass_tools.get_biomass_total(run_context=_context("A"), month=7).content


def test_years_written_in_the_yaml_follow_the_service_constants():
    first, last = biomass_tools.GPP_FIRST_YEAR, biomass_tools.GPP_LAST_YEAR
    for tool_name in BOTH:
        description = get_tool_description("biomass_tools", tool_name)
        assert f"({last})" in description and f"({first}-{last}" in description
        text = get_tool_result_text(
            "biomass_tools", tool_name, "year_unavailable", first_year=first, latest_year=last,
        )
        assert f"from {first} to {last}" in text
    from semente.configs.prompts import get_agent_config

    assert f"year, {last}, is used" in get_agent_config("single_agent")["instructions_default"]


def test_circle_areas_and_raster_differences_are_noted_in_the_biomass_text(monkeypatch, fake_service):
    from domain.utils.feature_utils import get_registered_features

    run_context = _context("Test area")
    feature = get_registered_features(run_context.session_state).features[0]
    fake_service[1]["value"] = ANNUAL.model_copy(update={"area_ha": round(feature.total_area + 2.0, 2), "pasture_ha": 205.44})

    result = biomass_tools.get_biomass_total(run_context=run_context)

    assert "approximate circle, not a real property boundary" in result.content and "difference comes from the raster resolution" in result.content
    assert "205.4 ha" in result.content


@pytest.mark.parametrize("tool_name", BOTH)
def test_biomass_results_announce_the_latest_area_and_always_name_the_area_and_period(fake_service, tool_name):
    run_context = _context("North", "South")

    unnamed = getattr(biomass_tools, tool_name)(run_context=run_context)
    named = getattr(biomass_tools, tool_name)(run_context=run_context, area_name="north")

    assert "Using South, the user's most recently registered area" in unnamed.content and '"South"' in unnamed.content and "2025" in unnamed.content
    assert "most recently registered" not in named.content and '"North"' in named.content
