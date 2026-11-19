"""End-to-end tests of the PDF bulletin over the 15 fixed global properties (needs ``GEE_*``).

Registers each property, generates the bulletin and checks: ``%PDF``, under
3 MB, 1-3 pages, text with hectares and the period, no country-specific text;
negative controls give a partial bulletin or none, never a crash. Prints the
time and a summary table. Set ``PASTO10_E2E_OUT`` to keep the PDFs.
Run with ``uv run pytest -m e2e -s tests/e2e/test_bulletin_e2e.py``.
"""

import os
import re
import time
from io import BytesIO

import pytest
from pypdf import PdfReader
from semente.backends.toolkit import StateContext

from domain.schemas.biomass import BiomassSource
from domain.services.geospatial import biomass
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import property_tools, report_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties

pytestmark = pytest.mark.e2e

PROPERTIES = load_global_properties()
PROPERTY_IDS = [item.id for item in PROPERTIES]
MAX_BYTES = 3 * 1024 * 1024
FORBIDDEN = re.compile(r"Brasil|Brazil|SICAR|MapBiomas|Embrapa|Lapig|UFG", re.IGNORECASE)
_summary: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def require_gee():
    if any(not os.getenv(name) for name in ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")):
        pytest.skip("missing GEE_* environment variables")


@pytest.fixture(scope="module", autouse=True)
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(15) + "kind".ljust(15) + "result".ljust(10) + "pages".rjust(6) + "KB".rjust(7) + "secs".rjust(7))
        for row in _summary:
            print(row["id"].ljust(15) + row["kind"].ljust(15) + row["result"].ljust(10) + row["pages"].rjust(6) + row["kb"].rjust(7) + row["secs"].rjust(7))


def _register(item: GlobalProperty) -> StateContext:
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=item.latitude, longitude=item.longitude, radius=item.radius_m,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=item.name)
    return context


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_bulletin_over_global_properties(item):
    context = _register(item)
    registered = dict(context.session_state)
    start = time.perf_counter()

    result = report_tools.generate_area_bulletin(run_context=context)

    seconds = time.perf_counter() - start
    row = {"id": item.id, "kind": item.kind, "result": "no_pdf", "pages": "-", "kb": "-", "secs": f"{seconds:.1f}"}
    _summary.append(row)
    assert context.session_state == registered
    if not result.files:
        assert item.is_control and "no bulletin was generated" in result.content
        return

    document = result.files[0].content
    assert document.startswith(b"%PDF") and len(document) < MAX_BYTES
    reader = PdfReader(BytesIO(document))
    text = re.sub(r"\s+", " ", "\n".join(page.extract_text() for page in reader.pages))
    assert 1 <= len(reader.pages) <= 3
    assert not FORBIDDEN.search(text)
    assert re.search(r"\d+(\.\d+)? ha", text) and "Dry-matter production" in text
    partial = "NOT available" in result.content
    if item.is_control:
        assert partial and "Not available" in text
    else:
        latest = biomass.get_biomass_total(build_buffered_area(item.latitude, item.longitude, item.radius_m))
        assert not partial and re.search(rf"{latest.period_label} \({latest.start} to {latest.end}", text)
        assert latest.source.value in text and ("short-period and indicative" in text) == (latest.source is BiomassSource.TIME2GRAZE)
        assert item.name in text or item.name.encode("latin-1", "replace").decode("latin-1") in text
    row.update(result="partial" if partial else "full", pages=str(len(reader.pages)), kb=f"{len(document) // 1024}")
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        with open(os.path.join(out, f"bulletin_{item.id}.pdf"), "wb") as handle:
            handle.write(document)
