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

## How the area is chosen

A coordinate is not a farm, so Pasto10 does not just draw a circle. The flow mirrors a rural property registry lookup: **coordinate -> candidate parcels (registry number, name, area, status) -> the user picks one -> the confirmed polygon**. `find_property_candidates` asks all enabled **property sources** (in parallel) for polygons around the coordinate and shows them numbered on a map; the user picks one (`select_candidate`) and confirms it. A user who already has a registry number can use `find_property_by_registry_id`, and one who has the boundary as GeoJSON can use `register_polygon`. Every polygon is labeled with where it comes from, and the assistant never calls something a "property" unless it is one:

| Origin | Label shown to the user | Source |
| --- | --- | --- |
| `registered_boundary` | registered property boundary | A real national registry (placeholder `NationalRegistrySource`, disabled by default) |
| `registered_boundary` (demo) | DEMO registry parcel (synthetic sample, not a real property boundary) | `DemoRegistrySource`: invented parcels around the 13 test properties in `domain/data/demo_registry.geojson` (registry number such as `DEMO-BRA-0001`, area, status; no owner data), enabled by default so the flow can be seen working anywhere |
| `osm_landuse` | OpenStreetMap land-use polygon (may not be a farm) | OpenStreetMap through the Overpass API (default): `landuse` farmland, meadow, farmyard, grass, orchard, vineyard and `place=farm` |
| `user_polygon` | user-provided polygon | GeoJSON pasted or attached by the user (`register_polygon`) |
| `approximate_circle` | approximate area, not a real property boundary | Buffer around the coordinate, the fallback (`start_buffer_registration_by_coordinate`) |

The default order is `demo_registry`, then `osm`. Every demo parcel carries a `synthetic_demo` warning, the tools and the maps (a red "DEMO — synthetic parcels" banner) say it is a demonstration, and the file is generated by the deterministic script `tools/make_demo_registry.py` (3 or 4 irregular parcels of about 30 to 170 ha per property, one containing the point; the urban and desert controls have none, to exercise the "no candidates" path). The OpenStreetMap source runs in parallel and is waited for at most 12 seconds, so its slowness never blocks the answer: if it does not respond in time the search continues without it and the user is told. The OpenStreetMap source keeps polygons between 0.5 and 50,000 ha, simplifies them to about 5 m, orders them (containing the point first, then nearest) and offers at most 5. A point inside a residential, commercial or industrial land use gets no farm candidate. Overpass answers are cached (an hour in memory, and on disk when `PASTO10_CACHE_DIR` is set); two public mirrors are tried with a 30 s timeout (`PASTO10_OVERPASS_URLS` overrides them). A land-use polygon only says how the land is used: it does not prove the place is a farm. **Coverage depends on the region**: it is good where mappers drew fields and poor elsewhere (see the measurement below), and when nothing is found the assistant offers the approximate circle or the user's own polygon. Data © OpenStreetMap contributors (ODbL).

In short: the demo registry shows the flow and the real-data path is a registry of your country, because OpenStreetMap alone gave useful farm candidates for only 1 of the 15 test properties (next table). A polygon provided by the user (`register_polygon`) is treated as hostile input and validated in layers: at most 1 MB (UTF-8 bytes), 20 features, 50 rings per polygon and 2,000 vertices (`PASTO10_MAX_POLYGON_VERTICES`), counted on the raw JSON before any geometry is built; finite lon/lat coordinates only (NaN and Infinity are refused); an invalid (for example self-intersecting) polygon with more than 100 vertices is refused instead of repaired; the shapely work runs in a child process killed after 3 seconds (GEOS cannot be interrupted from a thread); and an area limit (`PASTO10_MAX_POLYGON_HA`, default 50,000 ha). Rejections take well under a second (500 and 1,000 self-intersecting vertices, 300 features and NaN payloads: 0.00-0.19 s). Small invalid rings are repaired and polygons are simplified to about 5 m. KML and shapefiles are not supported yet. Overpass answers are kept on disk for 24 hours (`PASTO10_OVERPASS_CACHE_HOURS`). Whatever the origin, a chosen candidate becomes the same `Feature` (MultiPolygon, area, `center` and an enclosing `radius` in its metadata), so the pasture, biomass, weather, bulletin and video tools work unchanged.

### OpenStreetMap coverage measured on the 15 test properties

`tools/measure_osm_coverage.py` queried Overpass (2,000 m around each property, public mirrors, retries) on 2026-10-23; the raw answers are saved in `tests/fixtures/overpass/` and used by the offline tests:

| Property | Useful candidates | Seconds | Mirror | Land-use tags |
| --- | --- | --- | --- | --- |
| goias | 0 | 44.9 | overpass-api.de | - |
| beni | 0 | 43.9 | overpass-api.de | - |
| santa_cruz | 2 | 52.7 | lz4.overpass-api.de | farm, farmyard |
| chaco | 0 | 33.4 | lz4.overpass-api.de | - |
| llanos_colombia | 0 | 36.9 | lz4.overpass-api.de | - |
| cajamarca | 0 | 38.1 | lz4.overpass-api.de | - |
| pampas_ar | 0 | 41.3 | overpass-api.de | - |
| pampas_uy | 0 | 26.5 | overpass-api.de | - |
| aysen | 0 | 33.9 | overpass-api.de | - |
| llanos_venezuela | 0 | 46.9 | overpass-api.de | - |
| kansas | 0 | 30.0 | overpass-api.de | - |
| kenya (earlier point; later moved to Amboseli) | 0 | 34.9 | overpass-api.de | - |
| queensland | 0 | 31.8 | lz4.overpass-api.de | - |
| city_control | 5 | 47.2 | overpass-api.de | grass |
| desert_control | 0 | 40.9 | lz4.overpass-api.de | - |

Only 2 of the 15 points had candidates that are farms or farmyards (a `place=farm` and a `farmyard` in the Bolivian Chiquitania, 1 and 6 ha, none containing the point), and the 5 candidates around the Lima control are small urban grass polygons. In the other regions OpenStreetMap has no mapped farmland, meadow or farm polygon within 2 km, so the assistant falls back to the approximate circle or the user's own polygon. As a contrast, a point in Normandy (France) returned 5 meadow and grass polygons of 0.6 to 2.4 ha (none containing the point, all small): even where the map is dense, land-use polygons are often parcels, not whole farms, which is why a national registry (next section) gives much better candidates. The public Overpass instances were slow and sometimes unavailable (25 to 52 seconds per answer, frequent 502/504 errors), so the app waits up to 30 seconds per mirror (`PASTO10_OVERPASS_TIMEOUT`) and treats failures as "source unavailable".

## Adapting Pasto10 to your country

Pasto10 is a showcase: the global datasets (Global Pasture Watch, Time2Graze, Google Satellite Embedding, Open-Meteo) work anywhere, but every country has better local data and rules. To adapt it:

1. **Replace the demo registry with your property registry.** `DemoRegistrySource` (`domain/services/property_sources/demo_registry_source.py`) is only a synthetic sample that shows the flow; swap it for a source over your registry. Implement the `PropertySource` protocol (`domain/services/property_sources/base.py`): `find_by_point(latitude, longitude, search_radius_m)` returns `PropertyCandidate` objects (GeoJSON polygon in lon/lat, area in ha, `origin=registered_boundary`, your registry number as `candidate_id`, the data date) and `find_by_id(identifier)` returns the candidate of a registry number. `domain/services/property_sources/national_registry_source.py` documents the contract and has a commented example; a rural environmental registry, an agricultural parcel system (such as an EU LPIS) or a cadastre service all fit.
2. **Register and enable it.** Call `register_source("registry", MyRegistrySource)` (for example in `domain/__init__.py`) and list its key in `semente.yaml` (`property_sources: [registry, osm]`; the shipped default is `[demo_registry, osm]`) or in the `PASTO10_PROPERTY_SOURCES` environment variable. The first enabled source is the default of `find_property_candidates`; keep `osm` after it as a fallback if you like.
3. **Adjust the language and the rules.** Set `language` in `semente.yaml` and translate or edit `domain/prompts/agents.yml` and `tools.yml` (all text the model sees lives there, including the origin labels in `feature_type_names`); add the legal or policy wording of your country to the terms of use.
4. **Swap national datasets where they are better**, keeping the same tool contracts: land use and pasture maps (`domain/services/geospatial/pasture.py`), productivity and biomass (`biomass.py`), weather and season rules (`weather.py`, `season_forecast.py`), and the stocking assumptions of the animal-unit skill (`domain/skills/livestock/ua-calculator`).
5. **Keep what is generic**: the conversation states (choosing, pending, final), the polygon and Feature model, the maps, PDF bulletin and video, the cache layer and the tests; run `uv run pytest` after each change.

## Architecture

## Getting started

Install the dependencies with `uv sync` and run the tests with `uv run pytest` (offline). The pasture tools query Google Earth Engine, so they need the `GEE_*` variables described below.

### Running the chat locally

The chat UI is the Streamlit chat that ships with Semente, wrapped by `app/streamlit_app.py` (Semente is not modified; `app/frontend.py` documents what is adapted). The interface is in English, the debug panel starts closed, and the "send location" shortcut of Semente (fixed coordinates in Brazil) is hidden. Copy `.env.example` to `.env`, fill in `GOOGLE_API_KEY` and the `GEE_*` variables (`GEE_KEY_FILE` as an absolute path), then run:

```
scripts/run_local.sh
```

The script reads only `GOOGLE_API_KEY` and `GEE_*` from the env file (pass another file as the first argument), runs with SQLite and starts `streamlit run app/streamlit_app.py` on port 8501 (`PORT=8600 scripts/run_local.sh` to change it; extra arguments are passed to Streamlit). With Docker, `docker compose up` runs the same entrypoint. Open http://localhost:8501 and enter anonymously.

**Quick tests with the 15 test properties.** Once signed in, the sidebar has a "Test properties" box: pick one of the properties of the table below and click "Use this property". It is registered in your session right away: the synthetic demo registry parcel that contains the point (named `<property> - DEMO-XXX-0001`, origin `registered_boundary`; the message shows name, area and "synthetic demo parcel"), or an approximate circle for the two negative controls, which have no parcel and, outside production (`APP_ENV` other than `production`), the terms are accepted automatically, so you can go straight to "show the pasture map" or "biomass of the last years". If the direct registration is not possible, the button sends a chat message asking the assistant to register it. Property data comes from `domain/data/test_properties.json`. The picker, the automatic terms acceptance and the "Debug Mode" toggle exist only when `APP_ENV=development` is set explicitly (`scripts/run_local.sh` and `compose.yaml` set it; the Dockerfile does not) or `PASTO10_TEST_PICKER=1` opts the picker in; a missing `APP_ENV` leaves them off.

To register your own property instead, type its coordinates in the chat, for example `Register my farm at latitude -33.0, longitude -56.0 with a 1000 m radius`, confirm the area and give it a name. The upload box also accepts a `.geojson`/`.json` file (valid JSON, up to 1 MB; larger or invalid files are rejected with a warning, one file per message). The chat message only gets a short note (`[GeoJSON file attached: name, N vertices]`); the content is stored in the session state under `uploaded_geojson` for the duration of that turn and removed afterwards. `register_polygon` reads it from there when its `geojson` argument is empty. Then ask for the pasture map, biomass, a bulletin, a video, weather or a stocking-capacity estimate.

This chat is a demo front end for a few users in one Streamlit process: registrations and agent turns of the same session are serialized with a per-session lock (different sessions never block each other), which does not protect several processes on one database.

The front end wraps the Semente chat by matching its Portuguese UI strings, so it depends on the Semente revision locked in `uv.lock` (`semente-agents` at git commit `df820ff`; install with `uv sync --frozen`). When you update that revision, run `uv run pytest tests/test_streamlit_app.py`: a test fails if any translated label or hidden button no longer exists in the installed Semente web app.

| Name | Country | Latitude | Longitude | Profile |
| --- | --- | --- | --- | --- |
| Goiás | Brazil | -15.5 | -47.5 | Cultivated pasture in the Cerrado, mixed with cropland and shrubland |
| Llanos de Moxos | Bolivia | -14.0 | -65.5 | Seasonally flooded natural savanna grassland of the Beni (Llanos de Moxos) |
| Chiquitania | Bolivia | -15.5 | -60.5 | Cultivated pasture mixed with dry forest in the Chiquitania (Santa Cruz) |
| Chaco | Paraguay | -22.5 | -59.5 | Cultivated pasture of the Paraguayan Chaco cattle belt |
| Llanos Orientales | Colombia | 4.5 | -72.0 | Savanna of the Llanos Orientales with cultivated and natural grassland and gallery forest |
| Cajamarca | Peru | -7.0 | -78.3 | High-altitude natural pasture of the Andes (above 3000 m) |
| Pampa Humeda | Argentina | -36.0 | -60.0 | Mosaic of cropland and natural grassland of the humid Pampas |
| Pampas | Uruguay | -33.0 | -56.0 | Natural temperate grassland (Pampas) used for grazing |
| Aysén | Chile | -45.6 | -72.1 | Cultivated pasture and grass steppe of the Aysén valleys (Chilean Patagonia) |
| Llanos de Apure | Venezuela | 7.5 | -69.0 | Flat natural savanna grassland of the Venezuelan Llanos |
| Kansas | United States | 38.5 | -99.0 | Cropland and rangeland mosaic of the Great Plains |
| Kenya | Kenya | -2.0 | 37.0 | Savanna rangeland with shrubs and grasses |
| Queensland | Australia | -27.5 | 150.0 | Improved pasture and woodland in the Australian savanna belt |
| Lima | Peru | -12.05 | -77.04 | Negative control: dense urban area |
| Atacama | Chile | -23.5 | -69.0 | Negative control: hyper-arid desert |

## Configuration

### Google Earth Engine

The pasture map and area tools need an Earth Engine project and a service account (see `.env.example`):

- `GEE_PROJECT` — Google Cloud project registered for Earth Engine.
- `GEE_SERVICE_ACCOUNT` — service account e-mail.
- `GEE_KEY_FILE` — path to the service account JSON key (keep it out of version control).

Earth Engine is initialized lazily on the first pasture request, never at import. Without these variables the tools answer that the satellite data service is unavailable.

### Test properties and end-to-end tests

`domain/data/test_properties.json` (also behind the sidebar picker; used by the pasture, biomass, weather, bulletin and video e2e tests; loader `tests/fixtures/global_properties.py`) fixes 15 properties with emphasis on South America and a few global references: cultivated pasture (Goiás in Brazil, the Bolivian Chiquitania, the Paraguayan Chaco, the Colombian Llanos, Chilean Aysén, Queensland), natural grassland and savanna (Bolivian Llanos de Moxos, Venezuelan Llanos, Uruguayan Pampas, Kenya), high-altitude pasture (Peruvian Andes), mosaics (Argentine humid Pampas, Kansas) and two negative controls (Lima, the Atacama desert). The e2e test checks that each point really has its described profile against independent assets (GPW v1, ESA WorldCover, SRTM elevation).

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

### Language of the assistant

The assistant answers in the language the user writes in (English by default), with a decimal point and ISO dates; every prompt and tool text is English. The framework renders the user persona in Portuguese, which made the assistant answer in Portuguese, so `domain/agent.py` renders the persona itself in English (`persona_template` in `agents.yml`) and every state ends with an explicit language rule. `tests/test_language.py` checks that the prompt files contain no Portuguese.

## Acknowledgments

Pasto10 is developed by [LAPIG/UFG](https://lapig.iesa.ufg.br/) in the context of the Global Pasture Watch and Time2Graze initiatives. We thank the partners of both initiatives for producing and maintaining the map products that make this assistant possible.

## License

This project is licensed under the [GPL-3.0-or-later](LICENSE). As a Semente derivative, it must remain open.