"""Dry-matter productivity (biomass) service (Earth Engine).

Source rule, applied to the whole area and never pixel by pixel:

- a year up to ``GPP_LAST_YEAR`` (or no year and no month): Global Pasture
  Watch annual gross primary productivity (``gc_m2``, gC/m²/year, 30 m),
  masked by the Global Pasture Watch grassland map of ``min(year, MASK_LAST_YEAR)``;
- a month, or a year after ``GPP_LAST_YEAR``: Time2Graze productivity (``ugpp``,
  0.1 gC/m²/day, 10 m) averaged over the images of the period and multiplied
  by its number of days. Its coverage is recent and partial: the available
  date range is discovered dynamically (cached for a day) and a period without
  images is reported as unavailable, never replaced by Global Pasture Watch.

Productivity is converted to dry matter with ``DRY_MATTER_PER_GC_M2``. The
result is the accumulated dry-matter production of the period (t DM/ha), not
standing biomass nor available forage.

The ``ee`` coupling is confined to the small ``_``-prefixed functions below,
so tests can replace them without Earth Engine.
"""

import calendar
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import ee

from semente.logging import log_debug

from domain.schemas.biomass import BiomassSource, BiomassStats
from domain.schemas.feature import Feature
from domain.services.geospatial import pasture
from domain.services.geospatial.gee import initialize_ee
from domain.utils.cache_utils import DiskCache, SingleFlightCache

GPP_COLLECTION = "projects/global-pasture-watch/assets/ggpp-30m/v1-1/ugpp_m"
GPP_BAND = "gc_m2"
GPP_FIRST_YEAR = 2000
GPP_LAST_YEAR = 2025
MASK_COLLECTION = pasture.GPW_COLLECTION
MASK_LAST_YEAR = pasture.GPW_LAST_YEAR
T2G_COLLECTION = "projects/wri-lcl-time2graze/assets/ugpp_prod_10m_v1"
T2G_BAND = "ugpp"

# Dry matter (t DM/ha) per gC/m²: 0.50 (LUEmax) x 2.7 (C to dry matter) x 0.01 (gC/m² to t/ha).
# An approximation calibrated for cultivated tropical pastures; elsewhere it is an order of magnitude.
DRY_MATTER_PER_GC_M2 = 0.0135
T2G_GC_M2_PER_UNIT_DAY = 0.1
GPP_SCALE_METERS = 30
T2G_SCALE_METERS = 10
MIN_PASTURE_FRACTION = 0.01
MAX_PIXELS = 1e9
_HECTARE = 10_000.0

METHOD_VERSION = "bm-2"
_RESULT_CACHE_SIZE = 128
_RANGE_TTL_SECONDS = 86_400
_stats_cache = SingleFlightCache(_RESULT_CACHE_SIZE)
_range_cache = SingleFlightCache(1, ttl=_RANGE_TTL_SECONDS)


class BiomassPeriodUnavailable(Exception):
    """The requested year or month has no productivity data.

    Attributes:
        kind (str): ``"year"`` or ``"month"``.
        first (str): First available period (year or ISO date).
        last (str): Last available period (year or ISO date).
    """

    def __init__(self, kind: str, first: str, last: str):
        """Creates the error.

        Args:
            kind (str): ``"year"`` or ``"month"``.
            first (str): First available year (``"year"``) or Time2Graze date (``"month"``).
            last (str): Last available year or Time2Graze date.
        """
        super().__init__(f"{kind} unavailable; available {first} to {last}")
        self.kind = kind
        self.first = first
        self.last = last


class NoPastureBiomass(Exception):
    """The area has no productive pasture pixels, so no biomass figures exist."""


@dataclass(frozen=True)
class BiomassPeriod:
    """A resolved period and its data source.

    Attributes:
        source (BiomassSource): Dataset serving the period.
        year (int): Year of the period.
        month (int, optional): Month of the period (Time2Graze monthly only).
        start (date): First day covered.
        end (date): Last day covered (inclusive).
        mask_year (int): Year of the grassland mask.
        scale (int): Analysis resolution in meters.
        partial (bool): True when the covered days are fewer than the requested period.
        label (str): Human-readable period (``"2022"``, ``"2026-07"``, ``"2026"``).
    """

    source: BiomassSource
    year: int
    month: Optional[int]
    start: date
    end: date
    mask_year: int
    scale: int
    partial: bool
    label: str

    @property
    def days(self) -> int:
        """Number of days covered."""
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class BiomassImage:
    """A productivity image ready to be measured or drawn.

    Attributes:
        image (ee.Image): Band ``biomass`` (t DM/ha), masked to productive pasture.
        period (BiomassPeriod): The period behind the image.
        n_images (int, optional): Number of Time2Graze images averaged.
        key (tuple): Stats cache key.
        coverage (ee.Image, optional): Boolean bands ``grass`` (grassland mask) and ``covered``
            (grassland with productivity data), to measure the spatial data coverage (Time2Graze only).
    """

    image: "ee.Image"
    period: BiomassPeriod
    n_images: Optional[int]
    key: tuple
    coverage: Optional["ee.Image"] = None


def _to_date(milliseconds: float) -> date:
    """Converts an Earth Engine timestamp (milliseconds since the epoch) to its UTC date."""
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).date()


def _t2g_range_from_ee() -> Optional[tuple[date, date]]:
    """Returns the first and last day covered by the Time2Graze collection, or None (network).

    The last day is the date of the latest ``system:time_end``, inclusive: the
    images of that day are part of the data (verified: 2026-06-27 to
    2026-08-21 is 56 days and 48 images).
    """
    initialize_ee()
    collection = ee.ImageCollection(T2G_COLLECTION)
    start = collection.aggregate_min("system:time_start").getInfo()
    end = collection.aggregate_max("system:time_end").getInfo()
    return (_to_date(start), _to_date(end)) if start is not None and end is not None else None


def t2g_date_range() -> Optional[tuple[date, date]]:
    """Returns the date range of the Time2Graze data, cached for a day.

    Returns:
        tuple[date, date] | None: First and last covered day, or None when the
        collection has no images.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    return _range_cache.get_or_compute("range", lambda: _t2g_range_from_ee() or ()) or None


def _unavailable_range() -> tuple[str, str]:
    """Returns the Time2Graze range as ISO strings for error messages ('none' when empty)."""
    available = t2g_date_range()
    return (available[0].isoformat(), available[1].isoformat()) if available else ("none", "none")


def resolve_biomass_period(year: Optional[int], month: Optional[int]) -> BiomassPeriod:
    """Applies the source rule to the requested year and month.

    Args:
        year (int | None): Requested year.
        month (int | None): Requested month (1-12), served by Time2Graze.

    Returns:
        BiomassPeriod: The period, its source, mask year and covered days.

    Raises:
        BiomassPeriodUnavailable: If the year is before ``GPP_FIRST_YEAR`` (or not
        a valid calendar year), or
        the month/year has no Time2Graze coverage (``kind`` tells which).
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    if year is not None and not 1 <= year <= 9999:
        raise BiomassPeriodUnavailable("year", str(GPP_FIRST_YEAR), str(GPP_LAST_YEAR))
    if month is None and (year is None or GPP_FIRST_YEAR <= year <= GPP_LAST_YEAR):
        chosen = GPP_LAST_YEAR if year is None else year
        return BiomassPeriod(
            BiomassSource.GPW_ANNUAL, chosen, None, date(chosen, 1, 1), date(chosen, 12, 31),
            min(chosen, MASK_LAST_YEAR), GPP_SCALE_METERS, False, str(chosen),
        )
    available = t2g_date_range()
    if month is None:
        latest = max(GPP_LAST_YEAR, available[1].year if available else GPP_LAST_YEAR)
        if year < GPP_FIRST_YEAR or available is None:
            raise BiomassPeriodUnavailable("year", str(GPP_FIRST_YEAR), str(latest))
        requested = (date(year, 1, 1), date(year, 12, 31))
        label = str(year)
        kind = "year"
    else:
        if available is None or not 1 <= month <= 12:
            raise BiomassPeriodUnavailable("month", *_unavailable_range())
        year = year if year is not None else available[1].year
        requested = (date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))
        label = f"{year}-{month:02d}"
        kind = "month"
    start, end = max(requested[0], available[0]), min(requested[1], available[1])
    if start > end:
        first, last = _unavailable_range()
        if kind == "year":
            first, last = str(GPP_FIRST_YEAR), str(max(GPP_LAST_YEAR, available[1].year))
        raise BiomassPeriodUnavailable(kind, first, last)
    return BiomassPeriod(
        BiomassSource.TIME2GRAZE, year, month, start, end, min(year, MASK_LAST_YEAR),
        T2G_SCALE_METERS, (start, end) != requested, label,
    )


def _mask_image(mask_year: int) -> "ee.Image":
    """Returns the Global Pasture Watch grassland mask of a year (1 where grassland, masked pixels as 0)."""
    return (
        ee.ImageCollection(MASK_COLLECTION)
        .filter(ee.Filter.eq("system:index", str(mask_year)))
        .first()
        .select(pasture.CLASS_BAND)
        .unmask(pasture.NOT_GRASSLAND)
        .gte(1)
    )


def _gpp_biomass(year: int) -> "ee.Image":
    """Returns the annual dry-matter production image (t DM/ha) of a year, band ``biomass``.

    Raises:
        BiomassPeriodUnavailable: If the collection has no image for the year (network check).
    """
    collection = ee.ImageCollection(GPP_COLLECTION).filter(ee.Filter.eq("system:index", str(year)))
    if collection.size().getInfo() == 0:
        raise BiomassPeriodUnavailable("year", str(GPP_FIRST_YEAR), str(GPP_LAST_YEAR))
    return collection.first().select(GPP_BAND).multiply(DRY_MATTER_PER_GC_M2).rename("biomass")


def _t2g_collection(roi: "ee.Geometry", start: date, end: date) -> "ee.ImageCollection":
    """Returns the Time2Graze images over ``roi`` acquired between two days (inclusive)."""
    return (
        ee.ImageCollection(T2G_COLLECTION)
        .filterBounds(roi)
        .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat())
    )


def _t2g_count(roi: "ee.Geometry", start: date, end: date) -> int:
    """Returns how many Time2Graze images cover ``roi`` in the period (network)."""
    return int(_t2g_collection(roi, start, end).size().getInfo())


def _t2g_biomass(roi: "ee.Geometry", period: BiomassPeriod, days: Optional[int] = None) -> "ee.Image":
    """Returns the Time2Graze dry-matter production (t DM/ha) of the period, band ``biomass``.

    The mean of the images (0.1 gC/m²/day) times the days times
    ``DRY_MATTER_PER_GC_M2``; the days are those of the period unless ``days``
    is given (the video uses the calendar month, a monthly equivalent).
    """
    factor = T2G_GC_M2_PER_UNIT_DAY * (days or period.days) * DRY_MATTER_PER_GC_M2
    return _t2g_collection(roi, period.start, period.end).select(T2G_BAND).mean().multiply(factor).rename("biomass")


def _reduce_biomass(image: "ee.Image", roi: "ee.Geometry", scale: int, coverage: Optional["ee.Image"] = None) -> dict:
    """Measures the masked biomass image inside ``roi`` with one reduction (network).

    Args:
        image (ee.Image): Masked biomass image (band ``biomass``).
        roi (ee.Geometry): Area of interest.
        scale (int): Analysis scale in meters.
        coverage (ee.Image, optional): Bands ``grass`` and ``covered`` to also sum their hectares.

    Returns:
        dict: ``total`` (t DM), ``valid`` (productive pasture ha), ``area`` (ha),
        ``p2``/``p98`` (pixel percentiles, None without valid pixels) and, with
        ``coverage``, ``grass`` and ``covered`` (ha).
    """
    hectares = ee.Image.pixelArea().divide(_HECTARE)
    bands = [
        image.multiply(hectares).rename("total"),
        hectares.updateMask(image.mask()).rename("valid"),
        hectares.rename("area"),
        image.rename("pixel"),
    ]
    reducer = (
        ee.Reducer.sum().setOutputs(["total"])
        .combine(ee.Reducer.sum().setOutputs(["valid"]), sharedInputs=False)
        .combine(ee.Reducer.sum().setOutputs(["area"]), sharedInputs=False)
        .combine(ee.Reducer.percentile([2, 98], ["p2", "p98"]), sharedInputs=False)
    )
    if coverage is not None:
        bands += [coverage.select("grass").multiply(hectares).rename("grass"), coverage.select("covered").multiply(hectares).rename("covered")]
        reducer = reducer.combine(ee.Reducer.sum().setOutputs(["grass"]), sharedInputs=False).combine(
            ee.Reducer.sum().setOutputs(["covered"]), sharedInputs=False)
    stack = ee.Image.cat(bands)
    return stack.reduceRegion(reducer, roi, scale, maxPixels=MAX_PIXELS).getInfo()


def _coverage_image(biomass: "ee.Image", mask_year: int) -> "ee.Image":
    """Returns the boolean bands ``grass`` (grassland mask) and ``covered`` (grassland where the productivity has data)."""
    grass = _mask_image(mask_year)
    return ee.Image.cat([grass.rename("grass"), grass.And(biomass.mask()).rename("covered")])


def _mask_biomass(biomass: "ee.Image", mask_year: int) -> "ee.Image":
    """Masks a biomass image to the Global Pasture Watch grassland of ``mask_year`` and positive values."""
    return biomass.updateMask(_mask_image(mask_year).And(biomass.gt(0)))


def _stats_key(geom_key: str, period: BiomassPeriod) -> tuple:
    """Returns the cache key of a geometry and period (source, year, month, end day, method version)."""
    return (geom_key, period.source, period.year, period.month, period.end.isoformat(), METHOD_VERSION)


def build_biomass(feature: Feature, year: Optional[int] = None, month: Optional[int] = None) -> tuple[BiomassImage, "ee.Geometry"]:
    """Builds the productivity image of an area for a period.

    Args:
        feature (Feature): Registered area.
        year (int | None): Requested year (None: latest Global Pasture Watch year).
        month (int | None): Requested month (1-12), served by Time2Graze.

    Returns:
        tuple: (BiomassImage, the area as ee.Geometry). Pixels outside the
        Global Pasture Watch grassland mask or without productivity are masked.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        BiomassPeriodUnavailable: If the period has no data for the area.
    """
    roi = pasture.feature_geometry(feature)
    period = resolve_biomass_period(year, month)
    n_images = None
    if period.source is BiomassSource.TIME2GRAZE:
        n_images = _t2g_count(roi, period.start, period.end)
        if n_images == 0:
            first, last = _unavailable_range()
            raise BiomassPeriodUnavailable("month" if period.month else "year", first, last)
        biomass = _t2g_biomass(roi, period)
    else:
        biomass = _gpp_biomass(period.year)
    image = _mask_biomass(biomass, period.mask_year)
    coverage = _coverage_image(biomass, period.mask_year) if period.source is BiomassSource.TIME2GRAZE else None
    log_debug(f"build_biomass: period={period.label}, source={period.source.name}")
    return BiomassImage(image, period, n_images, _stats_key(pasture.geometry_key(feature), period), coverage), roi


def _disk_cache() -> DiskCache:
    """Returns the optional disk cache configured by ``PASTO10_CACHE_DIR`` (disabled when unset)."""
    return DiskCache(os.getenv(pasture.CACHE_DIR_ENV))


def _load_stats(key: tuple) -> Optional[BiomassStats]:
    """Reads stats from the disk cache; None on a miss or an invalid file."""
    payload = _disk_cache().load(key)
    if payload is None:
        return None
    try:
        return BiomassStats.model_validate(payload)
    except ValueError:
        return None


def measure_biomass(biomass: BiomassImage, roi: "ee.Geometry") -> BiomassStats:
    """Measures the mean and total dry-matter production of the pasture inside ``roi``.

    The result is cached per process under ``biomass.key`` (and on disk when
    ``PASTO10_CACHE_DIR`` is set); concurrent requests for one key measure once.

    Args:
        biomass (BiomassImage): Image to measure.
        roi (ee.Geometry): Area of interest.

    Returns:
        BiomassStats: Mean (area-weighted) and total figures, rounded to 2 decimals.

    Raises:
        NoPastureBiomass: If less than ``MIN_PASTURE_FRACTION`` of the area is productive pasture.
        Exception: Any Earth Engine error (computation, quota, network).

    Side effects:
        Fills the in-memory cache and, when enabled, the disk cache.
    """
    period = biomass.period

    def compute() -> BiomassStats:
        raw = _reduce_biomass(biomass.image, roi, period.scale, biomass.coverage)
        valid, area = float(raw.get("valid") or 0.0), float(raw.get("area") or 0.0)
        grass, covered = float(raw.get("grass") or 0.0), float(raw.get("covered") or 0.0)
        if valid <= 0 or valid < MIN_PASTURE_FRACTION * area:
            raise NoPastureBiomass()
        total = float(raw["total"])
        coverage = round(covered / grass, 3) if biomass.coverage is not None and grass > 0 else None
        return BiomassStats(
            source=period.source, period_label=period.label, start=period.start.isoformat(),
            end=period.end.isoformat(), mean_t_ha=round(total / valid, 2), total_t=round(total, 2),
            p02_t_ha=round(float(raw["p2"]), 2), p98_t_ha=round(float(raw["p98"]), 2),
            pasture_ha=round(valid, 2), area_ha=round(area, 2), scale_m=period.scale,
            mask_year=period.mask_year, year=period.year, partial=period.partial,
            n_images=biomass.n_images, days_covered=period.days, data_coverage=coverage,
        )

    return _stats_cache.get_or_compute(
        biomass.key, compute,
        load=lambda: _load_stats(biomass.key),
        store=lambda stats: _disk_cache().store(biomass.key, stats.model_dump(mode="json")),
    )


def get_biomass_total(feature: Feature, year: Optional[int] = None, month: Optional[int] = None) -> BiomassStats:
    """Measures the dry-matter production of a registered area for a period.

    The caches are consulted before the image is built; period resolution may
    query Earth Engine (the Time2Graze range is cached for a day).

    Args:
        feature (Feature): Registered area.
        year (int | None): Requested year (None: latest Global Pasture Watch year).
        month (int | None): Requested month (1-12), served by Time2Graze.

    Returns:
        BiomassStats: The measured figures.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        BiomassPeriodUnavailable: If the period has no data.
        NoPastureBiomass: If the area has no productive pasture.
        Exception: Any Earth Engine computation error.
    """
    initialize_ee()
    period = resolve_biomass_period(year, month)
    key = _stats_key(pasture.geometry_key(feature), period)
    cached = _stats_cache.peek(key) or _load_stats(key)
    if cached is not None:
        return cached
    biomass, roi = build_biomass(feature, year, month)
    return measure_biomass(biomass, roi)


MIN_FRAME_DAYS = 10
MIN_FRAME_COVERAGE = 0.25
MAX_MONTHLY_FRAMES = 24
DEFAULT_ANNUAL_FRAMES = 10
MAX_ANNUAL_FRAMES = 12


@dataclass(frozen=True)
class BiomassFrame:
    """One frame of a biomass time series.

    Attributes:
        label (str): Reference period (``"2026-07"``, ``"2026-07*"`` when the month is only partly covered, or ``"2021"``).
        image (BiomassImage): Masked biomass image of the frame (its key is unique to the frame).
        partial (bool): True for a partly covered month scaled to its calendar days.
    """

    label: str
    image: BiomassImage
    partial: bool = False


@dataclass(frozen=True)
class BiomassSeries:
    """A biomass time series for a video.

    Attributes:
        mode (str): ``"monthly"`` (Time2Graze, monthly equivalents) or ``"annual"`` (Global Pasture Watch fallback).
        reason (str): Why the mode was chosen: ``"time2graze"`` or ``"fewer_than_two_months"``.
        frames (list[BiomassFrame]): Frames in chronological order.
        roi (ee.Geometry): The area as an Earth Engine geometry.
    """

    mode: str
    reason: str
    frames: list[BiomassFrame]
    roi: "ee.Geometry"


def _checked_months(
    roi: "ee.Geometry", today: Optional[date] = None, check: Optional[Callable[[], None]] = None,
) -> list[tuple[BiomassPeriod, int]]:
    """Finds the usable Time2Graze months over an area, newest first, with their image counts.

    Months are visited from the most recent backwards and the search stops as
    soon as ``MAX_MONTHLY_FRAMES`` qualify, so a long Time2Graze history costs
    no extra Earth Engine calls.

    Args:
        roi (ee.Geometry): Area of interest.
        today (date, optional): Upper limit for the months (defaults to today, UTC).
        check (Callable, optional): Called before each month; may raise to abort (deadline).

    Returns:
        list[tuple[BiomassPeriod, int]]: (period with covered days, number of images), newest first.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    available = t2g_date_range()
    if available is None:
        return []
    limit = min(available[1], today or datetime.now(timezone.utc).date())
    year, month = limit.year, limit.month
    found: list[tuple[BiomassPeriod, int]] = []
    while (year, month) >= (available[0].year, available[0].month) and len(found) < MAX_MONTHLY_FRAMES:
        if check is not None:
            check()
        try:
            period = resolve_biomass_period(year, month)
        except BiomassPeriodUnavailable:
            period = None
        if period is not None and period.days >= MIN_FRAME_DAYS and period.end <= limit:
            images = _t2g_count(roi, period.start, period.end)
            if images > 0:
                found.append((period, images))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return found


def monthly_candidates(roi: "ee.Geometry", today: Optional[date] = None) -> list[BiomassPeriod]:
    """Lists the Time2Graze months usable as video frames over an area.

    A month qualifies when it lies within the Time2Graze date range up to
    ``today``, has at least ``MIN_FRAME_DAYS`` covered days and has images over
    the area. At most the last ``MAX_MONTHLY_FRAMES`` months are kept.

    Args:
        roi (ee.Geometry): Area of interest.
        today (date, optional): Upper limit for the months (defaults to today, UTC).

    Returns:
        list[BiomassPeriod]: Month periods in chronological order (covered days, ``partial`` flagged).

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    return [period for period, _ in reversed(_checked_months(roi, today))]


def _monthly_frame(feature: Feature, roi: "ee.Geometry", period: BiomassPeriod, n_images: int) -> BiomassFrame:
    """Builds a monthly-equivalent frame: the mean daily production times the days of the calendar month."""
    calendar_days = calendar.monthrange(period.year, period.month)[1]
    raw = _t2g_biomass(roi, period, calendar_days)
    key = _stats_key(pasture.geometry_key(feature), period) + ("monthly-equivalent",)
    frame_image = BiomassImage(_mask_biomass(raw, period.mask_year), period, n_images, key, _coverage_image(raw, period.mask_year))
    return BiomassFrame(period.label + ("*" if period.partial else ""), frame_image, period.partial)


def build_biomass_series(
    feature: Feature, years: Optional[int] = None, today: Optional[date] = None, check: Optional[Callable[[], None]] = None,
    force_annual: bool = False,
) -> BiomassSeries:
    """Builds the frames of the biomass video of an area.

    Mode rule: with at least two usable Time2Graze months, one frame per month
    (monthly equivalents: the mean daily production times the calendar days;
    partly covered months are flagged with ``*``). Otherwise the fallback is
    annual: the last ``years`` Global Pasture Watch years (default
    ``DEFAULT_ANNUAL_FRAMES``, at most ``MAX_ANNUAL_FRAMES``), each with its own
    grassland mask. The fallback is not monthly.

    Args:
        feature (Feature): Registered area.
        years (int, optional): Number of annual frames for the fallback.
        today (date, optional): Upper limit for the months (defaults to today, UTC).
        check (Callable, optional): Called between Earth Engine steps; may raise to abort (deadline).
        force_annual (bool): Skip the monthly mode (used when the monthly frames have no productive pasture).

    Returns:
        BiomassSeries: Mode, reason and frames in chronological order.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        BiomassPeriodUnavailable: If an annual frame has no data.
    """
    roi = pasture.feature_geometry(feature)
    months = [] if force_annual else list(reversed(_checked_months(roi, today, check)))
    if len(months) >= 2:
        return BiomassSeries("monthly", "time2graze", [_monthly_frame(feature, roi, period, images) for period, images in months], roi)
    count = max(2, min(years or DEFAULT_ANNUAL_FRAMES, MAX_ANNUAL_FRAMES))
    frames = []
    for year in range(GPP_LAST_YEAR - count + 1, GPP_LAST_YEAR + 1):
        if check is not None:
            check()
        biomass, _ = build_biomass(feature, year, None)
        frames.append(BiomassFrame(str(year), biomass))
    return BiomassSeries("annual", "months_without_pasture" if force_annual else "fewer_than_two_months", frames, roi)
