# StormTracker ERA5 Web App

Upload ERA5 NetCDF files, step through time to view pressure and wind, click on the map to digitize the storm track (like `master1.m` / `correct_storm_tracks.py`), and update the persisted track for each storm.

## Storm metadata and data files

Storm metadata is read from `StormTracker/data/storms.json`.

Water-level time series are read from `StormTracker/data/water_level_series.json` as a full series source, then filtered per selected storm in the API response.

Helper script:

```bash
python StormTracker/scripts/bootstrap_storms_metadata.py
```

This script bootstraps both files from the existing analysis outputs.

## Run locally

```bash
cd StormTracker/webapp
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — upload a `.nc` file (ERA5 format: `msl`, `u10`, `v10`, `valid_time`, `latitude`, `longitude`), use Prev/Next to step through time, click to add track points (or switch to "Delete point" and click to remove), then click `Update Storm Track` to persist changes.

## Configuration (share-ready defaults)

The app now runs from any clone location by default using `StormTracker/data`.
You can override storage locations with environment variables:

- `STORMTRACKER_DATA_DIR` (defaults to `StormTracker/data`)
- `STORMTRACKER_CODEC_GTSM_DIRS` (path-separated list of GTSM roots to scan)

Example:

```bash
export STORMTRACKER_DATA_DIR="/path/to/shared/data"
export STORMTRACKER_CODEC_GTSM_DIRS="/mnt/gtsmA:/mnt/gtsmB"
```

## Deploy on Railway

1. Push this repo to GitHub (or connect your repo in Railway).
2. In [Railway](https://railway.app), New Project → Deploy from GitHub repo → select the repo.
3. Set **Root Directory** to `StormTracker/webapp` (so `Dockerfile` and `main.py` are at the service root).
4. Railway will build the Dockerfile (installs Cartopy + GEOS/PROJ) and run `uvicorn`. It assigns a public URL and injects `PORT` at runtime.

Optional: add `railway.toml` in the same directory (included) to pin the start command. No extra env vars required for basic deployment.

## API

- `GET /api/storms` — Storm catalog loaded from `data/storms.json`.
- `GET /api/storms/<storm_id>/water_level_series` — Full water-level series for selected storm from `data/water_level_series.json` with `series`, `full_series_start_utc`, `full_series_end_utc`, `default_start_utc`, `default_end_utc`, and `storm_windows`.
- `POST /api/storm/start-session` — JSON `{ storm_id, start_utc, end_utc }` → reuse local ERA5 if present, otherwise download to `data/storm_<storm_id>/era5/ERA5_<start>_<end>.nc`, then return `{ session_id, times, track, gtsm_available, bounds }` where `track` auto-loads from `data/storm_<storm_id>/storm_track/track_<start>_<end>.json` when available.
- `POST /api/upload` — multipart `.nc` file → `{ session_id, times, bounds }`
- `GET /api/frame/<session_id>/<time_index>` — PNG image for that time step
- `GET /api/gtsm/frame/<session_id>/<time_index>` — GTSM frame PNG for the same timestamp (cached under `data/storm_<storm_id>/gtsm/`)
- `POST /api/track/add` — JSON `{ session_id, time_index, lon, lat }` → append point (pressure interpolated)
- `POST /api/track/delete` — JSON `{ session_id, time_index }` (or `time_index: -1` to remove last)
- `GET /api/track/<session_id>` — current track as JSON
- `POST /api/track/update/<session_id>` — persist track to `data/storm_<storm_id>/storm_track/track_<start>_<end>.json` (storm window key) and persist derived `storm_window` (min/max labelled frame times) into `data/storms.json`

Sessions are in-memory for live editing, but storm tracks are persisted per storm under `data/storm_<storm_id>/storm_track` when updated.

## Migrate existing data layout

If your data still uses legacy folders (`data/era5`, `data/gtsm`, `data/storm_tracks`), migrate once:

```bash
python StormTracker/scripts/migrate_storm_data_layout.py         # dry-run
python StormTracker/scripts/migrate_storm_data_layout.py --apply # perform move
```
