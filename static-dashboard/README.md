# StormTracker Static Dashboard

This folder contains a static storm browser for the existing StormTracker catalogue. It does not start ERA5 or GTSM sessions, download data, or edit tracks.

Open `index.html` directly in a browser, or serve this directory with any static file server.

The hydrograph uses vendored Chart.js browser files in `vendor/`, so it works without CDN access once the folder is hosted.

## GTSM gauge-series comparison

The GTSM comparison chart uses nearest-station surge series exported from cached GTSM NetCDF subsets:

```bash
cd StormTracker/static-dashboard
../webapp/.venv/bin/python export_gtsm_series.py --all
node build_static_data.js
```

The exporter only uses cached files in `StormTracker/data/gtsm`; it does not download missing GTSM data.

## Cached frame scrubbing

The dashboard can scrub image files when static frame folders exist under `frames/storm_<id>/`.

Export cached frames from the existing NetCDF cache:

```bash
cd StormTracker/static-dashboard
../webapp/.venv/bin/python export_static_frames.py --storm 1 --products era5,wind,gtsm --format webp
```

Use `--all` instead of `--storm 1` to export every catalogued storm. The exporter only uses cached files in `StormTracker/data/era5` and `StormTracker/data/gtsm`; it does not download missing ERA5 or GTSM data.

Rebuild the static data pack after changing `StormTracker/data/storms.json`, `StormTracker/data/storm_track/*.json`, or the water-level JSON files:

```bash
cd StormTracker/static-dashboard
node build_static_data.js
```

Rebuild it after exporting frames too, because it reads each product's `manifest.json` and wires those frame sequences into `data/storm-dashboard-data.js`.
