"""Tests of the bulletin tool (domain/tools/report_tools.py).

The pasture and biomass services and renderers are replaced by counted fakes,
so the tests also prove the bulletin computes nothing new. ``session_state``
must stay untouched.
"""

import copy
import re
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader
from semente.backends.toolkit import StateContext
from semente.configs.prompts import _load_yaml, get_tool_description

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.biomass import BiomassImage, BiomassPeriodUnavailable, NoPastureBiomass
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.geometry import build_buffered_area
from domain.services.geospatial.image import RenderedImage
from domain.services.geospatial.pasture import PastureClassification, YearUnavailable
from domain.tools import report_tools
from domain.utils.feature_utils import set_registered_features

PASTURE = PastureAreaStats(
    year=2022, source=PastureSource.GPW, pasture_ha=250.25, cultivated_ha=200.5, natural_ha=49.75, area_ha=313.5,
)
BIOMASS = BiomassStats(
    source=BiomassSource.GPW_ANNUAL, period_label="2025", start="2025-01-01", end="2025-12-31", mean_t_ha=17.39,
    total_t=3571.31, p02_t_ha=12.1, p98_t_ha=23.2, pasture_ha=205.4, area_ha=312.5, scale_m=30, mask_year=2022,
    year=2025, days_covered=365,
)
FORBIDDEN = re.compile(r"Brasil|Brazil|\bCAR\b|SICAR|MapBiomas|Embrapa|Lapig|UFG", re.IGNORECASE)


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (640, 640), (60, 140, 70)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _context(name="North Paddock") -> StateContext:
    feature = build_buffered_area(-2.0, 37.0, 500)
    if name:
        feature.metadata.append(FeatureMetadata(key="name", value=name))
    state = {}
    set_registered_features(state, RegisteredFeatures(features=[feature]))
    return StateContext(session_state=state)


def _pages(result) -> list[str]:
    return [page.extract_text() for page in PdfReader(BytesIO(result.files[0].content)).pages]


@pytest.fixture
def services(monkeypatch):
    calls = {"classify": 0, "measure_pasture": 0, "render_pasture": 0, "build_biomass": 0, "measure_biomass": 0, "render_biomass": 0}
    errors = {"pasture": None, "biomass": None}
    stats = {"pasture": PASTURE, "biomass": BIOMASS}

    def classify(feature, year):
        calls["classify"] += 1
        assert year is None
        if errors["pasture"]:
            raise errors["pasture"]
        return PastureClassification("image", 2022, PastureSource.GPW, 30, ()), "roi"

    def build_biomass(feature, year, month):
        calls["build_biomass"] += 1
        assert year is None and month is None
        if errors["biomass"]:
            raise errors["biomass"]
        return BiomassImage("image", None, None, ()), "roi"

    monkeypatch.setattr(report_tools, "classify_pasture", classify)
    monkeypatch.setattr(report_tools, "measure_pasture_area", lambda c, r: calls.__setitem__("measure_pasture", calls["measure_pasture"] + 1) or stats["pasture"])
    monkeypatch.setattr(report_tools, "render_pasture_map", lambda f, i, s, sc: calls.__setitem__("render_pasture", calls["render_pasture"] + 1) or RenderedImage(_jpeg(), "jpeg", "image/jpeg"))
    monkeypatch.setattr(report_tools, "build_biomass", build_biomass)
    monkeypatch.setattr(report_tools, "measure_biomass", lambda b, r: calls.__setitem__("measure_biomass", calls["measure_biomass"] + 1) or stats["biomass"])
    monkeypatch.setattr(report_tools, "render_biomass_map", lambda f, i, s: calls.__setitem__("render_biomass", calls["render_biomass"] + 1) or RenderedImage(_jpeg(), "jpeg", "image/jpeg"))
    return calls, errors, stats


def test_full_bulletin_is_a_short_pdf_with_name_and_numbers(services):
    run_context = _context()
    before = copy.deepcopy(run_context.session_state)

    result = report_tools.generate_area_bulletin(run_context=run_context)

    file = result.files[0]
    assert file.content.startswith(b"%PDF") and file.mime_type == "application/pdf" and file.format == "pdf"
    assert file.name == "bulletin_north-paddock.pdf" and "bulletin_north-paddock.pdf" in result.content
    pages = _pages(result)
    assert 1 <= len(pages) <= 3
    text = "\n".join(pages)
    for expected in ("North Paddock", "-2.00000, 37.00000", "500 m", "250.25 ha", "200.5 ha", "49.75 ha", "2022",
                     "17.39 t DM/ha", "3571.31 t DM", "205.4 ha", "2025", "Global Pasture Watch", "not standing biomass"):
        assert expected in text, expected
    assert "Not available" not in text
    assert run_context.session_state == before


def test_bulletin_has_no_country_or_institution_specific_text(services):
    text = "\n".join(_pages(report_tools.generate_area_bulletin(run_context=_context("Savanna Plot"))))

    assert not FORBIDDEN.search(text)


def test_bulletin_computes_nothing_new(services):
    calls, _, _ = services

    report_tools.generate_area_bulletin(run_context=_context())

    assert calls == {"classify": 1, "measure_pasture": 1, "render_pasture": 1, "build_biomass": 1, "measure_biomass": 1, "render_biomass": 1}


@pytest.mark.parametrize(
    "error", [NoPastureBiomass(), BiomassPeriodUnavailable("year", "2000", "2025"), EarthEngineUnavailable("x"), RuntimeError("boom secret")],
)
def test_biomass_failure_gives_a_partial_pdf_with_an_unavailable_section(services, error):
    _, errors, _ = services
    errors["biomass"] = error

    result = report_tools.generate_area_bulletin(run_context=_context())

    text = "\n".join(_pages(result))
    assert "Not available" in text and "250.25 ha" in text and "17.39" not in text and "secret" not in text
    assert "NOT available" in result.content and "biomass" in result.content


def test_pasture_failure_gives_a_partial_pdf(services):
    _, errors, _ = services
    errors["pasture"] = YearUnavailable(latest=2022)

    result = report_tools.generate_area_bulletin(run_context=_context())

    text = "\n".join(_pages(result))
    assert "Not available" in text and "17.39 t DM/ha" in text and "250.25" not in text
    assert "pasture" in result.content


def test_nothing_available_means_no_pdf(services):
    _, errors, _ = services
    errors["pasture"], errors["biomass"] = YearUnavailable(latest=2022), NoPastureBiomass()

    result = report_tools.generate_area_bulletin(run_context=_context())

    assert not result.files and "no bulletin was generated" in result.content


def test_earth_engine_down_means_no_pdf_and_its_own_text(services):
    _, errors, _ = services
    errors["pasture"] = errors["biomass"] = EarthEngineUnavailable("no creds")

    result = report_tools.generate_area_bulletin(run_context=_context())

    assert not result.files and "temporarily unavailable" in result.content


def test_estimate_warning_and_time2graze_notes_appear_when_relevant(services):
    _, _, stats = services
    stats["pasture"] = PASTURE.model_copy(update={"year": 2024, "source": PastureSource.CLASSIFICATION})
    stats["biomass"] = BIOMASS.model_copy(update={"source": BiomassSource.TIME2GRAZE, "year": 2026, "period_label": "2026-07", "days_covered": 31, "n_images": 7, "partial": True})

    text = "\n".join(_pages(report_tools.generate_area_bulletin(run_context=_context())))

    for expected in ("Experimental estimate", "Time2Graze", "31 days", "grassland map of 2022"):
        assert expected in text


def test_no_registered_area_and_unknown_name(services):
    empty = report_tools.generate_area_bulletin(run_context=StateContext(session_state={}))
    unknown = report_tools.generate_area_bulletin(run_context=_context(), area_name="Lake")

    assert "no registered area" in empty.content and "Lake" in unknown.content and not empty.files and not unknown.files


def test_unexpected_error_is_generic(services, monkeypatch):
    monkeypatch.setattr(report_tools, "render_buffer_image", lambda feature: (_ for _ in ()).throw(RuntimeError("boom secret")))

    result = report_tools.generate_area_bulletin(run_context=_context())

    assert "unexpected error" in result.content and "secret" not in result.content and not result.files


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Finca La Esperanza, Santa Cruz", "finca-la-esperanza-santa-cruz"),
        ("Estancia Ñandú", "estancia-nandu"),
        ("Fazenda São José", "fazenda-sao-jose"),
        ("Hacienda Cañón del Pingüino", "hacienda-canon-del-pinguino"),
    ],
)
def test_spanish_and_portuguese_names_are_rendered_correctly(services, name, slug):
    result = report_tools.generate_area_bulletin(run_context=_context(name))

    assert result.files[0].name == f"bulletin_{slug}.pdf"
    text = re.sub(r"\s+", " ", "\n".join(_pages(result)))
    assert name in text


def test_file_slug_handles_edge_cases():
    assert report_tools.file_slug("  !!! ") == "area" and report_tools.file_slug("A/B c") == "a-b-c"


def test_every_pdf_label_lives_in_the_yaml():
    results = _load_yaml("tools.yml")["report_tools"]["generate_area_bulletin"]["results"]

    reasons = {"pdf_reason_year", "pdf_reason_no_pasture", "pdf_reason_ee", "pdf_reason_error"}
    assert {key for key in results if key.startswith("pdf_")} == set(report_tools.PDF_LABEL_KEYS) | reasons


def test_description_says_it_takes_no_year():
    description = get_tool_description("report_tools", "generate_area_bulletin")

    assert "takes no year" in description and "PDF" in description


def test_units_and_separators_inside_the_pdf_come_from_the_yaml(services, monkeypatch):
    original = report_tools._labels

    def labels():
        values = original()
        values.update(pdf_fmt_ha="{value} hectares", pdf_fmt_t_ha="{value} tonnes per hectare", pdf_fmt_period="{label} from {start} until {end}, {days} days")
        return values

    monkeypatch.setattr(report_tools, "_labels", labels)

    text = "\n".join(_pages(report_tools.generate_area_bulletin(run_context=_context())))
    text = re.sub(r"\s+", " ", text)

    assert "250.25 hectares" in text and "17.39 tonnes per hectare" in text and "2025 from 2025-01-01 until 2025-12-31, 365 days" in text


def test_partial_note_only_appears_for_partial_time2graze_data(services):
    _, _, stats = services
    stats["biomass"] = BIOMASS.model_copy(update={"source": BiomassSource.TIME2GRAZE, "year": 2026, "period_label": "2026-07", "days_covered": 31, "n_images": 7})

    text = "\n".join(_pages(report_tools.generate_area_bulletin(run_context=_context())))

    assert "short-period and indicative" not in text and "Time2Graze" in text
