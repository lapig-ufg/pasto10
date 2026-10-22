"""Tests of the biomass video (series rule in biomass.py and biomass_video.py).

No Earth Engine and no network: the ``ee`` functions are replaced by fakes; ffmpeg
is only used (with fake PNG frames) when it is installed.
"""

import json
import shutil
import subprocess
import threading
import time
from datetime import date

import pytest
from PIL import Image

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.services.geospatial import biomass, biomass_image, biomass_video
from domain.services.geospatial.geometry import build_buffered_area

T2G_RANGE = (date(2026, 6, 27), date(2026, 8, 21))


class FakeImage:
    def __getattr__(self, name):
        return lambda *args, **kwargs: self


def _feature():
    return build_buffered_area(-2.0, 37.0, 500)


def _stats(p02=1.0, p98=2.0, mean=1.5, label="2026-07"):
    return BiomassStats(
        source=BiomassSource.TIME2GRAZE, period_label=label, start="2026-07-01", end="2026-07-31", mean_t_ha=mean,
        total_t=100.0, p02_t_ha=p02, p98_t_ha=p98, pasture_ha=60.0, area_ha=78.5, scale_m=10, mask_year=2022, year=2026,
        days_covered=31,
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for cache in (biomass._stats_cache, biomass._range_cache, biomass_video._video_cache):
        cache.clear()
    yield
    for cache in (biomass._stats_cache, biomass._range_cache, biomass_video._video_cache):
        cache.clear()


@pytest.fixture
def series_ee(monkeypatch):
    state = {"range": T2G_RANGE, "images": 5, "days": []}
    monkeypatch.setattr(biomass, "_t2g_range_from_ee", lambda: state["range"])
    monkeypatch.setattr(biomass, "initialize_ee", lambda: None)
    monkeypatch.setattr(biomass.pasture, "feature_geometry", lambda feature: "roi")
    monkeypatch.setattr(biomass, "_t2g_count", lambda roi, start, end: state["images"])
    monkeypatch.setattr(biomass, "_t2g_biomass", lambda roi, period, days=None: state["days"].append(days) or FakeImage())
    monkeypatch.setattr(biomass, "_mask_image", lambda mask_year: FakeImage())
    monkeypatch.setattr(biomass, "_coverage_image", lambda raw, mask_year: FakeImage())
    monkeypatch.setattr(biomass, "_gpp_biomass", lambda year: FakeImage())
    return state


def test_two_usable_months_give_a_monthly_series_with_partial_flagged(series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))

    assert series.mode == "monthly" and series.reason == "time2graze"
    assert [frame.label for frame in series.frames] == ["2026-07", "2026-08*"]
    assert [frame.partial for frame in series.frames] == [False, True]
    assert series_ee["days"] == [31, 31]


def test_months_with_fewer_than_ten_covered_days_are_dropped(series_ee):
    periods = biomass.monthly_candidates("roi", today=date(2026, 10, 22))

    assert [period.label for period in periods] == ["2026-07", "2026-08"]
    assert all(period.days >= biomass.MIN_FRAME_DAYS for period in periods)


def test_a_partial_month_with_at_least_ten_days_is_kept_scaled_and_marked(series_ee):
    series_ee["range"] = (date(2026, 7, 20), date(2026, 8, 21))

    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))

    assert [frame.label for frame in series.frames] == ["2026-07*", "2026-08*"]
    assert all(frame.image.period.partial for frame in series.frames)


def test_today_caps_the_months_and_one_month_falls_back_to_annual(series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 7, 31))

    assert series.mode == "annual" and series.reason == "fewer_than_two_months"
    assert [frame.label for frame in series.frames] == [str(year) for year in range(2016, 2026)]


@pytest.mark.parametrize("years, expected", [(None, 10), (3, 3), (99, 12), (1, 2)])
def test_annual_fallback_frame_count_is_bounded(series_ee, years, expected):
    series_ee["range"] = None

    series = biomass.build_biomass_series(_feature(), years=years)

    assert series.mode == "annual" and len(series.frames) == expected
    assert series.frames[-1].label == "2025" and [f.label for f in series.frames] == sorted(f.label for f in series.frames)
    assert all(frame.image.period.source is BiomassSource.GPW_ANNUAL for frame in series.frames)


def test_no_images_over_the_area_falls_back_to_annual(series_ee):
    series_ee["images"] = 0

    assert biomass.build_biomass_series(_feature()).mode == "annual"


def test_monthly_frames_have_their_own_cache_keys(series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))
    plain = biomass._stats_key(biomass.pasture.geometry_key(_feature()), series.frames[0].image.period)

    assert series.frames[0].image.key != plain and series.frames[0].image.key[-1] == "monthly-equivalent"


def test_plan_frames_fixes_one_scale_from_all_frames(monkeypatch, series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))
    values = iter([_stats(p02=1.0, p98=2.0), _stats(p02=0.4, p98=3.5)])
    lock = threading.Lock()

    def measure(image, roi):
        with lock:
            return next(values)

    monkeypatch.setattr(biomass, "measure_biomass", measure)

    plans, vmin, vmax = biomass_video.plan_frames(series)

    assert (vmin, vmax) == (0.4, 3.5) and [plan.label for plan in plans] == ["2026-07", "2026-08*"]


def test_plan_frames_propagates_a_missing_pasture(monkeypatch, series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))
    monkeypatch.setattr(biomass, "measure_biomass", lambda image, roi: (_ for _ in ()).throw(biomass.NoPastureBiomass()))

    with pytest.raises(biomass.NoPastureBiomass):
        biomass_video.plan_frames(series)


def test_every_frame_uses_the_same_scale_and_its_own_label(monkeypatch):
    ranges, bars, labels = [], [], []
    monkeypatch.setattr(biomass_video, "fetch_overlay", lambda image, stats, bounds, size, value_range: ranges.append(value_range) or Image.new("RGBA", size, (0, 0, 0, 0)))
    monkeypatch.setattr(biomass_video, "draw_colorbar", lambda image, stats, bottom, value_range: bars.append(value_range) or image)
    monkeypatch.setattr(biomass_video, "draw_corner_label", lambda image, text: labels.append(text) or image)
    plans = [biomass_video.FramePlan(label, _stats(mean=mean), FakeImage()) for label, mean in (("2026-07", 1.5), ("2026-08*", 2.5))]

    frames = biomass_video.render_frames(_feature(), plans, 0.4, 3.5, "monthly")

    assert len(frames) == 2 and all(frame.size == (640, 640) for frame in frames)
    assert ranges == [(0.4, 3.5)] * 2 and bars == [(0.4, 3.5)] * 2
    assert labels[0].startswith("2026-07 ") and labels[1].startswith("2026-08* ") and "1.5 t/ha" in labels[0]


def test_default_color_stretch_of_the_static_map_is_unchanged():
    stats = _stats(p02=12.1, p98=23.2)

    assert biomass_image.stretch_of(stats) == (12.1, 23.2)
    assert biomass_image.stretch_of(stats, (0.0, 30.0)) == (0.0, 30.0)
    assert biomass_image.stretch_of(_stats(p02=5.0, p98=5.0)) == (5.0, 5.1)
    image = Image.new("RGB", (640, 640), (10, 10, 10))
    default = biomass_image.draw_colorbar(image, stats, 600)
    explicit = biomass_image.draw_colorbar(image, stats, 600, (12.1, 23.2))
    assert default.tobytes() == explicit.tobytes()


def test_missing_ffmpeg_is_reported(monkeypatch):
    monkeypatch.delenv(biomass_video.FFMPEG_ENV, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(biomass_video.VideoEncoderUnavailable):
        biomass_video.encode_mp4([Image.new("RGB", (64, 64))])


def test_ffmpeg_path_environment_variable_is_honored(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("")
    monkeypatch.setenv(biomass_video.FFMPEG_ENV, str(fake))

    assert biomass_video._ffmpeg() == str(fake)


def test_encoding_without_frames_is_rejected():
    with pytest.raises(ValueError):
        biomass_video.encode_mp4([])


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed")
def test_real_mp4_is_h264_yuv420p_with_even_dimensions_and_the_expected_duration(tmp_path):
    frames = [Image.new("RGB", (641, 639), (index * 40, 100, 60)) for index in range(4)]

    content, duration, width, height = biomass_video.encode_mp4(frames)

    path = tmp_path / "video.mp4"
    path.write_bytes(content)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, check=True, text=True,
    ).stdout)
    stream = info["streams"][0]
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert stream["width"] % 2 == 0 and stream["height"] % 2 == 0 and (stream["width"], stream["height"]) == (width, height) == (640, 638)
    expected = (4 + round(biomass_video.HOLD_SECONDS * biomass_video.FRAME_RATE)) / biomass_video.FRAME_RATE
    assert duration == pytest.approx(expected) and float(info["format"]["duration"]) == pytest.approx(expected, abs=0.4)
    assert content[4:8] == b"ftyp"


def test_generation_is_cached_per_process(monkeypatch, series_ee):
    series_ee["range"] = None
    calls = {"plan": 0, "encode": 0}

    def plan(series, workers=4):
        calls["plan"] += 1
        return [biomass_video.FramePlan(frame.label, _stats(), FakeImage()) for frame in series.frames], 1.0, 2.0

    def encode(frames, timeout=None):
        calls["encode"] += 1
        return b"mp4", 3.0, 640, 640

    monkeypatch.setattr(biomass_video, "plan_frames", plan)
    monkeypatch.setattr(biomass_video, "render_frames", lambda feature, plans, vmin, vmax, mode, deadline=None: [Image.new("RGB", (8, 8))] * len(plans))
    monkeypatch.setattr(biomass_video, "encode_mp4", encode)

    first = biomass_video.generate_biomass_video(_feature())
    second = biomass_video.generate_biomass_video(_feature())

    assert first is second and calls == {"plan": 1, "encode": 1}
    assert first.mode == "annual" and first.frames[0] == "2016" and (first.vmin, first.vmax) == (1.0, 2.0)


def test_deadline_is_enforced(monkeypatch, series_ee):
    monkeypatch.setattr(biomass_video, "DEADLINE_SECONDS", -1.0)
    monkeypatch.setattr(biomass_video, "plan_frames", lambda series, workers=4: ([], 0.0, 1.0))

    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass_video.generate_biomass_video(_feature())


def test_months_are_searched_backwards_and_stop_at_the_maximum(series_ee, monkeypatch):
    series_ee["range"] = (date(2024, 1, 1), date(2026, 8, 21))
    counted = []
    monkeypatch.setattr(biomass, "_t2g_count", lambda roi, start, end: counted.append(start) or 4)

    periods = biomass.monthly_candidates("roi", today=date(2026, 8, 21))

    assert len(periods) == biomass.MAX_MONTHLY_FRAMES == len(counted)
    assert counted[0] == date(2026, 8, 1) and [p.label for p in periods] == sorted(p.label for p in periods)
    assert periods[-1].label == "2026-08" and periods[0].label == "2024-09"


def test_image_counts_are_reused_by_the_frames(series_ee, monkeypatch):
    counted = []
    monkeypatch.setattr(biomass, "_t2g_count", lambda roi, start, end: counted.append(start) or 7)

    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))

    assert len(counted) == len(series.frames) == 2
    assert [frame.image.n_images for frame in series.frames] == [7, 7]


def test_the_deadline_check_runs_while_the_series_is_built(series_ee):
    def abort():
        raise biomass_video.VideoDeadlineExceeded()

    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass.build_biomass_series(_feature(), today=date(2026, 10, 22), check=abort)
    series_ee["range"] = None
    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass.build_biomass_series(_feature(), check=abort)


def test_repeating_a_request_hits_the_video_cache_before_rebuilding_the_series(series_ee, monkeypatch):
    series_ee["range"] = None
    built = []
    real = biomass.build_biomass_series
    monkeypatch.setattr(biomass, "build_biomass_series", lambda *args, **kwargs: built.append(1) or real(*args, **kwargs))
    monkeypatch.setattr(biomass_video, "plan_frames", lambda series, workers=4: ([biomass_video.FramePlan(f.label, _stats(), FakeImage()) for f in series.frames], 1.0, 2.0))
    monkeypatch.setattr(biomass_video, "render_frames", lambda *args, **kwargs: [Image.new("RGB", (8, 8))])
    monkeypatch.setattr(biomass_video, "encode_mp4", lambda frames, timeout=None: (b"mp4", 3.0, 8, 8))

    biomass_video.generate_biomass_video(_feature())
    start = time.perf_counter()
    biomass_video.generate_biomass_video(_feature())

    assert len(built) == 1 and time.perf_counter() - start < 0.2
    biomass_video.generate_biomass_video(_feature(), years=4)
    assert len(built) == 2


def test_a_frame_without_pasture_is_skipped_unless_fewer_than_two_remain(monkeypatch, series_ee):
    series_ee["range"] = None
    series = biomass.build_biomass_series(_feature(), years=3)

    def measure_factory(skip):
        def measure(image, roi):
            if image is series.frames[skip].image if isinstance(skip, int) else image in [series.frames[i].image for i in skip]:
                raise biomass.NoPastureBiomass()
            return _stats()
        return measure

    monkeypatch.setattr(biomass, "measure_biomass", measure_factory(0))
    plans, _, _ = biomass_video.plan_frames(series)
    assert [plan.label for plan in plans] == ["2024", "2025"]

    monkeypatch.setattr(biomass, "measure_biomass", measure_factory([0, 1]))
    with pytest.raises(biomass.NoPastureBiomass):
        biomass_video.plan_frames(series)


def test_vmin_is_rounded_before_vmax_is_derived(monkeypatch, series_ee):
    series_ee["range"] = None
    series = biomass.build_biomass_series(_feature(), years=2)
    monkeypatch.setattr(biomass, "measure_biomass", lambda image, roi: _stats(p02=0.123, p98=0.125))

    _, vmin, vmax = biomass_video.plan_frames(series)

    assert (vmin, vmax) == (0.12, 0.22)


def test_ffmpeg_failures_are_distinct_from_a_missing_binary(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(biomass_video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(biomass_video, "log_warning", logged.append)

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b"x" * 2000)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(biomass_video.VideoEncodingFailed):
        biomass_video.encode_mp4([Image.new("RGB", (8, 8))])
    assert len(logged) == 1 and len(logged[0]) < 600
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1)))
    with pytest.raises(biomass_video.VideoEncodingFailed):
        biomass_video.encode_mp4([Image.new("RGB", (8, 8))])
    assert not issubclass(biomass_video.VideoEncodingFailed, biomass_video.VideoEncoderUnavailable)


def test_ffmpeg_path_must_be_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv(biomass_video.FFMPEG_ENV, str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(biomass_video.VideoEncoderUnavailable):
        biomass_video._ffmpeg()


def test_ffmpeg_timeout_respects_the_remaining_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(biomass_video, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: seen.append(kwargs["timeout"]) or (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1)))

    with pytest.raises(biomass_video.VideoEncodingFailed):
        biomass_video.encode_mp4([Image.new("RGB", (8, 8))], timeout=7.5)
    assert seen == [7.5]


def test_monthly_frames_without_pasture_fall_back_to_the_annual_series(monkeypatch, series_ee):
    monthly_calls = []

    def measure(image, roi):
        if image.period.source is BiomassSource.TIME2GRAZE:
            monthly_calls.append(1)
            raise biomass.NoPastureBiomass()
        return _stats()

    monkeypatch.setattr(biomass, "measure_biomass", measure)
    monkeypatch.setattr(biomass_video, "render_frames", lambda feature, plans, vmin, vmax, mode, deadline=None: [Image.new("RGB", (8, 8))] * len(plans))
    monkeypatch.setattr(biomass_video, "encode_mp4", lambda frames, timeout=None: (b"mp4", 3.0, 8, 8))

    video = biomass_video.generate_biomass_video(_feature())

    assert len(monthly_calls) == 2 and video.mode == "annual" and video.reason == "months_without_pasture"


def test_annual_frames_without_pasture_still_fail(monkeypatch, series_ee):
    series_ee["range"] = None
    monkeypatch.setattr(biomass, "measure_biomass", lambda image, roi: (_ for _ in ()).throw(biomass.NoPastureBiomass()))

    with pytest.raises(biomass.NoPastureBiomass):
        biomass_video.generate_biomass_video(_feature())


def test_frames_with_too_little_time2graze_coverage_are_skipped(monkeypatch, series_ee):
    series = biomass.build_biomass_series(_feature(), today=date(2026, 10, 22))
    coverages = {series.frames[0].image.key: 0.19, series.frames[1].image.key: 0.9}
    monkeypatch.setattr(biomass, "measure_biomass", lambda image, roi: _stats().model_copy(update={"data_coverage": coverages[image.key]}))

    with pytest.raises(biomass.NoPastureBiomass):
        biomass_video.plan_frames(series)
    coverages[series.frames[0].image.key] = 0.3
    plans, _, _ = biomass_video.plan_frames(series)
    assert len(plans) == 2 and biomass.MIN_FRAME_COVERAGE == 0.25


def _stub_pipeline(monkeypatch, sleep=0.0, order=None):
    def plan(series, workers=4):
        time.sleep(sleep)
        return [biomass_video.FramePlan(f.label, _stats(), FakeImage()) for f in series.frames], 1.0, 2.0

    monkeypatch.setattr(biomass_video, "plan_frames", plan)
    monkeypatch.setattr(biomass_video, "render_frames", lambda *args, **kwargs: [Image.new("RGB", (8, 8))])
    monkeypatch.setattr(biomass_video, "encode_mp4", lambda frames, timeout=None: (b"mp4", 3.0, 8, 8))


def test_deadline_is_checked_after_the_measurement(monkeypatch, series_ee):
    series_ee["range"] = None
    _stub_pipeline(monkeypatch, sleep=0.15)
    monkeypatch.setattr(biomass_video, "DEADLINE_SECONDS", 0.05)

    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass_video.generate_biomass_video(_feature())


def test_deadline_is_checked_before_the_fallback_series_is_rebuilt(monkeypatch, series_ee):
    built = []
    real = biomass.build_biomass_series
    monkeypatch.setattr(biomass, "build_biomass_series", lambda *args, **kwargs: built.append(kwargs.get("force_annual")) or real(*args, **kwargs))

    def plan(series, workers=4):
        time.sleep(0.15)
        raise biomass.NoPastureBiomass()

    monkeypatch.setattr(biomass_video, "plan_frames", plan)
    monkeypatch.setattr(biomass_video, "DEADLINE_SECONDS", 0.05)

    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass_video.generate_biomass_video(_feature())
    assert built == [False] or built == [None]


def test_an_exhausted_budget_never_runs_ffmpeg(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("ffmpeg ran"))
    monkeypatch.setattr(biomass_video, "_ffmpeg", lambda: "ffmpeg")

    with pytest.raises(biomass_video.VideoDeadlineExceeded):
        biomass_video.encode_mp4([Image.new("RGB", (8, 8))], timeout=0)


def test_cache_key_normalizes_years_for_the_annual_mode(monkeypatch, series_ee):
    series_ee["range"] = None
    built = []
    real = biomass.build_biomass_series
    monkeypatch.setattr(biomass, "build_biomass_series", lambda *args, **kwargs: built.append(1) or real(*args, **kwargs))
    _stub_pipeline(monkeypatch)

    biomass_video.generate_biomass_video(_feature(), years=1)
    biomass_video.generate_biomass_video(_feature(), years=2)
    biomass_video.generate_biomass_video(_feature(), years=99)
    biomass_video.generate_biomass_video(_feature(), years=12)

    assert len(built) == 2


def test_a_monthly_video_is_cached_without_the_years_argument(monkeypatch, series_ee):
    built = []
    real = biomass.build_biomass_series
    monkeypatch.setattr(biomass, "build_biomass_series", lambda *args, **kwargs: built.append(1) or real(*args, **kwargs))
    _stub_pipeline(monkeypatch)
    series_ee["range"] = (date(2026, 6, 27), date.today())

    first = biomass_video.generate_biomass_video(_feature(), years=3)
    second = biomass_video.generate_biomass_video(_feature(), years=8)

    assert first.mode == "monthly" and first is second and len(built) == 1


def test_ffmpeg_failure_cleans_the_temporary_directory(monkeypatch):
    import tempfile
    from pathlib import Path

    created = []
    real = tempfile.TemporaryDirectory

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(Path(self.name))

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Recording)
    monkeypatch.setattr(biomass_video, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, command, stderr=b"bad")))

    with pytest.raises(biomass_video.VideoEncodingFailed):
        biomass_video.encode_mp4([Image.new("RGB", (8, 8))])

    assert len(created) == 1 and not created[0].exists()
