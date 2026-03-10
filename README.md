# StormTracker ERA5 Web App

Upload ERA5 NetCDF files, step through time to view pressure and wind, click on the map to digitize the storm track (like `master1.m` / `correct_storm_tracks.py`), and download the track as CSV.

## Storm catalog and ERA5 window

When the app is run from the FLOOD-CDT repo (with `Objective1/3a_ANALYSIS_Python/output/mast1.pkl` and water-level analysis available):

1. **Choose storm** — Select a storm from the dropdown (catalog comes from `mast1.pkl`).
2. **Water level and surge** — A scrollable time-series chart shows the full water level and surge for the storm. Storm closure windows are drawn as shaded regions. A **single dual-handle slider** (start and end) lets you narrow the ERA5 download window; the chart view zooms to the selected range and the displayed start/end times (UTC) are sent to the CDS ERA5 request when you click "Download ERA5 & start labeling".
3. **Download ERA5 & start labeling** — Starts a session for the selected storm and time window (or upload an existing `.nc` file instead).

The ERA5 window is set by the dual-handle slider; the chart viewport and the request to `POST /api/storm/start-session` both use the selected `start_utc` and `end_utc`.

## Run locally

```bash
cd StormTracker/webapp
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — upload a `.nc` file (ERA5 format: `msl`, `u10`, `v10`, `valid_time`, `latitude`, `longitude`), use Prev/Next to step through time, click to add track points (or switch to "Delete point" and click to remove), then Download CSV.

## Deploy on Railway

1. Push this repo to GitHub (or connect your repo in Railway).
2. In [Railway](https://railway.app), New Project → Deploy from GitHub repo → select the repo.
3. Set **Root Directory** to `StormTracker/webapp` (so `Dockerfile` and `main.py` are at the service root).
4. Railway will build the Dockerfile (installs Cartopy + GEOS/PROJ) and run `uvicorn`. It assigns a public URL and injects `PORT` at runtime.

Optional: add `railway.toml` in the same directory (included) to pin the start command. No extra env vars required for basic deployment.

## API

- `GET /api/storms` — Storm catalog (from mast1.pkl) with default ERA5 windows and `has_water_level_series`.
- `GET /api/storms/<storm_id>/water_level_series` — Full water-level and surge time series (hourly) for the storm; includes `series`, `full_series_start_utc`, `full_series_end_utc`, `default_start_utc`, `default_end_utc`, and `storm_windows` (closure ranges in UTC for chart shading).
- `GET /api/storms/<storm_id>/water_level` — Pre-generated water-level plot PNG (if present).
- `POST /api/storm/start-session` — JSON `{ storm_id, start_utc, end_utc }` → download (or reuse) ERA5 NetCDF and return `{ session_id, times, bounds }`.
- `POST /api/upload` — multipart `.nc` file → `{ session_id, times, bounds }`
- `GET /api/frame/<session_id>/<time_index>` — PNG image for that time step
- `POST /api/track/add` — JSON `{ session_id, time_index, lon, lat }` → append point (pressure interpolated)
- `POST /api/track/delete` — JSON `{ session_id, time_index }` (or `time_index: -1` to remove last)
- `GET /api/track/<session_id>` — current track as JSON
- `GET /api/csv/<session_id>` — CSV attachment: `storm_id`, `time_utc`, `lon`, `lat`, `pressure_hpa`

Sessions are in-memory; no persistence. Use a single upload per session and download CSV when done.
