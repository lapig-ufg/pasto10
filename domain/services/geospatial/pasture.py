"""Pasture classification and area service (Earth Engine).

For the years covered by Global Pasture Watch (``GPW_FIRST_YEAR`` to
``GPW_LAST_YEAR``) the published annual grassland classes are used as is. For
later years, where no map exists yet, a Random Forest is trained on the fly
with samples of the Global Pasture Watch v1-1 maps of the three previous years
(up to 2024) and the Google Satellite Embedding (AlphaEarth) of the same
years, then applied to the embedding of the requested year. Both paths
yield an image with a single band ``cls``:
0 = not grassland, 1 = cultivated grassland, 2 = natural/semi-natural grassland.

The ``ee`` coupling is confined to the small ``_``-prefixed functions below,
so tests can replace them without Earth Engine.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

import ee

from semente.logging import log_debug

from domain.schemas.feature import Feature
from domain.schemas.pasture import PastureAreaStats, PastureSource
from domain.services.geospatial.gee import initialize_ee
from domain.utils.cache_utils import DiskCache, SingleFlightCache

GPW_FIRST_YEAR = 2000
GPW_LAST_YEAR = 2022
TRAIN_LAST_YEAR = 2024
TRAIN_WINDOW_YEARS = 3

GPW_COLLECTION = "projects/global-pasture-watch/assets/ggc-30m/v1/grassland_c"
TRAIN_COLLECTION = "projects/global-pasture-watch/assets/ggc-30m/v1-1/grassland_c"
EMBEDDING_COLLECTION = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
CLASS_BAND = "dominant_class"
LABEL = "cls"
EMBEDDING_BANDS = [f"A{index:02d}" for index in range(64)]

NOT_GRASSLAND = 0
CULTIVATED = 1
NATURAL = 2
CLASS_VALUES = [NOT_GRASSLAND, CULTIVATED, NATURAL]

GPW_SCALE_METERS = 30
CLASSIFICATION_SCALE_METERS = 10
TRAIN_SCALE_METERS = 30
TRAIN_REGION_BUFFER_METERS = 5000
CLASS_POINTS = [300, 300, 300]
TREES = 100
SEED = 42
MAX_PIXELS = 1e9
_SQUARE_METERS_PER_HECTARE = 10_000.0

CACHE_DIR_ENV = "PASTO10_CACHE_DIR"
METHOD_VERSION = "rf-2"
_STATS_CACHE_SIZE = 128
_CLASSIFIER_CACHE_SIZE = 16
_LATEST_YEAR_TTL_SECONDS = 3600
_stats_cache = SingleFlightCache(_STATS_CACHE_SIZE)
_classifier_cache = SingleFlightCache(_CLASSIFIER_CACHE_SIZE)
_latest_year_cache = SingleFlightCache(_STATS_CACHE_SIZE, ttl=_LATEST_YEAR_TTL_SECONDS)


class YearUnavailable(Exception):
    """The requested year has no pasture data for the area.

    Attributes:
        latest (int): Most recent year that can be analyzed for the area.
    """

    def __init__(self, latest: int):
        """Creates the error.

        Args:
            latest (int): Most recent year that can be analyzed for the area.
        """
        super().__init__(f"Year unavailable; latest available year: {latest}")
        self.latest = latest


@dataclass(frozen=True)
class PastureClassification:
    """A pasture class image ready to be measured or drawn.

    Attributes:
        image (ee.Image): Single-band image ``cls`` (0, 1, 2).
        year (int): Reference year.
        source (PastureSource): Dataset behind the classes.
        scale (int): Analysis scale in meters.
        key (tuple): Stats cache key (geometry hash, year, source, method version).
    """

    image: "ee.Image"
    year: int
    source: PastureSource
    scale: int
    key: tuple


def feature_geometry(feature: Feature) -> "ee.Geometry":
    """Converts a registered feature into an Earth Engine geometry.

    Args:
        feature (Feature): Feature whose coords are GeoJSON MultiPolygon nesting.

    Returns:
        ee.Geometry: Planar (non-geodesic) MultiPolygon in EPSG:4326.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
    """
    initialize_ee()
    return ee.Geometry.MultiPolygon(feature.get_coords(), proj="EPSG:4326", geodesic=False)


def _latest_embedding_year(roi: "ee.Geometry") -> Optional[int]:
    """Returns the latest year with a Satellite Embedding image over the area, or None (network)."""
    collection = ee.ImageCollection(EMBEDDING_COLLECTION).filterBounds(roi)
    latest = collection.aggregate_max("system:time_start").getInfo()
    return ee.Date(latest).get("year").getInfo() if latest is not None else None


def _cached_latest_embedding_year(roi: "ee.Geometry", geometry_key: Optional[str]) -> int:
    """Returns the latest embedding year over the area, cached for ``_LATEST_YEAR_TTL_SECONDS``.

    Args:
        roi (ee.Geometry): Area of interest.
        geometry_key (str, optional): Geometry hash used as cache key; None skips the cache.

    Returns:
        int: The latest embedding year, or ``GPW_LAST_YEAR`` when there is none.
    """
    def lookup() -> int:
        return _latest_embedding_year(roi) or GPW_LAST_YEAR

    if geometry_key is None:
        return lookup()
    return _latest_year_cache.get_or_compute(geometry_key, lookup)


def _gpw_image(year: int) -> "ee.Image":
    """Returns the Global Pasture Watch class image of a year, band renamed to ``cls``.

    The asset is masked (no data) wherever its model did not run (water, bare
    ground, dense urban areas...); probes showed it is unmasked, with class 0,
    over forests and croplands, so masked pixels are treated as class 0
    (not grassland) and the measured area covers the whole polygon.

    Raises:
        YearUnavailable: If the collection has no image for the year (network check).
    """
    collection = ee.ImageCollection(GPW_COLLECTION).filter(ee.Filter.eq("system:index", str(year)))
    if collection.size().getInfo() == 0:
        raise YearUnavailable(latest=GPW_LAST_YEAR)
    return collection.first().select(CLASS_BAND).rename(LABEL).unmask(NOT_GRASSLAND)


def _embedding_image(roi: "ee.Geometry", year: int) -> "ee.Image":
    """Returns the mosaic of the year's Satellite Embedding tiles over ``roi``."""
    return (
        ee.ImageCollection(EMBEDDING_COLLECTION)
        .filterBounds(roi)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .mosaic()
        .select(EMBEDDING_BANDS)
    )


def training_years(year: int) -> list[int]:
    """Returns the label years used to train the forest that predicts ``year``.

    The ``TRAIN_WINDOW_YEARS`` years before the requested one, capped at
    ``TRAIN_LAST_YEAR`` (the last year with v1-1 labels): 2023 trains on
    2020-2022, 2024 on 2021-2023 and any later year on 2022-2024. The year
    being predicted is never a training year.

    Args:
        year (int): The year to predict (after ``GPW_LAST_YEAR``).

    Returns:
        list[int]: The ``TRAIN_WINDOW_YEARS`` consecutive label years, ascending.
    """
    last = min(year - 1, TRAIN_LAST_YEAR)
    return list(range(last - TRAIN_WINDOW_YEARS + 1, last + 1))


def _train_classifier(roi: "ee.Geometry", years: list[int]):
    """Trains the Random Forest on the labels of ``years`` around the area (network).

    Training labels are unmasked to class 0 like ``_gpw_image`` so built-up,
    water and bare areas give the forest examples of non-grassland. Samples
    are drawn (stratified, ``CLASS_POINTS`` per class and year, fixed seed)
    from the v1-1 Global Pasture Watch map of each year, paired with the
    embedding of that year. The trees are materialized into an explicit
    classifier, so area measurement and thumbnail reuse the same model.

    Returns:
        ee.Classifier | int: The forest, or, when the training region holds a
        single class (open water, desert, uniform pasture), that class as an
        ``int``: Earth Engine cannot train on one class, and every pixel of
        such a region belongs to it anyway.
    """
    region = roi.buffer(TRAIN_REGION_BUFFER_METERS)
    samples = ee.FeatureCollection([
        _label_image(year).addBands(_embedding_image(region, year)).stratifiedSample(
            numPoints=0,
            classBand=LABEL,
            region=region,
            scale=TRAIN_SCALE_METERS,
            classValues=CLASS_VALUES,
            classPoints=CLASS_POINTS,
            seed=SEED + year,
            dropNulls=True,
        )
        for year in years
    ]).flatten()
    trained = ee.Classifier.smileRandomForest(TREES).train(samples, LABEL, EMBEDDING_BANDS)
    try:
        return ee.Classifier.decisionTreeEnsemble(trained.explain().get("trees").getInfo())
    except ee.EEException:
        classes = samples.aggregate_array(LABEL).distinct().getInfo()
        if len(classes) > 1:
            raise
        return int(classes[0]) if classes else NOT_GRASSLAND


def _label_image(year: int) -> "ee.Image":
    """Returns the v1-1 class image of a year used as training label (masked pixels as class 0)."""
    return (
        ee.ImageCollection(TRAIN_COLLECTION)
        .filter(ee.Filter.eq("system:index", str(year)))
        .first()
        .select(CLASS_BAND)
        .rename(LABEL)
        .unmask(NOT_GRASSLAND)
    )


def _predicted_image(roi: "ee.Geometry", year: int, geometry_key: str) -> "ee.Image":
    """Classifies the embedding of ``year`` with the area's Random Forest.

    The forest is trained once per (geometry, training years, method version)
    and kept in a per-process cache with single-flight semantics: concurrent
    requests wait for the same training and every later year, tool call and
    map of the area reuse it.
    """
    years = training_years(year)
    classifier = _classifier_cache.get_or_compute(
        (geometry_key, tuple(years), METHOD_VERSION), lambda: _train_classifier(roi, years),
    )
    if isinstance(classifier, int):
        return ee.Image.constant(classifier).rename(LABEL)
    return _embedding_image(roi, year).classify(classifier).rename(LABEL).unmask(NOT_GRASSLAND)


def _area_by_class(image: "ee.Image", roi: "ee.Geometry", scale: int) -> dict[int, float]:
    """Sums the pixel area (m²) of each class of ``image`` inside ``roi`` (network)."""
    reducer = ee.Reducer.sum().group(groupField=1, groupName=LABEL)
    result = (
        ee.Image.pixelArea()
        .addBands(image)
        .reduceRegion(reducer=reducer, geometry=roi, scale=scale, maxPixels=MAX_PIXELS)
        .getInfo()
    )
    return {int(group[LABEL]): float(group["sum"]) for group in result.get("groups", [])}


def _to_hectares(square_meters: float) -> float:
    """Converts m² to hectares, rounded to 2 decimals."""
    return round(square_meters / _SQUARE_METERS_PER_HECTARE, 2)


def resolve_year(roi: "ee.Geometry", year: Optional[int], geometry_key: Optional[str] = None) -> int:
    """Validates the requested year; without one, picks ``GPW_LAST_YEAR``.

    Years from ``GPW_FIRST_YEAR`` to ``GPW_LAST_YEAR`` are always available
    (published Global Pasture Watch maps). Later years are on-the-fly
    estimates and need a Satellite Embedding image over the area, so they must
    be requested explicitly: the default is never an estimate.

    Args:
        roi (ee.Geometry): Area of interest.
        year (int | None): Requested year, or None for ``GPW_LAST_YEAR``.
        geometry_key (str, optional): Geometry hash; lets the lookup of the
            latest embedding year be cached for an hour.

    Returns:
        int: The year to analyze.

    Raises:
        YearUnavailable: If the year is before ``GPW_FIRST_YEAR`` or after the
        latest year with an embedding (``latest`` carries that year).
    """
    if year is None:
        return GPW_LAST_YEAR
    if GPW_FIRST_YEAR <= year <= GPW_LAST_YEAR:
        return year
    latest = max(_cached_latest_embedding_year(roi, geometry_key), GPW_LAST_YEAR)
    if year < GPW_FIRST_YEAR or year > latest:
        raise YearUnavailable(latest=latest)
    return year


def _geometry_key(feature: Feature) -> str:
    """Returns a short hash of the feature geometry, so edited areas never hit stale cache entries."""
    return hashlib.sha1(json.dumps(feature.get_coords()).encode()).hexdigest()[:16]


def _source_for(year: int) -> PastureSource:
    """Returns the dataset that serves a (validated) year."""
    return PastureSource.GPW if year <= GPW_LAST_YEAR else PastureSource.CLASSIFICATION


def _stats_key(geometry_key: str, year: int) -> tuple:
    """Returns the cache key of the stats of a geometry and (validated) year.

    The key carries the geometry hash, the year, the source and the method
    version, so a change in the classifier design never reuses old figures.
    """
    return (geometry_key, year, _source_for(year), METHOD_VERSION)


def _build_classification(feature: Feature, roi: "ee.Geometry", year: int) -> PastureClassification:
    """Builds the class image of an already validated year (trains the forest when needed)."""
    geometry_key = _geometry_key(feature)
    source = _source_for(year)
    if source is PastureSource.GPW:
        image, scale = _gpw_image(year), GPW_SCALE_METERS
    else:
        image, scale = _predicted_image(roi, year, geometry_key), CLASSIFICATION_SCALE_METERS
    return PastureClassification(image, year, source, scale, _stats_key(geometry_key, year))


def classify_pasture(feature: Feature, year: Optional[int] = None) -> tuple[PastureClassification, "ee.Geometry"]:
    """Builds the pasture class image of an area for a year.

    Args:
        feature (Feature): Registered area.
        year (int | None): Reference year; None selects ``GPW_LAST_YEAR``.

    Returns:
        tuple: (PastureClassification, the area as ee.Geometry). For years
        after ``GPW_LAST_YEAR`` the area's forest is trained here the first time.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        YearUnavailable: If the year has no data (see ``resolve_year``).
    """
    roi = feature_geometry(feature)
    classification = _build_classification(feature, roi, resolve_year(roi, year, _geometry_key(feature)))
    log_debug(f"classify_pasture: year={classification.year}, source={classification.source.name}")
    return classification, roi


def _disk_cache() -> DiskCache:
    """Returns the optional disk cache configured by ``PASTO10_CACHE_DIR`` (disabled when unset)."""
    return DiskCache(os.getenv(CACHE_DIR_ENV))


def _load_stats(key: tuple) -> Optional[PastureAreaStats]:
    """Reads stats from the disk cache; None on a miss or an invalid file."""
    payload = _disk_cache().load(key)
    if payload is None:
        return None
    try:
        return PastureAreaStats.model_validate(payload)
    except ValueError:
        return None


def measure_pasture_area(classification: PastureClassification, roi: "ee.Geometry") -> PastureAreaStats:
    """Measures cultivated, natural and total pasture area inside ``roi``.

    Masked pixels were already turned into class 0 (see ``_gpw_image``), so
    ``area_ha`` covers the whole polygon. The Random Forest is not bit-for-bit
    reproducible between runs, so results are cached per process (last
    ``_STATS_CACHE_SIZE`` entries) under ``classification.key``: the same
    geometry and year always report the same figures until the process ends
    (or, with ``PASTO10_CACHE_DIR`` set, until the cache directory or the
    ``METHOD_VERSION`` changes). Concurrent requests for one key measure once.
    ``PastureAreaStats`` is immutable, so cached objects can be shared.

    Args:
        classification (PastureClassification): Class image to measure.
        roi (ee.Geometry): Area of interest.

    Returns:
        PastureAreaStats: Hectares rounded to 2 decimals; ``pasture_ha`` is
        the sum of the two grassland classes.

    Raises:
        Exception: Any Earth Engine error (computation, quota, network).

    Side effects:
        Fills the in-memory cache and, when enabled, writes one JSON file per
        key into the disk cache directory.
    """
    def compute() -> PastureAreaStats:
        square_meters = _area_by_class(classification.image, roi, classification.scale)
        cultivated = _to_hectares(square_meters.get(CULTIVATED, 0.0))
        natural = _to_hectares(square_meters.get(NATURAL, 0.0))
        return PastureAreaStats(
            year=classification.year,
            source=classification.source,
            pasture_ha=round(cultivated + natural, 2),
            cultivated_ha=cultivated,
            natural_ha=natural,
            area_ha=_to_hectares(sum(square_meters.values())),
        )

    return _stats_cache.get_or_compute(
        classification.key,
        compute,
        load=lambda: _load_stats(classification.key),
        store=lambda stats: _disk_cache().store(classification.key, stats.model_dump(mode="json")),
    )


def get_pasture_area(feature: Feature, year: Optional[int] = None) -> PastureAreaStats:
    """Measures the pasture area of a registered area for a year.

    The caches (memory, then disk when enabled) are consulted before the
    class image is built, so repeating a call for the same geometry and year
    neither retrains the forest nor queries Earth Engine for the area. Year
    validation may still query Earth Engine (cached for an hour) when the
    year is after ``GPW_LAST_YEAR``.

    Args:
        feature (Feature): Registered area.
        year (int | None): Reference year; None selects ``GPW_LAST_YEAR``
            (the latest published Global Pasture Watch map).

    Returns:
        PastureAreaStats: The measured areas.

    Raises:
        EarthEngineUnavailable: If Earth Engine cannot be initialized.
        YearUnavailable: If the year has no data.
        Exception: Any Earth Engine computation error.
    """
    roi = feature_geometry(feature)
    geometry_key = _geometry_key(feature)
    resolved = resolve_year(roi, year, geometry_key)
    cached = _stats_cache.peek(_stats_key(geometry_key, resolved)) or _load_stats(_stats_key(geometry_key, resolved))
    if cached is not None:
        return cached
    return measure_pasture_area(_build_classification(feature, roi, resolved), roi)
