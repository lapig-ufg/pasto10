"""End-to-end tests of the biomass video over the 15 fixed global properties (needs ``GEE_*`` and ffmpeg).

Generates the video of each registered property and checks: an MP4 of H.264
yuv420p with even dimensions, at least two frames, under 2 MB; the mode
declared in the text (monthly where recent Time2Graze months exist over the
area, annual otherwise); the negative controls get no video. Prints the time
and a summary table. Set ``PASTO10_E2E_OUT`` to keep the videos and a
contact sheet of their frames. Run with
``uv run pytest -m e2e -s tests/e2e/test_biomass_video_e2e.py``.
"""

import json
import os
import re
import shutil
import subprocess
import time

import pytest
from semente.backends.toolkit import StateContext

from domain.services.geospatial import biomass
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import biomass_tools, property_tools
from tests.fixtures.global_properties import GlobalProperty, load_global_properties

pytestmark = pytest.mark.e2e

PROPERTIES = load_global_properties()
PROPERTY_IDS = [item.id for item in PROPERTIES]
MAX_BYTES = 2 * 1024 * 1024
_summary: list[dict] = []


@pytest.fixture(scope="module", autouse=True)
def require_environment():
    if any(not os.getenv(name) for name in ("GEE_PROJECT", "GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE")):
        pytest.skip("missing GEE_* environment variables")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")


@pytest.fixture(scope="module", autouse=True)
def print_summary():
    yield
    if _summary:
        print("\n\n" + "id".ljust(15) + "kind".ljust(15) + "mode".ljust(9) + "frames".rjust(7) + "KB".rjust(7) + "secs".rjust(7) + "  scale / labels")
        for row in _summary:
            print(row["id"].ljust(15) + row["kind"].ljust(15) + row["mode"].ljust(9) + row["frames"].rjust(7) + row["kb"].rjust(7)
                  + row["secs"].rjust(7) + "  " + row["detail"])


def _register(item: GlobalProperty) -> StateContext:
    context = StateContext(session_state={})
    property_tools.start_buffer_registration_by_coordinate(
        run_context=context, latitude=item.latitude, longitude=item.longitude, radius=item.radius_m,
    )
    property_tools.confirm_registration(run_context=context)
    property_tools.complete_registration(run_context=context, name=item.name)
    return context


def _probe(path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt,width,height,nb_read_frames", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, check=True, text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("item", PROPERTIES, ids=PROPERTY_IDS)
def test_biomass_video_over_global_properties(item, tmp_path):
    context = _register(item)
    registered = dict(context.session_state)
    feature = build_buffered_area(item.latitude, item.longitude, item.radius_m)
    expected_mode = "monthly" if len(biomass.monthly_candidates(biomass.pasture.feature_geometry(feature))) >= 2 else "annual"
    start = time.perf_counter()

    result = biomass_tools.generate_biomass_video(run_context=context)

    seconds = time.perf_counter() - start
    row = {"id": item.id, "kind": item.kind, "mode": "none", "frames": "-", "kb": "-", "secs": f"{seconds:.1f}", "detail": ""}
    _summary.append(row)
    assert context.session_state == registered
    if item.is_control:
        assert not result.videos and "no productive pasture" in result.content
        return

    assert result.videos, result.content
    video = result.videos[0]
    assert video.format == "mp4" and video.mime_type == "video/mp4" and len(video.content) < MAX_BYTES
    path = tmp_path / "video.mp4"
    path.write_bytes(video.content)
    info = _probe(str(path))
    stream = info["streams"][0]
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert stream["width"] % 2 == 0 and stream["height"] % 2 == 0 and (stream["width"], stream["height"]) == (640, 640)
    assert int(stream["nb_read_frames"]) >= 2 and float(info["format"]["duration"]) == pytest.approx(video.duration, abs=0.5)
    mode = "annual" if "NOT monthly" in result.content else "monthly"
    labels = result.content.split("(")[1].split(")")[0].split(", ")
    pattern = r"\d{4}-\d{2}\*?" if mode == "monthly" else r"\d{4}"
    assert all(re.fullmatch(pattern, label) for label in labels) and len(labels) >= 2
    assert ("MONTHLY" in result.content) is (mode == "monthly")
    if expected_mode == "annual":
        assert mode == "annual"  # no monthly candidates over the area: the annual fallback is the only possible mode
    assert "same scale" in result.content and seconds < 90
    row.update(mode=mode, frames=str(int(stream["nb_read_frames"])), kb=f"{len(video.content) // 1024}",
               detail=result.content.split("fixed color scale ")[1].split(",")[0] + " | " + result.content.split("(")[1].split(")")[0])
    out = os.getenv("PASTO10_E2E_OUT")
    if out:
        shutil.copy(path, os.path.join(out, f"video_{item.id}.mp4"))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "fps=1.5,scale=320:-1,tile=4x3",
                        "-frames:v", "1", os.path.join(out, f"video_{item.id}_sheet.png")], check=False)


def test_repeated_request_is_served_from_the_cache():
    item = next(item for item in PROPERTIES if item.id == "goias")
    context = _register(item)

    first_start = time.perf_counter()
    first = biomass_tools.generate_biomass_video(run_context=context)
    first_seconds = time.perf_counter() - first_start
    second_start = time.perf_counter()
    second = biomass_tools.generate_biomass_video(run_context=context)
    second_seconds = time.perf_counter() - second_start

    print(f"\nvideo first call {first_seconds:.1f}s, repeated call {second_seconds:.2f}s")
    assert first.videos and second.videos and second.videos[0].content == first.videos[0].content
    assert second_seconds < 3
