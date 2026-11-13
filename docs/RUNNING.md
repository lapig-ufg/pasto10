# Running and testing Pasto10

Step by step to run the Pasto10 chat in the browser and to test it. For what the
tools do and how the area is chosen, see the [README](../README.md).

## 1. What you need

| Requirement | Why | How to check |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | Installs Python 3.14 and the dependencies | `uv --version` |
| Git access to `github.com/lapig-ufg/semente` | `semente-agents` is installed from Git | `uv sync` finishes without errors |
| `ffmpeg` | Biomass time-series video (the other tools work without it) | `ffmpeg -version` |
| Google AI API key (Gemini) | The assistant's language model | a key from <https://aistudio.google.com/apikey> |
| Google Earth Engine project and service account | Pasture and biomass data | see section 3 |
| Internet access | Earth Engine, Open-Meteo, basemap tiles, OpenStreetMap | |

Python 3.14 is installed by `uv` itself (`.python-version`); you do not need it beforehand.
`ffmpeg`: `brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Debian/Ubuntu.

## 2. Install

```
git clone <this repository> && cd pasto10
git checkout dev
uv sync --frozen
```

## 3. Google Earth Engine credentials

The pasture, biomass and video tools read Earth Engine. Weather (Open-Meteo), the
animal unit calculator and the PDF layout do not need it.

1. Create (or reuse) a Google Cloud project and register it for Earth Engine at
   <https://code.earthengine.google.com/register> (non-commercial use is free).
2. In that project enable the **Earth Engine API**.
3. Create a **service account** (IAM and admin, Service accounts) and generate a
   **JSON key** for it. Keep the file outside version control.
4. Register the service account for Earth Engine: in the Earth Engine registration
   page of the project, or by granting it the role *Earth Engine Resource Viewer* (or Writer).
5. The public assets used (`projects/global-pasture-watch/...`, `GOOGLE/SATELLITE_EMBEDDING/...`,
   Time2Graze) are readable by any registered project; no extra sharing is needed.

You will need three values: the project id, the service account e-mail and the path to the key.

## 4. Configure

```
cp .env.example .env
```

Edit `.env` and fill only these variables (the rest can stay as it is for a local run):

```
GOOGLE_API_KEY="your-gemini-key"
GEE_PROJECT="your-project-id"
GEE_SERVICE_ACCOUNT="your-service-account@your-project.iam.gserviceaccount.com"
GEE_KEY_FILE="/absolute/path/to/key.json"
```

`GEE_KEY_FILE` should be an absolute path (a relative one is resolved from the folder of the env file).
`scripts/run_local.sh` reads **only** `GOOGLE_API_KEY` and `GEE_*` from the file; databases, S3 and
WhatsApp settings are never loaded, so it can safely point at an env file shared with another project.

## 5. Run the chat

```
scripts/run_local.sh            # uses ./.env
scripts/run_local.sh path/to/other.env
PORT=8600 scripts/run_local.sh  # another port
```

The script starts `streamlit run app/streamlit_app.py` with SQLite (`users_db.json` and the local
session store; nothing external), `APP_ENV=development` and the manifest `semente.yaml`.
The default port is 8501; set `PORT` to use another one (the examples below assume 8501, so replace
it with your `PORT`). Open <http://localhost:8501>.

To stop it, press `Ctrl+C` in the terminal that runs it. If it was left running elsewhere, first check which
process listens on the port and make sure it is Pasto10's Streamlit:

```
lsof -nP -iTCP:8501 -sTCP:LISTEN   # shows the PID and the command
kill <PID>                          # only after confirming it is the Pasto10 process
```

Health check: `curl http://localhost:8501/_stcore/health` answers `ok`.

### With Docker

`docker compose up --build` builds the `Dockerfile` and starts the app. What each file does:

- `Dockerfile`: Python 3.14 image with `uv`, `git` (to fetch `semente-agents`) and `ffmpeg`; it installs the
  locked dependencies (`uv sync --frozen`), copies the code and starts
  `streamlit run app/streamlit_app.py` on port 8501. The image has no credentials: `.dockerignore` leaves out
  `.env`, key files, `tmp` and `logs`, so the variables and the Earth Engine key must be provided when it runs.
  On its own it does not set `APP_ENV`, so the "Test properties" box is hidden (the production behaviour).
- `compose.yaml`: mounts the repository over `/app` (code changes show up without rebuilding), runs
  `uv run streamlit ...` (which also checks the environment at start, so it needs network), sets
  `APP_ENV=development` (the test picker is on) and publishes host port **8501** (to use another host port,
  edit the `ports` entry, for example `"8600:8501"`; `PORT` only applies to `scripts/run_local.sh`).
  It requires a `.env` file and loads **all** of it into the container (unlike `scripts/run_local.sh`, which
  reads only `GOOGLE_API_KEY` and `GEE_*`), so keep database, S3 and WhatsApp variables out of that file.
  `GEE_KEY_FILE` must be a path that exists **inside** the container (the key is not copied into the image):
  place the key file in a folder mounted into `/app` or add a volume for it.
- Neither `Dockerfile` nor `compose.yaml` sets `DATABASE_TYPE`; it comes from the `.env` loaded by compose
  (`.env.example` has `sqlite`) and, if absent, Semente defaults to SQLite, inside the container.
- The manifest is found from the working directory (`/app`, where `semente.yaml` lives) because
  `SEMENTE_MANIFEST` is not set (`scripts/run_local.sh` sets it explicitly).

## 6. Try it in the browser

1. Choose **anonymous** sign-in.
2. In the sidebar, **Test properties**: pick one of the 15 and click **Use this property**. It registers a
   circular area (buffer) with the radius of the property and accepts the terms, so you can ask right away.
   The picker only exists with `APP_ENV=development`, which `run_local.sh` already sets.
3. Ask in English, for example:
   - `Show me the pasture map and the total pasture area.`
   - `Show me the dry-matter biomass map and the total biomass.`
   - `Generate the PDF bulletin.`
   - `Make the biomass time-series video.` (needs `ffmpeg`)
   - `What is the rain forecast for the next weeks?`
   - `How many animal units can this area support?`
4. To use your own area, type `Register my farm at latitude -33.0, longitude -56.0 with a 1000 m radius`
   (radius from 50 to 1000 m, default 500 m), confirm the circle and give it a name. You can also paste a
   Google Maps location link instead of the coordinate (in Google Maps: press and hold the spot, Share, copy the link),
   for example `Register https://www.google.com/maps/place/Pampa/@-33.0,-56.0,14z with a 300 m radius`; short and long links work, only
   `https` Google hosts are opened (at most 5 redirects, short timeouts, no body downloaded) and a link that
   only names a place, without coordinate, is refused. Circular areas around a coordinate are the only way to
   register an area; the link is just another way to give the center.

What to expect: each answer shows the map or figures and says the year and the source. Maps after 2022 are
experimental estimates and the assistant warns about it; the two negative controls (Lima, Atacama) should
return little or no pasture. Registered areas are approximate circles, not property boundaries.

First requests are slow (Earth Engine, up to about a minute); repeated ones are served from cache.

## 7. Run the automated tests

Offline tests (no keys, no network, a few seconds):

```
uv run pytest
```

End-to-end tests (real Earth Engine, Open-Meteo and network; need the `GEE_*` variables exported):

```
set -a; source .env; set +a
uv run pytest -m e2e -s                                 # everything (139 tests in 8 files, long)
uv run pytest -m e2e -s tests/e2e/test_pasture_e2e.py   # one file at a time
PASTO10_E2E_OUT=/some/dir uv run pytest -m e2e -s       # also keep maps and PDFs
uv run pytest --collect-only -m e2e -q                  # list every e2e test
```

The files in `tests/e2e/` (all but the weather one need the `GEE_*` variables; the video one also needs `ffmpeg`):

| File | What it checks |
| --- | --- |
| `test_pasture_e2e.py` | Pasture tools: areas by year, Global Pasture Watch and the Random Forest classification |
| `test_biomass_e2e.py` | Biomass tools on the 15 test properties (annual years and Time2Graze months) |
| `test_biomass_video_e2e.py` | The biomass time-series video of each property (needs `ffmpeg`) |
| `test_weather_e2e.py` | The five weather tools on the 15 properties (Open-Meteo only, no Earth Engine) |
| `test_bulletin_e2e.py` | The PDF bulletin of each property |
| `test_fallback_e2e.py` | Requests outside the available coverage return the nearest data and say so |
| `test_ua_skill_e2e.py` | Arithmetic of the animal-unit skill on real biomass figures |
| `test_generalist_e2e.py` | Whole chat flow over several continents, area consistency and concurrency |

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Env file not found` | Create `.env` (section 4) or pass the path as the first argument |
| The assistant says the satellite data service is unavailable | `GEE_*` missing or wrong; check the key path, the service account registration and that the Earth Engine API is enabled |
| `Earth Engine` permission or quota error | The service account is not registered for Earth Engine, or the project is not registered |
| Port already in use | Another instance is running: stop it (section 5) or use `PORT=8600` (with Docker, change the `ports` entry of `compose.yaml`) |
| `[prompts]` lines in the log at start | Informational, from Semente's prompt loader. Pasto10 customises only the `description` of Semente's generic tools, so it logs "`results` missing - falling back to English default" for them (the English default is the text used) and "not in the English default (domain-specific addition; included as-is)" for the tools and agent keys that Pasto10 adds. Nothing to fix |
| No "Test properties" box | `APP_ENV=development` is not set; use `scripts/run_local.sh` |
| Video fails | Install `ffmpeg` |
| `ImportError` when Streamlit starts | Use `uv sync --frozen`; Streamlit is pinned below 1.53 |
| Answers in the wrong language or model errors | Check `GOOGLE_API_KEY` |
| A request takes very long | First Earth Engine call or a slow basemap tile download (3 s connect and 4 s read per tile; it falls back to a plain background); retry |
