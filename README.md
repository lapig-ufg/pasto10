# Pasto10

**Multi-agent decision-support tool for pasture management, built on the [Semente](https://github.com/lapig-ufg/semente) framework and powered by maps from the Global Pasture Watch and Time2Graze initiatives.**

---

## About

Pasto10 is a decision-support system that combines multi-agent AI with satellite-based pasture maps to help ranchers and pasture managers around the world monitor, diagnose, and improve their pasturelands. Instead of navigating dashboards and map layers, users converse with an assistant that interprets the data for them, answers questions about specific properties or municipalities, and turns map indicators into practical management recommendations.

The project is a **showcase and reference implementation of [Semente](https://github.com/lapig-ufg/semente)**, the multi-agent AI chat framework for land use and agriculture. Just as Pasto Legal (LAPIG/UFG's WhatsApp assistant for pasture diagnostics) was the seed from which Semente was extracted, Pasto10 demonstrates how the framework grows into a new domain — in this case, one grounded in Global Pasture Watch and Time2Graze map products.

## Data

Pasto10's knowledge comes from maps produced in the context of two initiatives:

- **Global Pasture Watch** — a global collaboration delivering the first open, satellite-derived maps of pastureland extent, condition, and productivity. These layers tell us where pastures are and how degraded or vigorous they are.
- **Time2Graze** — an initiative advancing temporal analysis of grazing lands, including pasture dynamics over time series that support management decisions.

Pasto10 reads these maps through Google Earth Engine:

| Years | Source | How it is used |
| --- | --- | --- |
| 2000-2022 | Global Pasture Watch grassland classes (`ggc-30m/v1/grassland_c`, 30 m) | Used as published: cultivated grassland, natural/semi-natural grassland and non-grassland. |
| After 2022 | [Google Satellite Embedding](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_SATELLITE_EMBEDDING_V1_ANNUAL) (AlphaEarth, 10 m) | No published v1 map exists: a Random Forest (100 trees, 300 samples per class and year, fixed seed) is trained on the fly, around the registered area, with the Global Pasture Watch v1-1 classes of the three previous years (2023 uses 2020-2022, 2024 uses 2021-2023, later years use 2022-2024) and the embedding of the same years, then applied to the embedding of the requested year. |

This is a demonstration of how to combine global datasets, not a production-grade product: each country would improve it with its own policies and registries. Cells that Global Pasture Watch leaves without data are counted as non-grassland, so the analyzed area equals the whole registered area.

When no year is given the tools use 2022, the latest published Global Pasture Watch v1 map; a later year must be requested explicitly and always returns an experimental estimate. Pasture area is the sum of cultivated and natural/semi-natural grassland, in hectares inside the registered area. Time2Graze is used by the biomass tools (below), not by the pasture tools.

### Validation of the post-2022 estimate

The v1-1 collection has labels for 2023 and 2024, so the estimate can be checked against official maps (`tools/benchmark_pasture.py`, 27 sites of 2 km radius on all continents, never training on the evaluated year, pasture versus not pasture at 30 m):

| Design | Accuracy | F1 (pasture) | Median relative error of pasture ha | Mean relative error |
| --- | --- | --- | --- | --- |
| Single year (2022) labels | 0.88 | 0.79 | 0.13 | 0.41 |
| Three previous years (adopted) | 0.90 | 0.83 | 0.10 | 0.46 |

(Relative error is `|estimate - official| / max(official, 2% of the area)`.) The three-year window improved accuracy, F1 and the median error, but the mean relative error got worse (0.41 to 0.46) because a few areas deviate strongly. On average the estimate agrees with the official map on about 90% of the pixels, but individual areas deviate strongly (for example a wetland mosaic in the Argentine Pampas: official 29 ha, estimate about 310 ha; on the 15 fixed test properties, Queensland 2023 was the worst case: official 896 ha, estimate 86 ha). Pooling several years, balancing classes differently, larger training regions and regularization were also tried: none beat the three-year window by a clear margin, so the simpler design was kept. On the 15 fixed test properties the mean accuracy was 0.88 and the median relative error 0.09. The classifier can also report small amounts of pasture where there is none in arid or urban areas (for example about 4 ha in a 1 km buffer and about 61 ha in a 2 km buffer in the Atacama desert in 2023). The estimate is therefore good as an order of magnitude and never as a measurement; for this reason the default year stays 2022 and the figures of 2022 and of later years must not be compared as a trend (different source and resolution, 30 m versus 10 m).

### Masked pixels of Global Pasture Watch

Global Pasture Watch leaves its class band masked where its model did not run (water, bare ground, dense urban areas). `tools/check_gpw_mask.py` compares those pixels with ESA WorldCover 2021 over 27 buffers on all continents: 74.5% of the masked pixels are built-up, bare, snow or water, 1.6% are WorldCover grassland, and only 1.0% of all WorldCover grassland is masked; the remainder is tree, shrub or cropland. Masked therefore means not grassland, and Pasto10 counts it as class 0.

### Biomass (dry-matter production)

`generate_biomass_map` and `get_biomass_total` report the dry-matter production of the pasture of a registered area: mean (t DM/ha) and total (t DM). The figures are the accumulated production of a period, not standing biomass nor available forage, and they say nothing about stocking capacity.

| Request | Source | Resolution |
| --- | --- | --- |
| No year, no month | Global Pasture Watch annual productivity (`ggpp-30m/v1-1/ugpp_m`, band `gc_m2`), latest year (2025) | 30 m |
| A year from 2000 to 2025 | The same collection, that year | 30 m |
| A month (with or without year) | Time2Graze productivity (`projects/wri-lcl-time2graze/assets/ugpp_prod_10m_v1`, band `ugpp`), average of the images of the month times its days | 10 m |
| A year after 2025 | Time2Graze accumulated over the part of the year with data (the real period and number of days are reported) | 10 m |

The source is chosen for the whole area, never pixel by pixel, and a missing period is reported as unavailable instead of being replaced by another source. Time2Graze covers only recent months (currently a few recent months, discovered dynamically and cached for a day) and only part of the world, so many areas have no monthly data. Pasture pixels are those of the Global Pasture Watch grassland map (`ggc-30m/v1/grassland_c`, masked pixels counted as not grassland); that map ends in 2022, so later periods use the 2022 mask and say so. An area with less than 1% of productive pasture returns no figures.

Productivity (gC/m²) becomes dry matter with a single constant, `DRY_MATTER_PER_GC_M2 = 0.0135` t DM/ha per gC/m² (0.50 light-use efficiency x 2.7 carbon-to-dry-matter x 0.01 unit conversion). It is a general approximation calibrated for cultivated tropical pastures; elsewhere read it as an order of magnitude. Annual and monthly figures, and Global Pasture Watch and Time2Graze figures, must not be compared with each other.

### Biomass video

`generate_biomass_video` animates the evolution of the dry-matter production of a registered area over the satellite image, with one fixed color scale across the frames (from the lowest 2nd percentile to the highest 98th percentile of all frames) and the reference period written on each frame. The mode is chosen by the data, never by the user:

- **Monthly** (Time2Graze): when at least two recent months over the area have at least 10 covered days and images; each frame is a monthly equivalent (mean daily production times the days of the calendar month), and a month only partly covered in time is scaled and marked with `*`, and a month whose Time2Graze images cover less than 25% of the grassland of the area is skipped (the share of the grassland with data is reported in `data_coverage`).
- **Annual fallback** (Global Pasture Watch): otherwise (also when the monthly frames have no productive pasture, for example in a cold season) one frame per year for the last 10 years (`years` sets 2 to 12); this fallback is not monthly and the tool says so.

The pasture mask and the dry-matter conversion are the ones of the biomass tools (`ggpp-30m/v1-1/ugpp_m`, grassland mask `ggc-30m/v1` capped at 2022), a deliberate deviation from the `ggpp-30m/v1` collection named in the issue so that all biomass products share one dataset. The frames are encoded to an H.264 yuv420p MP4 (640x640, last frame held about 2 s) with `ffmpeg` (found in `PATH` or through `FFMPEG_PATH`; the Dockerfile installs it); without it the tool answers that the encoder is unavailable. At most 24 frames, 90 s deadline, finished videos cached per process.

### PDF bulletin

`generate_area_bulletin` consolidates a registered area into a shareable PDF (1-2 pages, built with `reportlab`): location and map, pasture map and area (2022), dry-matter production map and totals (latest annual year), method notes and the data and basemap attributions. It reuses the cached statistics of the pasture and biomass tools (no new Earth Engine measurement), but the maps and basemap tiles are drawn again for every bulletin, which takes about 10-25 seconds. If one section has no data (for example an area without pasture) it is shown as "not available" and the answer is partial; with nothing available no PDF is produced. All the labels of the PDF live in `tools.yml` (`report_tools`). The text is rendered in Latin-1 (covers English, Spanish, Portuguese and other Western European languages); characters outside it show as `?`.

### Animal units (skill)

The `ua-calculator` skill (`domain/skills/livestock/ua-calculator/SKILL.md`) teaches the agent to compute Animal Units, the actual stocking rate and the carrying capacity of a registered pasture from the dry-matter production returned by `get_biomass_total`, using the calculator tools: 1 AU = 450 kg live weight, intake of 2.5% of the live weight per day, a protection factor of 2 (about 4.1 t DM/AU/year of demand, 8.2 with protection) and the productive pasture hectares. Short Time2Graze periods are annualized with `365 / days covered` (never by multiplying a month by 12) and the user is warned that this extrapolates a short period. The assumptions are declared in every answer and can be overridden; the result is an order of magnitude, not a recommendation.

### Weather (Open-Meteo)

Five tools query [Open-Meteo](https://open-meteo.com/) for the center of a registered area, always in the timezone of the area (`timezone=auto`; the local "today" comes from the `utc_offset_seconds` of the response): `get_monthly_precipitation_forecast` (seasonal, up to 7 months), `get_daily_precipitation_forecast` (one day up to about 33 days ahead, ensemble median and quartiles from `ncep_gefs_seamless`, falling back to `gem_global`), `get_rain_season_onset_forecast` and `get_dry_season_onset_forecast`, and `get_temperature_forecast` (up to 16 days). No API key or extra dependency is needed (`requests` with retries); responses are cached for an hour per rounded coordinate. Weather data by Open-Meteo.com (CC-BY 4.0).

The season onset is a simple global heuristic, for demonstration: the 30-day rolling rainfall total of each ensemble member, median over the members; the area starts wet when the last 30 days had at least 60 mm; the rainy season starts when the total stays above 60 mm for 7 days and the dry season when it falls below 30 mm, each only from the opposite state (`domain/services/geospatial/season_forecast.py`). It suits regions with a marked wet/dry cycle, and seasonal forecasts have low skill.

### Caches

Results are cached per process (areas, trained forests and the latest embedding year for an hour), and concurrent requests for the same geometry and year compute only once. Because the forest is not bit-for-bit reproducible, the first estimate of a year is kept until the process restarts. Set `PASTO10_CACHE_DIR` to a writable directory to also keep results on disk between runs (off by default; entries are keyed by geometry, year, source and method version).

Both map portfolios are developed with contributions from [LAPIG/UFG](https://lapig.iesa.ufg.br/), the Image Processing and Geoprocessing Laboratory of the Federal University of Goiás, which maintains this repository.

## Architecture

## Getting started

Install the dependencies with `uv sync` and run the tests with `uv run pytest` (offline). The pasture tools query Google Earth Engine, so they need the `GEE_*` variables described below.

## Configuration

### Google Earth Engine

The pasture map and area tools need an Earth Engine project and a service account (see `.env.example`):

- `GEE_PROJECT` — Google Cloud project registered for Earth Engine.
- `GEE_SERVICE_ACCOUNT` — service account e-mail.
- `GEE_KEY_FILE` — path to the service account JSON key (keep it out of version control).

Earth Engine is initialized lazily on the first pasture request, never at import. Without these variables the tools answer that the satellite data service is unavailable.

### Test properties and end-to-end tests

`tests/fixtures/global_properties.json` (used by the pasture, biomass, weather, bulletin and video e2e tests; loader `tests/fixtures/global_properties.py`) fixes 15 properties with emphasis on South America and a few global references: cultivated pasture (Goiás in Brazil, the Bolivian Chiquitania, the Paraguayan Chaco, the Colombian Llanos, Chilean Aysén, Queensland), natural grassland and savanna (Bolivian Llanos de Moxos, Venezuelan Llanos, Uruguayan Pampas, Kenya), high-altitude pasture (Peruvian Andes), mosaics (Argentine humid Pampas, Kansas) and two negative controls (Lima, the Atacama desert). The e2e test checks that each point really has its described profile against independent assets (GPW v1, ESA WorldCover, SRTM elevation).

`uv run pytest` skips the real Earth Engine tests. With the `GEE_*` variables exported run:

```
uv run pytest -m e2e -s                                # everything
uv run pytest -m e2e -s tests/e2e/test_weather_e2e.py  # weather tools (no Earth Engine needed)
uv run pytest -m e2e -s tests/e2e/test_biomass_e2e.py  # biomass tools on the 15 properties, prints a summary table
uv run pytest -m e2e -s tests/e2e/test_generalist_e2e.py -k tools_over   # the 15 properties, prints a summary table
PASTO10_E2E_OUT=/some/dir uv run pytest -m e2e -s      # also saves the generated maps
uv run python tools/benchmark_pasture.py --designs slide3 --out bench.jsonl   # validation against v1-1
```

### Basemap providers

The confirmation image of a registered area is drawn over a tile basemap, configured by environment variables (see `.env.example`):

- `PASTO10_BASEMAP_PROVIDERS` — providers tried in order (default `esri,osm`). Unknown names are ignored with a warning; an empty value or `none` disables the basemap and the image uses a neutral background.
- `ARCGIS_API_KEY` — optional [ArcGIS Location Platform](https://location.arcgis.com/) API key. When set, Esri tiles are requested from the authenticated basemap endpoint. Pasto10 masks the key in its own log messages, but avoid enabling DEBUG logging for urllib3 in production, as it would log request URLs including the token.

Terms of use:

- **Esri World Imagery** — without an API key the public tile service is used, whose use is governed by the [Esri terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement); production deployments should set `ARCGIS_API_KEY`. The image always shows the required attribution ("Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community" and "Powered by Esri").
- **OpenStreetMap** — tiles from `tile.openstreetmap.org` follow the [OSM Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/) (light use, identifying User-Agent, respect the HTTP caching headers, no bulk downloads) and carry the "© OpenStreetMap contributors" attribution. Pasto10 is designed for light use only (one tile mosaic per registration) and keeps no local tile cache; for heavier use, switch to a commercial OSM tile provider.

## Acknowledgments

Pasto10 is developed by [LAPIG/UFG](https://lapig.iesa.ufg.br/) in the context of the Global Pasture Watch and Time2Graze initiatives. We thank the partners of both initiatives for producing and maintaining the map products that make this assistant possible.

## License

This project is licensed under the [GPL-3.0-or-later](LICENSE). As a Semente derivative, it must remain open.