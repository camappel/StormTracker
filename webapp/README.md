# StormTracker ERA5 Web App

Upload ERA5 NetCDF files, step through time to view pressure and wind, click on the map to digitize the storm track (like `master1.m` / `correct_storm_tracks.py`), and download the track as CSV.

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

- `POST /api/upload` — multipart `.nc` file → `{ session_id, times, bounds }`
- `GET /api/frame/<session_id>/<time_index>` — PNG image for that time step
- `POST /api/track/add` — JSON `{ session_id, time_index, lon, lat }` → append point (pressure interpolated)
- `POST /api/track/delete` — JSON `{ session_id, time_index }` (or `time_index: -1` to remove last)
- `GET /api/track/<session_id>` — current track as JSON
- `GET /api/csv/<session_id>` — CSV attachment: `storm_id`, `time_utc`, `lon`, `lat`, `pressure_hpa`

Sessions are in-memory; no persistence. Use a single upload per session and download CSV when done.
