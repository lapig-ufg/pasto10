"""Tests of the video tool (domain/tools/biomass_tools.py: generate_biomass_video). The service is replaced by fakes."""

import copy

import pytest
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_tool_description

from domain.schemas.feature import FeatureMetadata, RegisteredFeatures
from domain.services.geospatial.biomass import BiomassPeriodUnavailable, NoPastureBiomass
from domain.services.geospatial.biomass_video import RenderedVideo, VideoDeadlineExceeded, VideoEncoderUnavailable, VideoEncodingFailed
from domain.services.geospatial.gee import EarthEngineUnavailable
from domain.services.geospatial.geometry import build_buffered_area
from domain.tools import biomass_tools
from domain.utils.feature_utils import set_registered_features


def _video(mode="monthly", frames=("2026-07", "2026-08*")):
    return RenderedVideo(
        content=b"mp4bytes", format="mp4", mime_type="video/mp4", duration=2.67, width=640, height=640, fps=10.0,
        mode=mode, frames=list(frames), vmin=0.4, vmax=3.5, reason="time2graze" if mode == "monthly" else "fewer_than_two_months",
    )


def _context(name="North Paddock") -> StateContext:
    feature = build_buffered_area(-2.0, 37.0, 500)
    if name:
        feature.metadata.append(FeatureMetadata(key="name", value=name))
    state = {}
    set_registered_features(state, RegisteredFeatures(features=[feature]))
    return StateContext(session_state=state)


def test_monthly_success_declares_the_mode_and_the_scale(monkeypatch):
    calls = []
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: calls.append(years) or _video())
    run_context = _context()
    before = copy.deepcopy(run_context.session_state)

    result = biomass_tools.generate_biomass_video(run_context=run_context, years=5)

    video = result.videos[0]
    assert (video.format, video.mime_type, video.width, video.height, video.fps, video.duration) == ("mp4", "video/mp4", 640, 640, 10.0, 2.67)
    assert video.content == b"mp4bytes" and calls == [5]
    for expected in ("MONTHLY", "2026-07, 2026-08*", "0.4 to 3.5 t DM/ha", "monthly equivalent", "same scale", "Never compare"):
        assert expected in result.content
    assert run_context.session_state == before


def test_annual_fallback_says_it_is_not_monthly_and_why(monkeypatch):
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: _video("annual", ("2016", "2017")))

    result = biomass_tools.generate_biomass_video(run_context=_context())

    for expected in ("ANNUAL", "NOT monthly", "fewer than two usable recent Time2Graze months", "2016, 2017", "Never compare"):
        assert expected in result.content


@pytest.mark.parametrize(
    "error, expected",
    [
        (NoPastureBiomass(), "no productive pasture"),
        (BiomassPeriodUnavailable("year", "2000", "2025"), "not enough frames"),
        (VideoEncoderUnavailable("ffmpeg"), "encoder is not available"),
        (EarthEngineUnavailable("no creds"), "temporarily unavailable"),
        (RuntimeError("boom secret"), "unexpected error"),
    ],
)
def test_failures_use_yaml_texts_and_never_carry_a_video(monkeypatch, error, expected):
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: (_ for _ in ()).throw(error))

    result = biomass_tools.generate_biomass_video(run_context=_context())

    assert expected in result.content and "secret" not in result.content and not result.videos


def test_no_registered_area_and_unknown_name(monkeypatch):
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: pytest.fail("rendered"))

    empty = biomass_tools.generate_biomass_video(run_context=StateContext(session_state={}))
    unknown = biomass_tools.generate_biomass_video(run_context=_context(), area_name="Lake")

    assert "no registered area" in empty.content and "Lake" in unknown.content and not empty.videos and not unknown.videos


def test_description_explains_the_mode_rule():
    description = get_tool_description("biomass_tools", "generate_biomass_video")

    for expected in ("Never pick the mode yourself", "NOT monthly", "2 to 12", "fixed color scale"):
        assert expected in description


@pytest.mark.parametrize(
    "error, expected",
    [(VideoDeadlineExceeded(), "took too long"), (VideoEncodingFailed("ffmpeg returned an error stderr-secret"), "could not be encoded")],
)
def test_deadline_and_encoding_failures_have_their_own_texts(monkeypatch, error, expected):
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: (_ for _ in ()).throw(error))

    result = biomass_tools.generate_biomass_video(run_context=_context())

    assert expected in result.content and "secret" not in result.content and "static biomass map" in result.content and not result.videos


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("fewer_than_two_months", "fewer than two usable recent Time2Graze months"),
        ("months_without_pasture", "no productive pasture or too little data"),
    ],
)
def test_the_fallback_reason_reaches_the_text(monkeypatch, reason, expected):
    video = _video("annual", ("2016", "2017"))
    monkeypatch.setattr(biomass_tools, "render_video", lambda feature, years: RenderedVideo(**{**video.__dict__, "reason": reason}))

    result = biomass_tools.generate_biomass_video(run_context=_context())

    assert expected in result.content and "NOT monthly" in result.content
