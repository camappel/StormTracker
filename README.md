# StormTracker ERA5 Web App

Upload ERA5 NetCDF files, step through time to view pressure and wind, click on the map to digitize the storm track (like `master1.m` / `correct_storm_tracks.py`), and update the persisted track for each storm.

## Storm metadata and data files

Canonical storm metadata now lives in `StormTracker/data/storms.json` (master source of truth).
Each closure entry in the master file includes a `barrier` field (`eastern_scheldt` or `thames`).

For current app compatibility, `StormTracker/data/eastern_scheldt/storms.json` is generated from the master file.

Water-level time series are read from `StormTracker/data/<dataset>/water_level_series.json` as a flat full-gauge series (no storm IDs). The API slices this series per selected storm using closure bounds +/- 62 hours.

Supported datasets in the UI/API are:

- `eastern_scheldt` (default)
- `thames_barrier`

Helper scripts:

```bash
python StormTracker/scripts/bootstrap_storms_metadata.py
python StormTracker/scripts/merge_master_storms.py --write
```

- `bootstrap_storms_metadata.py` bootstraps Eastern Scheldt metadata and water-level series from analysis outputs.
- `merge_master_storms.py --write` builds `data/storms.json` from Eastern Scheldt + Thames storm lists and refreshes the generated Eastern Scheldt compatibility file.

## Run locally

```bash
cd StormTracker/webapp
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — upload a `.nc` file (ERA5 format: `msl`, `u10`, `v10`, `valid_time`, `latitude`, `longitude`), use Prev/Next to step through time, click to add track points (or switch to "Delete point" and click to remove), then click `Update Storm Track` to persist changes.

Startup order in the UI:

1. Load the generated compatibility `storms.json` for the selected dataset.
2. Default the storm selector to storm `1` (fallback to first available).
3. Load and render only that storm's sliced water-level window.

## Configuration (share-ready defaults)

The app now runs from any clone location by default using `StormTracker/data`.
Dataset-specific files (metadata/water levels) are read from `StormTracker/data/<dataset>/...`, while shared ERA5/GTSM/track cache assets are stored directly under `StormTracker/data`.
You can override storage locations with environment variables:

- `STORMTRACKER_DATA_DIR` (defaults to `StormTracker/data`)
- `STORMTRACKER_DEFAULT_DATASET` (defaults to `eastern_scheldt`)
- `STORMTRACKER_CODEC_GTSM_DIRS` (path-separated list of GTSM roots to scan)

Example:

```bash
export STORMTRACKER_DATA_DIR="/path/to/shared/data"
export STORMTRACKER_DEFAULT_DATASET="eastern_scheldt"
export STORMTRACKER_CODEC_GTSM_DIRS="/mnt/gtsmA:/mnt/gtsmB"
```

## Deploy on Railway

1. Push this repo to GitHub (or connect your repo in Railway).
2. In [Railway](https://railway.app), New Project → Deploy from GitHub repo → select the repo.
3. Set **Root Directory** to `StormTracker/webapp` (so `Dockerfile` and `main.py` are at the service root).
4. Railway will build the Dockerfile (installs Cartopy + GEOS/PROJ) and run `uvicorn`. It assigns a public URL and injects `PORT` at runtime.

Optional: add `railway.toml` in the same directory (included) to pin the start command. No extra env vars required for basic deployment.

## API

- `GET /api/storms?dataset=<dataset>` — Storm catalog loaded from `data/<dataset>/storms.json` (compatibility file generated from master metadata).
- `GET /api/storms/<storm_id>/water_level_series?dataset=<dataset>` — Water-level series for selected storm, sliced server-side from flat `data/<dataset>/water_level_series.json` using closure bounds +/- 62 hours; returns `series`, `full_series_start_utc`, `full_series_end_utc`, `default_start_utc`, `default_end_utc`, and `storm_windows`.
- `POST /api/storm/start-session` — JSON `{ storm_id, start_utc, end_utc, dataset }` → reuse shared ERA5 cache if present, otherwise download to `data/era5/ERA5_<start>_<end>.nc`, then return `{ session_id, times, track, gtsm_available, bounds }` where `track` auto-loads from `data/storm_track/track_<start>_<end>.json` (window-keyed, barrier-agnostic).
- `POST /api/upload` — multipart `.nc` file → `{ session_id, times, bounds }`
- `GET /api/frame/<session_id>/<time_index>` — PNG image for that time step
- `GET /api/gtsm/frame/<session_id>/<time_index>` — GTSM frame PNG for the same timestamp (subset/cache shared under `data/gtsm/`)
- `POST /api/track/add` — JSON `{ session_id, time_index, lon, lat }` → append point (pressure interpolated)
- `POST /api/track/delete` — JSON `{ session_id, time_index }` (or `time_index: -1` to remove last)
- `GET /api/track/<session_id>` — current track as JSON
- `POST /api/track/update/<session_id>` — persist track to `data/storm_track/track_<start>_<end>.json` (ERA5 window key) and persist derived `storm_window` (min/max labelled frame times) into `data/<dataset>/storms.json`

Sessions are in-memory for live editing, but storm tracks are persisted once per ERA5 window under `data/storm_track`.

## Migrate existing data layout

On startup, the app automatically migrates legacy per-storm assets into shared folders when safe:

- `data/*/storm_*/era5/ERA5_*.nc` -> `data/era5/`
- `data/*/storm_*/gtsm/**` -> `data/gtsm/**`
- `data/*/storm_*/storm_track/track_*.json` -> `data/storm_track/`

If a shared destination already exists with different content, startup raises a conflict error so data is not silently overwritten.

The current shared layout does not require a separate migration command; startup migration handles supported legacy paths automatically.
