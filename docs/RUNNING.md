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
Open <http://localhost:8501>.

With Docker instead: `docker compose up` (same entrypoint on port 8501; give it the same variables).

To stop it, press `Ctrl+C` in the terminal, or from another one:

```
pkill -f "streamlit run app/streamlit_app.py"
```

Health check: `curl http://localhost:8501/_stcore/health` answers `ok`.

## 6. Try it in the browser

1. Choose **anonymous** sign-in.
2. In the sidebar, **Test properties**: pick one of the 15 and click **Use this property**. It registers a
   synthetic demo parcel (named `... - DEMO-XXX-0001`) and accepts the terms, so you can ask right away.
   The picker only exists with `APP_ENV=development`, which `run_local.sh` already sets.
3. Ask in English, for example:
   - `Show me the pasture map and the total pasture area.`
   - `Show me the dry-matter biomass map and the total biomass.`
   - `Generate the PDF bulletin.`
   - `Make the biomass time-series video.` (needs `ffmpeg`)
   - `What is the rain forecast for the next weeks?`
   - `How many animal units can this area support?`
4. To use your own area, type `Register my farm at latitude -33.0, longitude -56.0 with a 1000 m radius`,
   or attach a `.geojson` polygon (valid JSON, up to 1 MB).

What to expect: each answer shows the map or figures and says the year and the source. Maps after 2022 are
experimental estimates and the assistant warns about it; the two negative controls (Lima, Atacama) should
return little or no pasture. The synthetic demo registry is labeled **DEMO** everywhere.

First requests are slow (Earth Engine, up to about a minute); repeated ones are served from cache.

## 7. Run the automated tests

Offline tests (no keys, no network, a few seconds):

```
uv run pytest
```

End-to-end tests (real Earth Engine, Open-Meteo and network; need the `GEE_*` variables exported):

```
set -a; source .env; set +a
uv run pytest -m e2e -s                                 # everything (long)
uv run pytest -m e2e -s tests/e2e/test_pasture_e2e.py   # one area at a time
uv run pytest -m e2e -s tests/e2e/test_bulletin_e2e.py
PASTO10_E2E_OUT=/some/dir uv run pytest -m e2e -s       # also keep maps and PDFs
```

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Env file not found` | Create `.env` (section 4) or pass the path as the first argument |
| The assistant says the satellite data service is unavailable | `GEE_*` missing or wrong; check the key path, the service account registration and that the Earth Engine API is enabled |
| `Earth Engine` permission or quota error | The service account is not registered for Earth Engine, or the project is not registered |
| Port already in use | Another instance is running: stop it (section 5) or use `PORT=8600` |
| No "Test properties" box | `APP_ENV=development` is not set; use `scripts/run_local.sh` |
| Video fails | Install `ffmpeg` |
| `ImportError` when Streamlit starts | Use `uv sync --frozen`; Streamlit is pinned below 1.53 |
| Answers in the wrong language or model errors | Check `GOOGLE_API_KEY` |
| A request takes very long | First Earth Engine call or a slow OpenStreetMap lookup (capped at 12 s); retry |
