"""Biomass video service — animated time series of the dry-matter production of an area.

Frames come from ``biomass.build_biomass_series`` (monthly Time2Graze
equivalents, or the annual Global Pasture Watch fallback). Every frame is
measured with the cached ``measure_biomass`` and drawn over the same basemap
with ONE fixed color scale (the minimum of the 2nd percentiles to the maximum
of the 98th percentiles of all frames), a global colorbar and the label of its
reference period. The frames are encoded to an H.264 MP4 (yuv420p, even
dimensions) with ffmpeg, holding the last frame for about two seconds.

ffmpeg is looked up in ``PATH`` or in the ``FFMPEG_PATH`` environment variable;
without it ``VideoEncoderUnavailable`` is raised. The finished videos are
cached per process.
"""

import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

import ee  # noqa: F401  (type annotations)
from PIL import Image

from semente.logging import log_warning

from domain.schemas.biomass import BiomassStats
from domain.schemas.feature import Feature
from domain.services.geospatial import biomass, pasture
from domain.services.geospatial.basemap import (
    ESRI_WORLD_IMAGERY,
    TileProvider,
    build_basemap,
    choose_zoom,
    latlon_to_pixel,
    meters_per_pixel,
    resolve_providers,
)
from domain.services.geospatial.biomass_image import draw_colorbar, fetch_overlay
from domain.services.geospatial.image import (
    IMAGE_SIZE,
    NEUTRAL_BACKGROUND,
    draw_attribution,
    draw_center_marker,
    draw_corner_label,
    draw_scale_bar,
    parse_center,
    project_ring,
    scale_bar_box,
)
from domain.services.geospatial.pasture_image import blend_overlay, draw_outline, mercator_bounds
from domain.utils.cache_utils import SingleFlightCache

MAX_FRAMES = 24
FRAME_RATE = 1.5
HOLD_SECONDS = 2.0
OUTPUT_FPS = 10
MAX_WORKERS = 4
DEADLINE_SECONDS = 90.0
FFMPEG_ENV = "FFMPEG_PATH"
ENCODER_TIMEOUT_SECONDS = 60
_ATTRIBUTION = {
    "monthly": "Dry matter: Time2Graze + Global Pasture Watch mask (estimate)",
    "annual": "Dry matter: Global Pasture Watch annual (estimate)",
}
_video_cache = SingleFlightCache(4)


class VideoEncoderUnavailable(Exception):
    """ffmpeg is not installed (or not found through ``PATH`` / ``FFMPEG_PATH``).

    Raised by ``encode_mp4`` (through ``_ffmpeg``) before any encoding starts.
    ``biomass_tools.generate_biomass_video`` catches it and answers with the
    ``encoder_unavailable`` text.
    """


class VideoEncodingFailed(Exception):
    """ffmpeg is installed but the encoding failed, could not run or timed out.

    Raised by ``encode_mp4`` (the truncated ffmpeg stderr is logged, never put
    in the message). ``biomass_tools.generate_biomass_video`` catches it and
    answers with the ``encoding_failed`` text.
    """


class VideoDeadlineExceeded(Exception):
    """Producing the video took longer than ``DEADLINE_SECONDS``.

    Raised between the steps of the generation (series, measurement, overlays,
    encoding) when the shared time budget is spent. ``biomass_tools.generate_biomass_video``
    catches it and answers with the ``timeout`` text.
    """


@dataclass(frozen=True)
class RenderedVideo:
    """An encoded biomass video.

    Attributes:
        content (bytes): MP4 bytes.
        format (str): ``"mp4"``.
        mime_type (str): ``"video/mp4"``.
        duration (float): Duration in seconds (frames plus the hold of the last one).
        width (int): Width in pixels (even).
        height (int): Height in pixels (even).
        fps (float): Frames per second of the file.
        mode (str): ``"monthly"`` or ``"annual"``.
        frames (list[str]): Frame labels in order (``*`` marks a partly covered month).
        vmin (float): Lower end of the fixed color scale (t DM/ha).
        vmax (float): Upper end of the fixed color scale (t DM/ha).
        reason (str): Why the mode was chosen: ``"time2graze"`` (monthly), ``"fewer_than_two_months"``
            (annual, fewer than two usable months) or ``"months_without_pasture"`` (annual, the monthly
            frames had no productive pasture or too little data).
    """

    content: bytes
    format: str
    mime_type: str
    duration: float
    width: int
    height: int
    fps: float
    mode: str
    frames: list[str]
    vmin: float
    vmax: float
    reason: str = ""


@dataclass(frozen=True)
class FramePlan:
    """A measured frame ready to be drawn.

    Attributes:
        label (str): Reference period shown on the frame (``"2026-07"``, ``"2026-08*"``, ``"2021"``).
        stats (BiomassStats): Measured figures of the frame (cached), including its percentiles.
        image (ee.Image): Masked biomass image (t DM/ha) of the frame.
    """

    label: str
    stats: BiomassStats
    image: "ee.Image"


def plan_frames(series: "biomass.BiomassSeries", workers: int = MAX_WORKERS) -> tuple[list[FramePlan], float, float]:
    """Measures every frame (cached, in parallel) and fixes the shared color scale.

    Args:
        series (BiomassSeries): The frames to measure (at most ``MAX_FRAMES`` are used, the latest).
        workers (int): Maximum parallel measurements.

    Returns:
        tuple: (frame plans in order, vmin, vmax) with ``vmin`` the minimum of the
        2nd percentiles and ``vmax`` the maximum of the 98th percentiles of the
        frames that have pasture (``vmin`` is rounded first, ``vmax`` is at least 0.1 above it).

    Raises:
        NoPastureBiomass: If fewer than two frames remain: frames without productive pasture, or whose
            Time2Graze data cover less than ``MIN_FRAME_COVERAGE`` of the grassland area, are skipped.
        Exception: Any Earth Engine error while measuring.
    """
    frames = series.frames[-MAX_FRAMES:]

    def measure(frame):
        try:
            stats = biomass.measure_biomass(frame.image, series.roi)
        except biomass.NoPastureBiomass:
            return None
        if stats.data_coverage is not None and stats.data_coverage < biomass.MIN_FRAME_COVERAGE:
            return None
        return stats

    with ThreadPoolExecutor(min(workers, MAX_WORKERS)) as executor:
        measured = list(executor.map(measure, frames))
    plans = [FramePlan(frame.label, stats, frame.image.image) for frame, stats in zip(frames, measured) if stats is not None]
    if len(plans) < 2:
        raise biomass.NoPastureBiomass()
    vmin = round(min(plan.stats.p02_t_ha for plan in plans), 2)
    vmax = round(max(max(plan.stats.p98_t_ha for plan in plans), vmin + 0.1), 2)
    return plans, vmin, vmax


def render_frames(
    feature: Feature, plans: Sequence[FramePlan], vmin: float, vmax: float, mode: str,
    size: Tuple[int, int] = IMAGE_SIZE, providers: Optional[Sequence[TileProvider]] = None, deadline: Optional[float] = None,
) -> list[Image.Image]:
    """Draws the frames over one basemap with the fixed color scale (network: overlays in parallel).

    The basemap is built once and the outline projection computed once; each
    frame adds its overlay, the global colorbar, the scale bar, the attribution
    and its period label.

    Args:
        feature (Feature): Registered buffer area (metadata ``center`` and ``radius``).
        plans (Sequence[FramePlan]): Measured frames.
        vmin (float): Lower end of the color scale in t DM/ha.
        vmax (float): Upper end of the color scale in t DM/ha.
        mode (str): ``"monthly"`` or ``"annual"``, for the attribution.
        size (Tuple[int, int]): Frame size in pixels.
        providers (Sequence[TileProvider], optional): Tile providers; defaults to ``resolve_providers()``.
        deadline (float, optional): ``time.monotonic()`` instant after which ``VideoDeadlineExceeded`` is raised.

    Returns:
        list[PIL.Image.Image]: RGB frames in the order of ``plans``.

    Raises:
        ValueError: If the feature lacks valid ``center``/``radius`` metadata.
        VideoDeadlineExceeded: If the deadline passes.
        Exception: Earth Engine or HTTP failure while fetching an overlay.
    """
    latitude, longitude = parse_center(feature)
    radius = float(feature.get_metadata("radius", 0))
    if radius <= 0:
        raise ValueError(f"Feature {feature.id} has no valid 'radius' metadata.")
    if providers is None:
        providers = resolve_providers()
    max_zoom = min((p.max_zoom for p in providers), default=ESRI_WORLD_IMAGERY.max_zoom)
    zoom = choose_zoom(latitude, radius, size, max_zoom=max_zoom)
    background, provider = build_basemap(latitude, longitude, zoom, size, providers=providers)
    if background is None:
        background = Image.new("RGB", size, NEUTRAL_BACKGROUND)
    bounds = mercator_bounds(latlon_to_pixel(latitude, longitude, zoom), zoom, size)
    ring = project_ring(feature.get_coords()[0][0], (latitude, longitude), zoom, size)
    mpp = meters_per_pixel(latitude, zoom)
    bar_bottom = scale_bar_box(size, mpp)[1] - 8
    lines = [_ATTRIBUTION[mode]] + ([provider.attribution] if provider is not None else [])

    def overlay(plan: FramePlan) -> Image.Image:
        if deadline is not None and time.monotonic() > deadline:
            raise VideoDeadlineExceeded()
        return fetch_overlay(plan.image, plan.stats, bounds, size, (vmin, vmax))

    with ThreadPoolExecutor(MAX_WORKERS) as executor:
        overlays = list(executor.map(overlay, plans))

    frames = []
    for plan, layer in zip(plans, overlays):
        image = blend_overlay(background, layer)
        image = draw_outline(image, ring)
        image = draw_center_marker(image, (size[0] / 2, size[1] / 2))
        image = draw_scale_bar(image, mpp)
        image = draw_colorbar(image, plan.stats, bar_bottom, (vmin, vmax))
        image = draw_corner_label(image, f"{plan.label} · Dry matter {plan.stats.mean_t_ha:g} t/ha")
        frames.append(draw_attribution(image, "\n".join(lines)))
    return frames


def _ffmpeg() -> str:
    """Returns the ffmpeg executable (``FFMPEG_PATH`` or ``PATH``).

    Raises:
        VideoEncoderUnavailable: If ffmpeg cannot be found.
    """
    configured = os.getenv(FFMPEG_ENV)
    found = configured if configured and Path(configured).is_file() else shutil.which("ffmpeg")
    if not found:
        raise VideoEncoderUnavailable("ffmpeg not found")
    return found


def encode_mp4(frames: Sequence[Image.Image], timeout: Optional[float] = None) -> tuple[bytes, float, int, int]:
    """Encodes frames into an H.264 yuv420p MP4 with ffmpeg.

    Frames are shown at ``FRAME_RATE`` per second and the last one is held for
    about ``HOLD_SECONDS``; the file runs at ``OUTPUT_FPS``; width and height
    are rounded down to even numbers.

    Args:
        frames (Sequence[PIL.Image.Image]): The frames (same size).
        timeout (float, optional): Seconds ffmpeg may run; defaults to ``ENCODER_TIMEOUT_SECONDS``
            (never more than that).

    Returns:
        tuple: (MP4 bytes, duration in seconds, width, height).

    Raises:
        VideoEncoderUnavailable: If ffmpeg cannot be found.
        VideoEncodingFailed: If ffmpeg fails or times out (its truncated stderr is logged).
        VideoDeadlineExceeded: If ``timeout`` is already spent.
        ValueError: If there are no frames.
    """
    if not frames:
        raise ValueError("no frames to encode")
    if timeout is not None and timeout <= 0:
        raise VideoDeadlineExceeded()
    executable = _ffmpeg()
    hold = max(0, round(HOLD_SECONDS * FRAME_RATE))
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        sequence = list(frames) + [frames[-1]] * hold
        for index, frame in enumerate(sequence):
            frame.convert("RGB").save(folder / f"frame_{index:03d}.png")
        output = folder / "out.mp4"
        command = [
            executable, "-y", "-loglevel", "error", "-framerate", str(FRAME_RATE), "-i", str(folder / "frame_%03d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(OUTPUT_FPS),
            "-movflags", "+faststart", str(output),
        ]
        limit = min(timeout, ENCODER_TIMEOUT_SECONDS) if timeout is not None else ENCODER_TIMEOUT_SECONDS
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=limit)
        except subprocess.CalledProcessError as error:
            log_warning(f"encode_mp4: ffmpeg failed: {(error.stderr or b'').decode(errors='replace')[:500]}")
            raise VideoEncodingFailed("ffmpeg returned an error") from error
        except (subprocess.SubprocessError, OSError) as error:
            raise VideoEncodingFailed(f"ffmpeg could not run: {type(error).__name__}") from error
        width, height = frames[0].size
        return output.read_bytes(), len(sequence) / FRAME_RATE, width - width % 2, height - height % 2


def _check_deadline(deadline: float) -> None:
    """Raises ``VideoDeadlineExceeded`` when the monotonic ``deadline`` has passed."""
    if time.monotonic() > deadline:
        raise VideoDeadlineExceeded()


def generate_biomass_video(feature: Feature, years: Optional[int] = None) -> RenderedVideo:
    """Generates the biomass video of an area (cached per process).

    A finished video is looked up before any series is built: first under the
    monthly key (geometry, Time2Graze date range, current month, method), then
    under the key with the normalized number of annual frames (``years`` limited
    to 2-12). Only the Time2Graze date range itself may need an Earth Engine
    call (cached for a day). A video of monthly mode is stored without
    ``years``, since the requested number of annual frames does not affect it.
    Concurrent requests for one key share the computation, and a failure of the
    owner propagates to the waiters. The whole work shares one
    ``DEADLINE_SECONDS`` budget, checked between steps and bounding ffmpeg.

    Args:
        feature (Feature): Registered area.
        years (int, optional): Number of annual frames when the annual fallback is used.

    Returns:
        RenderedVideo: The MP4 with its mode, frame labels and fixed color scale.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        NoPastureBiomass: If fewer than two frames have productive pasture (when the monthly
            frames have none, for example in a cold month, the annual fallback is tried first).
        VideoEncoderUnavailable: If ffmpeg is missing.
        VideoEncodingFailed: If ffmpeg fails.
        VideoDeadlineExceeded: If the work exceeds ``DEADLINE_SECONDS``.
        Exception: Any Earth Engine or network error.
    """
    deadline = time.monotonic() + DEADLINE_SECONDS
    window = biomass.t2g_date_range()
    base = (pasture.geometry_key(feature), window, time.strftime("%Y-%m", time.gmtime()), biomass.METHOD_VERSION)
    monthly_key = base + ("monthly",)
    annual_key = base + ("annual", min(max(years or biomass.DEFAULT_ANNUAL_FRAMES, 2), biomass.MAX_ANNUAL_FRAMES))
    check = lambda: _check_deadline(deadline)

    def finish(series: "biomass.BiomassSeries") -> RenderedVideo:
        """Measures, draws and encodes a series."""
        plans, vmin, vmax = plan_frames(series)
        check()
        frames = render_frames(feature, plans, vmin, vmax, series.mode, deadline=deadline)
        check()
        content, duration, width, height = encode_mp4(frames, timeout=deadline - time.monotonic())
        return RenderedVideo(
            content=content, format="mp4", mime_type="video/mp4", duration=round(duration, 2), width=width,
            height=height, fps=float(OUTPUT_FPS), mode=series.mode, frames=[plan.label for plan in plans],
            vmin=vmin, vmax=vmax, reason=series.reason,
        )

    def compute() -> RenderedVideo:
        series = biomass.build_biomass_series(feature, years, check=check)
        if series.mode == "monthly":
            try:
                return _video_cache.get_or_compute(monthly_key, lambda: finish(series))
            except biomass.NoPastureBiomass:
                check()
                series = biomass.build_biomass_series(feature, years, check=check, force_annual=True)
        return finish(series)

    cached = _video_cache.peek(monthly_key)
    if cached is not None:
        return cached
    return _video_cache.get_or_compute(annual_key, compute)
