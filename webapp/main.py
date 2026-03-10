"""
StormTracker ERA5 Web App — FastAPI backend.
Upload ERA5 NetCDF, step through time, draw storm track by clicking, download CSV.
"""
import io
import os
import tempfile
import time
import uuid
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
import pandas as pd
import xarray as xr
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from scipy.interpolate import RegularGridInterpolator
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Display bounds (same as correct_storm_tracks.py)
XB_MET = [-30, 15]
YB_MET = [40, 70]
INT_SKIP = 3
VMIN, VMAX = 967, 1020

# Session TTL (seconds); optional cleanup not implemented in MVP
SESSION_TTL = 3600

# Paths to analysis outputs (mast1.pkl, water level plots, ERA5 files)
ROOT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = ROOT_DIR / "Objective1" / "3a_ANALYSIS_Python"
ANALYSIS_OUTPUT_DIR = ANALYSIS_DIR / "output"
MAST1_PATH = ANALYSIS_OUTPUT_DIR / "mast1.pkl"
WATER_LEVEL_DIR = ANALYSIS_OUTPUT_DIR / "water_level_plots"
ERA5_DIR = ROOT_DIR / "2_DATA" / "4_ERA5_API"

COMBINED_TRACKS_PATH = ANALYSIS_OUTPUT_DIR / "storm_tracks_labeled_web.csv"

app = FastAPI(title="StormTracker ERA5 Web App")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# session_id -> { path, storm_id, LON, LAT, P_MET, U_MET, V_MET, ts_valid, frame_list, track, last_access }
sessions: dict = {}
# Temp directory for uploads
UPLOAD_DIR = Path(tempfile.gettempdir()) / "stormtracker_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _load_mast1():
    """Load storm catalog (storm_df) from mast1.pkl."""
    if not MAST1_PATH.exists():
        raise HTTPException(status_code=500, detail=f"mast1.pkl not found at {MAST1_PATH}")
    data = pd.read_pickle(MAST1_PATH)
    if "storm_df" not in data:
        raise HTTPException(status_code=500, detail="mast1.pkl missing 'storm_df'")
    return data["storm_df"]


def _load_mast1_full():
    """Load full mast1.pkl (storm_df + TSP, WLP, SUP, TIP) for water-level series."""
    if not MAST1_PATH.exists():
        raise HTTPException(status_code=500, detail=f"mast1.pkl not found at {MAST1_PATH}")
    data = pd.read_pickle(MAST1_PATH)
    for key in ("storm_df", "TSP", "WLP", "SUP"):
        if key not in data:
            raise HTTPException(status_code=500, detail=f"mast1.pkl missing {key!r}")
    return data


def _storm_series_window(storm_id: int):
    """Storm start/end (local), default UTC window (24h padding), and closure ranges in UTC."""
    storm_df = _load_mast1()
    group = storm_df[storm_df["Storm"] == storm_id]
    if group.empty:
        return None
    storm_start = group["Start of Closure"].min()
    storm_end = group["End of Closure"].fillna(
        group["Start of Closure"] + pd.Timedelta(days=1)
    ).max()
    start_utc = storm_start.tz_localize("Europe/Amsterdam", ambiguous=False).tz_convert("UTC")
    end_utc = storm_end.tz_localize("Europe/Amsterdam", ambiguous=False).tz_convert("UTC")
    default_start = (start_utc - pd.Timedelta(hours=24)).strftime("%Y-%m-%dT%H:00:00Z")
    default_end = (end_utc + pd.Timedelta(hours=24)).strftime("%Y-%m-%dT%H:00:00Z")
    storm_windows = []
    for _, r in group.iterrows():
        c_start = r["Start of Closure"]
        c_end = r["End of Closure"] if pd.notna(r["End of Closure"]) else r["Start of Closure"] + pd.Timedelta(days=1)
        c_start_utc = c_start.tz_localize("Europe/Amsterdam", ambiguous=False).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        c_end_utc = c_end.tz_localize("Europe/Amsterdam", ambiguous=False).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        storm_windows.append({"start_utc": c_start_utc, "end_utc": c_end_utc})
    return {
        "storm_start": storm_start,
        "storm_end": storm_end,
        "default_start_utc": default_start,
        "default_end_utc": default_end,
        "storm_windows": storm_windows,
    }


def _get_storm_catalog():
    storm_df = _load_mast1()
    storm_ids = sorted(storm_df["Storm"].unique())
    rows = []
    for sid in storm_ids:
        win = _storm_series_window(sid)
        if not win:
            continue
        storm_start = win["storm_start"]
        storm_end = win["storm_end"]
        wl_path = WATER_LEVEL_DIR / f"water_level_storm_{sid}.png"
        has_series = True  # all storms in catalog have mast1 data for series
        rows.append(
            {
                "storm_id": int(sid),
                "label": f"Storm {sid}",
                "storm_start_local": storm_start.strftime("%Y-%m-%d %H:%M"),
                "storm_end_local": storm_end.strftime("%Y-%m-%d %H:%M"),
                "default_start_utc": win["default_start_utc"],
                "default_end_utc": win["default_end_utc"],
                "has_water_level_plot": wl_path.exists(),
                "has_water_level_series": has_series,
                "series_start_utc": win["default_start_utc"],
                "series_end_utc": win["default_end_utc"],
            }
        )
    return rows


@app.get("/api/storms")
async def api_storms():
    """
    Return storm catalog with suggested ERA5 download windows and
    whether a pre-generated water level plot exists.
    """
    return {"storms": _get_storm_catalog()}


@app.get("/api/storms/{storm_id}/water_level")
async def api_storm_water_level(storm_id: int):
    """
    Serve the pre-generated water level plot PNG for a storm.
    """
    path = WATER_LEVEL_DIR / f"water_level_storm_{storm_id}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Water level plot not found for this storm")
    return FileResponse(path, media_type="image/png")


def _to_utc_iso(ts) -> str:
    """Convert a timestamp (naive Europe/Amsterdam) to UTC ISO string."""
    t = pd.Timestamp(ts)
    if t.tz is None:
        t = t.tz_localize("Europe/Amsterdam", ambiguous="NaT").tz_convert("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/api/storms/{storm_id}/water_level_series")
async def api_storm_water_level_series(
    storm_id: int,
    hours_before: int = 168,
    hours_after: int = 168,
):
    """
    Return water level and surge time series from mast1.pkl.
    Series is trimmed to storm_start - hours_before through storm_end + hours_after.
    Default is 7 days each side for slider boundaries.
    default_start_utc / default_end_utc use 62h each side for initial handle positions.
    """
    win = _storm_series_window(storm_id)
    if not win:
        raise HTTPException(status_code=404, detail="Storm not found")
    hours_before = max(0, min(168, hours_before))
    hours_after = max(0, min(168, hours_after))
    tmin = win["storm_start"] - pd.Timedelta(hours=int(hours_before))
    tmax = win["storm_end"] + pd.Timedelta(hours=int(hours_after))
    data = _load_mast1_full()
    TSP = pd.to_datetime(np.asarray(data["TSP"]))
    WLP = np.asarray(data["WLP"], dtype=float)
    SUP = np.asarray(data["SUP"], dtype=float)
    TIP = np.asarray(data["TIP"], dtype=float) if "TIP" in data else np.full_like(WLP, np.nan)
    tmin_ = pd.Timestamp(tmin)
    tmax_ = pd.Timestamp(tmax)
    mask = (TSP >= tmin_) & (TSP <= tmax_)
    if not np.any(mask):
        return {
            "series": [],
            "full_series_start_utc": win["default_start_utc"],
            "full_series_end_utc": win["default_end_utc"],
            "default_start_utc": win["default_start_utc"],
            "default_end_utc": win["default_end_utc"],
            "storm_windows": win.get("storm_windows", []),
        }
    tsp = TSP[mask]
    wlp = WLP[mask]
    sup = SUP[mask]
    tip = TIP[mask]
    hourly = pd.date_range(
        start=pd.Timestamp(tmin).floor("h"),
        end=pd.Timestamp(tmax).ceil("h"),
        freq="h",
    )
    series = []
    for h in hourly:
        ht = pd.Timestamp(h)
        if ht.tz is not None:
            ht = ht.tz_convert("Europe/Amsterdam").tz_localize(None)
        diff = np.abs(tsp - ht)
        idx = np.argmin(diff)
        time_utc = _to_utc_iso(tsp[idx])
        wl = float(wlp[idx]) if not np.isnan(wlp[idx]) else None
        sg = float(sup[idx]) if not np.isnan(sup[idx]) else None
        td = float(tip[idx]) if not np.isnan(tip[idx]) else None
        series.append({
            "time_utc": time_utc,
            "water_level": wl,
            "surge": sg,
            "tide": td,
        })
    full_start = series[0]["time_utc"] if series else win["default_start_utc"]
    full_end = series[-1]["time_utc"] if series else win["default_end_utc"]
    return {
        "series": series,
        "full_series_start_utc": full_start,
        "full_series_end_utc": full_end,
        "default_start_utc": win["default_start_utc"],
        "default_end_utc": win["default_end_utc"],
        "storm_windows": win.get("storm_windows", []),
    }


class TrackAddBody(BaseModel):
    session_id: str
    time_index: int
    lon: float
    lat: float


class TrackDeleteBody(BaseModel):
    session_id: str
    time_index: int = -1


def _time_coord(ds: xr.Dataset):
    if "valid_time" in ds.coords:
        return ds.coords["valid_time"]
    if "valid_time" in ds.variables:
        return ds["valid_time"]
    return ds.coords["time"] if "time" in ds.coords else ds["time"]


def _normalize_dims(P: np.ndarray, U: np.ndarray, V: np.ndarray, dims: tuple) -> tuple:
    """Ensure (time, lat, lon) order."""
    # CDS ERA5: (time, latitude, longitude)
    if len(P.shape) != 3:
        return P, U, V
    d0, d1, d2 = dims[0].lower(), dims[1].lower(), dims[2].lower()
    if "time" in d0 or "valid" in d0:
        if "lat" in d1 and "lon" in d2:
            return P, U, V
        if "lon" in d1 and "lat" in d2:
            return np.transpose(P, (0, 2, 1)), np.transpose(U, (0, 2, 1)), np.transpose(V, (0, 2, 1))
    if "time" in d2 or "valid" in d2:
        if d0 == "latitude" and d1 == "longitude":
            return np.transpose(P, (2, 0, 1)), np.transpose(U, (2, 0, 1)), np.transpose(V, (2, 0, 1))
        if d0 == "longitude" and d1 == "latitude":
            return np.transpose(P, (2, 1, 0)), np.transpose(U, (2, 1, 0)), np.transpose(V, (2, 1, 0))
    return P, U, V


def parse_era5(path: Path) -> dict:
    """Load ERA5 NetCDF and return arrays + metadata. Raises on invalid format."""
    ds = xr.open_dataset(path)
    try:
        lon_var = ds["longitude"] if "longitude" in ds.coords else ds["longitude"]
        lat_var = ds["latitude"] if "latitude" in ds.coords else ds["latitude"]
    except KeyError:
        if "lon" in ds.coords:
            lon_var = ds["lon"]
        else:
            raise HTTPException(status_code=400, detail="ERA5 must have longitude/latitude (or lon/lat) coordinates")
        if "lat" in ds.coords:
            lat_var = ds["lat"]
        else:
            raise HTTPException(status_code=400, detail="ERA5 must have latitude coordinate")
    time_var = _time_coord(ds)
    LON = np.asarray(lon_var.values).ravel()
    LAT = np.asarray(lat_var.values).ravel()
    P = np.asarray(ds["msl"].values)
    U = np.asarray(ds["u10"].values)
    V = np.asarray(ds["v10"].values)
    ts = pd.to_datetime(time_var.values)
    dims = ds["msl"].dims
    ds.close()
    P, U, V = _normalize_dims(P, U, V, dims)
    nt = P.shape[0]
    ts_valid = ts[:nt]
    if nt == 0:
        raise HTTPException(status_code=400, detail="No time steps in ERA5 file")
    # Frame list: all time steps (chronological for UI)
    frame_list = [(i, pd.Timestamp(t)) for i, t in enumerate(ts_valid)]
    lon_min, lon_max = float(LON.min()), float(LON.max())
    lat_min, lat_max = float(LAT.min()), float(LAT.max())
    return {
        "LON": LON,
        "LAT": LAT,
        "P_MET": P,
        "U_MET": U,
        "V_MET": V,
        "ts_valid": ts_valid,
        "frame_list": frame_list,
        "lon_min": lon_min,
        "lon_max": lon_max,
        "lat_min": lat_min,
        "lat_max": lat_max,
    }


def add_map_base(ax, xlim, ylim):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="facecolor will have no effect", module="cartopy")
        ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="white")
        ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor="lightgray", alpha=0.3)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.8, color="white")
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.5, color="white")
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    # Fill the full axes area; prevents top/bottom bands in the rendered PNG.
    ax.set_aspect("auto")
    # Keep reference grid but avoid outer label margins so image pixels map
    # directly to lon/lat bounds for click-to-point accuracy.
    ax.gridlines(draw_labels=False, linewidth=0.5, color="gray", alpha=0.5, linestyle="--")


def render_frame(session: dict, time_index: int, track: list, *, show_track_points: bool = True) -> bytes:
    """Render one time step as PNG: pressure + wind + track overlay.

    When show_track_points is False, only the track line is drawn (no point markers).
    """
    LON = session["LON"]
    LAT = session["LAT"]
    P_MET = session["P_MET"]
    U_MET = session["U_MET"]
    V_MET = session["V_MET"]
    frame_list = session["frame_list"]
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    ti, grid_time = frame_list[time_index]
    lon_in = (LON >= XB_MET[0]) & (LON <= XB_MET[1])
    lat_in = (LAT >= YB_MET[0]) & (LAT <= YB_MET[1])
    lat_inds = np.where(lat_in)[0]
    lon_inds = np.where(lon_in)[0]
    if len(lat_inds) == 0 or len(lon_inds) == 0:
        raise HTTPException(status_code=400, detail="No grid points in display bounds")
    lat_slice = slice(lat_inds[0], lat_inds[-1] + 1)
    lon_slice = slice(lon_inds[0], lon_inds[-1] + 1)
    X_MET, Y_MET = np.meshgrid(LON, LAT)
    X_sub = X_MET[lat_slice, lon_slice]
    Y_sub = Y_MET[lat_slice, lon_slice]
    P_sub = P_MET[ti, lat_slice, lon_slice] / 100.0
    U_vec = U_MET[
        ti,
        lat_inds[0] : lat_inds[-1] + 1 : INT_SKIP,
        lon_inds[0] : lon_inds[-1] + 1 : INT_SKIP,
    ]
    V_vec = V_MET[
        ti,
        lat_inds[0] : lat_inds[-1] + 1 : INT_SKIP,
        lon_inds[0] : lon_inds[-1] + 1 : INT_SKIP,
    ]
    X_vec = X_MET[
        lat_inds[0] : lat_inds[-1] + 1 : INT_SKIP,
        lon_inds[0] : lon_inds[-1] + 1 : INT_SKIP,
    ]
    Y_vec = Y_MET[
        lat_inds[0] : lat_inds[-1] + 1 : INT_SKIP,
        lon_inds[0] : lon_inds[-1] + 1 : INT_SKIP,
    ]
    lon_span = float(XB_MET[1] - XB_MET[0])
    lat_span = float(YB_MET[1] - YB_MET[0])
    map_ratio = lon_span / lat_span if lat_span else 1.5
    fig_height = 8.0
    fig = plt.figure(figsize=(fig_height * map_ratio, fig_height))
    # Fill the full canvas to eliminate outer whitespace.
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0], projection=ccrs.PlateCarree())
    add_map_base(ax, XB_MET, YB_MET)
    ax.pcolormesh(
        X_sub,
        Y_sub,
        P_sub,
        cmap="jet",
        shading="gouraud",
        vmin=VMIN,
        vmax=VMAX,
        transform=ccrs.PlateCarree(),
    )
    ax.quiver(
        X_vec,
        Y_vec,
        U_vec,
        V_vec,
        color="k",
        scale=420,
        width=0.002,
        transform=ccrs.PlateCarree(),
    )
    track_lons = [p["lon"] for p in track]
    track_lats = [p["lat"] for p in track]
    if track_lons and track_lats:
        ax.plot(
            track_lons,
            track_lats,
            color="darkred",
            linewidth=2,
            transform=ccrs.PlateCarree(),
            zorder=10,
        )
        if show_track_points:
            ax.scatter(
                track_lons,
                track_lats,
                c="darkred",
                s=30,
                zorder=11,
                transform=ccrs.PlateCarree(),
            )
    ts_str = pd.Timestamp(grid_time).strftime("%Y-%m-%d %H:%M UTC")
    ax.text(
        0.01,
        0.99,
        f"{ts_str} (step {time_index + 1}/{len(frame_list)})",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=11,
        color="white",
        bbox={"facecolor": "black", "alpha": 0.35, "pad": 3, "edgecolor": "none"},
        zorder=20,
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches=None, pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def interpolate_pressure(session: dict, time_index: int, lon: float, lat: float) -> float:
    """Bilinear interpolation of MSLP (hPa) at (lon, lat) for given time index."""
    LON = session["LON"]
    LAT = session["LAT"]
    P_MET = session["P_MET"]
    frame_list = session["frame_list"]
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    ti, _ = frame_list[time_index]
    p_slice = P_MET[ti, :, :] / 100.0  # Pa -> hPa
    # RegularGridInterpolator expects (x, y) = (lon, lat) with 1D arrays
    interp = RegularGridInterpolator(
        (LAT, LON),
        p_slice,
        bounds_error=False,
        fill_value=np.nan,
    )
    val = float(interp(np.array([[lat, lon]]))[0])
    if np.isnan(val):
        # Clamp to grid bounds for edge clicks
        lon_c = np.clip(lon, LON.min(), LON.max())
        lat_c = np.clip(lat, LAT.min(), LAT.max())
        val = float(interp(np.array([[lat_c, lon_c]]))[0])
    return val


def get_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return sessions[session_id]


def _create_session_from_era5(path: Path, storm_id: int | None = None) -> dict:
    data = parse_era5(path)
    session_id = uuid.uuid4().hex
    sessions[session_id] = {
        "path": path,
        "storm_id": int(storm_id) if storm_id is not None else None,
        **data,
        "track": [],
        "last_access": time.time(),
    }
    frame_list = data["frame_list"]
    times = [
        {"index": i, "time_utc": pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")}
        for i, t in frame_list
    ]
    return {
        "session_id": session_id,
        "times": times,
        "bounds": {
            "lon_min": XB_MET[0],
            "lon_max": XB_MET[1],
            "lat_min": YB_MET[0],
            "lat_max": YB_MET[1],
        },
    }


class StartStormSessionBody(BaseModel):
    storm_id: int
    start_utc: str
    end_utc: str


def _parse_iso_utc(dt_str: str) -> datetime:
    try:
        # Accept with or without trailing 'Z'
        s = dt_str.strip()
        if s.endswith("Z"):
            s = s[:-1]
        return datetime.fromisoformat(s)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {dt_str!r}")


@app.post("/api/storm/start-session")
async def api_start_storm_session(body: StartStormSessionBody):
    """
    Download ERA5 file for the given storm + time window and create a
    StormTracker session bound to that storm_id.

    The ERA5 files are stored under the shared 2_DATA/4_ERA5_API directory so that
    they remain available for the analysis notebook and this web app.
    """
    storm_id = int(body.storm_id)
    t_start = _parse_iso_utc(body.start_utc)
    t_end = _parse_iso_utc(body.end_utc)
    if t_end <= t_start:
        raise HTTPException(status_code=400, detail="end_utc must be after start_utc")

    # Filenames match the convention used in storm.qmd helper
    era5_filename = f"ERA5_{storm_id}_{t_start:%Y%m%d}_{t_end:%Y%m%d}.nc"
    era5_path = ERA5_DIR / era5_filename

    try:
        import cdsapi  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="cdsapi is not installed on the server; cannot download ERA5 automatically.",
        )

    start_date = t_start.date()
    end_date = t_end.date()
    same_day = start_date == end_date
    if same_day:
        hours_in_range = list(range(t_start.hour, t_end.hour + 1))
    else:
        hours_in_range = list(range(24))
    if not hours_in_range:
        raise HTTPException(status_code=400, detail="Invalid time range")

    ERA5_DIR.mkdir(parents=True, exist_ok=True)
    era5_path.unlink(missing_ok=True)
    client = cdsapi.Client()

    if start_date.year == end_date.year and start_date.month == end_date.month:
        days_in_range = list(range(start_date.day, end_date.day + 1))
        request = {
            "product_type": "reanalysis",
            "variable": [
                "mean_sea_level_pressure",
                "10m_u_component_of_wind",
                "10m_v_component_of_wind",
            ],
            "year": [f"{t_start.year:04d}"],
            "month": [f"{t_start.month:02d}"],
            "day": [f"{d:02d}" for d in days_in_range],
            "time": [f"{h:02d}:00" for h in hours_in_range],
            "area": [YB_MET[1], XB_MET[0], YB_MET[0], XB_MET[1]],
            "format": "netcdf",
        }
        client.retrieve("reanalysis-era5-single-levels", request).download(str(era5_path))
    else:
        paths_to_merge = []
        cur = start_date
        while cur <= end_date:
            y, m = cur.year, cur.month
            month_end = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
            d_start = cur.day if (cur.year, cur.month) == (start_date.year, start_date.month) else 1
            d_end = end_date.day if (end_date.year, end_date.month) == (y, m) else month_end.day
            if cur == start_date and start_date != end_date:
                use_hours = list(range(t_start.hour, 24))
            elif (end_date.year, end_date.month, end_date.day) == (y, m, d_end) and start_date != end_date:
                use_hours = list(range(0, t_end.hour + 1))
            else:
                use_hours = list(range(24))
            request = {
                "product_type": "reanalysis",
                "variable": [
                    "mean_sea_level_pressure",
                    "10m_u_component_of_wind",
                    "10m_v_component_of_wind",
                ],
                "year": [f"{y:04d}"],
                "month": [f"{m:02d}"],
                "day": [f"{d:02d}" for d in range(d_start, d_end + 1)],
                "time": [f"{h:02d}:00" for h in use_hours],
                "area": [YB_MET[1], XB_MET[0], YB_MET[0], XB_MET[1]],
                "format": "netcdf",
            }
            part_path = ERA5_DIR / f"ERA5_{storm_id}_{y:04d}{m:02d}_part.nc"
            client.retrieve("reanalysis-era5-single-levels", request).download(str(part_path))
            paths_to_merge.append(part_path)
            cur = month_end + timedelta(days=1)
        if len(paths_to_merge) == 1:
            paths_to_merge[0].rename(era5_path)
        else:
            ds_list = [xr.open_dataset(p) for p in paths_to_merge]
            combined = xr.concat(sorted(ds_list, key=lambda d: d["valid_time"].values[0]), dim="valid_time")
            for ds in ds_list:
                ds.close()
            combined.to_netcdf(era5_path)
            for p in paths_to_merge:
                p.unlink(missing_ok=True)

    return _create_session_from_era5(era5_path, storm_id=storm_id)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".nc"):
        raise HTTPException(status_code=400, detail="Upload a .nc (NetCDF) file")
    contents = await file.read()
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}.nc"
    path.write_bytes(contents)
    try:
        return _create_session_from_era5(path, storm_id=None)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise


@app.get("/api/frame/{session_id}/{time_index}")
async def api_frame(session_id: str, time_index: int):
    session = get_session(session_id)
    session["last_access"] = time.time()
    track = session["track"]
    png_bytes = render_frame(session, time_index, track)
    return Response(content=png_bytes, media_type="image/png")


@app.get("/api/track/{session_id}")
async def api_get_track(session_id: str):
    session = get_session(session_id)
    session["last_access"] = time.time()
    return {"track": session["track"]}


@app.post("/api/track/add")
async def api_track_add(body: TrackAddBody):
    session_id = body.session_id
    time_index = body.time_index
    lon = body.lon
    lat = body.lat
    session = get_session(session_id)
    session["last_access"] = time.time()
    frame_list = session["frame_list"]
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=400, detail="Invalid time_index")
    _, grid_time = frame_list[time_index]
    pressure_hpa = interpolate_pressure(session, time_index, lon, lat)
    time_utc_str = pd.Timestamp(grid_time).strftime("%Y-%m-%d %H:%M:%S")
    # Replace existing point at same time (within 1 min) or append
    track = session["track"]
    t_ts = pd.Timestamp(grid_time)
    track[:] = [
        p for p in track
        if abs((pd.Timestamp(p["time_utc"]) - t_ts).total_seconds()) >= 60
    ]
    track.append({
        "time_utc": time_utc_str,
        "time_index": time_index,
        "lon": round(float(lon), 6),
        "lat": round(float(lat), 6),
        "pressure_hpa": round(pressure_hpa, 2),
    })
    track.sort(key=lambda p: p["time_index"])
    return {"track": session["track"]}


@app.post("/api/track/delete")
async def api_track_delete(body: TrackDeleteBody):
    """Delete point at time_index. If time_index < 0, delete last point."""
    session_id = body.session_id
    time_index = body.time_index
    session = get_session(session_id)
    session["last_access"] = time.time()
    track = session["track"]
    if not track:
        return {"track": []}
    if time_index < 0:
        track.pop()
    else:
        session["track"] = [p for p in track if p["time_index"] != time_index]
    return {"track": session["track"]}


@app.get("/api/csv/{session_id}")
async def api_csv(session_id: str):
    session = get_session(session_id)
    session["last_access"] = time.time()
    track = session["track"]
    storm_id = session.get("storm_id")
    if not track:
        csv_content = "storm_id,time_utc,lon,lat,pressure_hpa\n"
    else:
        df = pd.DataFrame(track)
        df = df.rename(columns={"pressure_hpa": "pressure_hpa"})
        df.insert(0, "storm_id", int(storm_id) if storm_id is not None else "")
        df = df[["storm_id", "time_utc", "lon", "lat", "pressure_hpa"]]
        csv_content = df.to_csv(index=False)
    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=storm_track.csv"},
    )


def _labelled_window_indices(session: dict, pad_hours: int = 3) -> tuple[int, int]:
    """Return (start_idx, end_idx) in frame_list covering labelled track ± pad_hours.

    Clamps to available frame indices.
    """
    track = session.get("track") or []
    frame_list = session["frame_list"]
    if not track or not frame_list:
        raise HTTPException(status_code=400, detail="Track is empty; cannot determine labelled window")

    # Track indices are in terms of frame_list indices
    t_indices = [int(p["time_index"]) for p in track]
    first_idx = max(0, min(t_indices))
    last_idx = min(len(frame_list) - 1, max(t_indices))

    _, first_time = frame_list[first_idx]
    _, last_time = frame_list[last_idx]

    start_target = pd.Timestamp(first_time) - pd.Timedelta(hours=pad_hours)
    end_target = pd.Timestamp(last_time) + pd.Timedelta(hours=pad_hours)

    frame_times = [pd.Timestamp(t) for _, t in frame_list]

    # Find earliest index with time >= start_target
    start_idx = 0
    for i, t in enumerate(frame_times):
        if t >= start_target:
            start_idx = i
            break

    # Find latest index with time <= end_target
    end_idx = len(frame_times) - 1
    for i in range(len(frame_times) - 1, -1, -1):
        if frame_times[i] <= end_target:
            end_idx = i
            break

    start_idx = max(0, min(start_idx, len(frame_list) - 1))
    end_idx = max(start_idx, min(end_idx, len(frame_list) - 1))
    return start_idx, end_idx


@app.get("/api/gif/era5/{session_id}")
async def api_era5_gif(session_id: str):
    """Generate an ERA5 GIF over the labelled window ±3h with full track line in every frame (no point markers)."""
    session = get_session(session_id)
    session["last_access"] = time.time()
    track = session.get("track") or []
    if not track:
        raise HTTPException(status_code=400, detail="Track is empty; label at least one point first")

    start_idx, end_idx = _labelled_window_indices(session, pad_hours=3)
    frame_indices = list(range(start_idx, end_idx + 1))
    if not frame_indices:
        raise HTTPException(status_code=400, detail="No frames available for labelled window")

    # Use full track (constant across frames)
    frames: list[np.ndarray] = []
    for ti in frame_indices:
        png_bytes = render_frame(session, ti, track, show_track_points=False)
        buf = io.BytesIO(png_bytes)
        img = imageio.imread(buf)
        frames.append(img)

    if not frames:
        raise HTTPException(status_code=500, detail="Failed to render any GIF frames")

    out = io.BytesIO()
    # Use FPS similar to analysis notebook GIFs
    imageio.mimsave(out, frames, format="GIF", loop=0, fps=4)
    out.seek(0)
    return Response(content=out.read(), media_type="image/gif")


@app.post("/api/combined/save/{session_id}")
async def api_combined_save(session_id: str):
    """
    Append this session's track to the combined labeled CSV, keyed by (storm_id, time_utc).
    Existing rows for those keys are replaced.
    """
    session = get_session(session_id)
    session["last_access"] = time.time()
    storm_id = session.get("storm_id")
    if storm_id is None:
        raise HTTPException(status_code=400, detail="Session is not associated with a storm_id")
    track = session.get("track") or []
    if not track:
        raise HTTPException(status_code=400, detail="Track is empty; nothing to save")

    new_df = pd.DataFrame(track)
    new_df["storm_id"] = int(storm_id)
    new_df = new_df[["storm_id", "time_utc", "lon", "lat", "pressure_hpa"]]
    new_df["time_utc"] = pd.to_datetime(new_df["time_utc"])

    if COMBINED_TRACKS_PATH.exists():
        existing = pd.read_csv(COMBINED_TRACKS_PATH)
        if not existing.empty:
            existing["time_utc"] = pd.to_datetime(existing["time_utc"])
            mask = existing["storm_id"] == int(storm_id)
            existing = existing[~mask]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
    else:
        COMBINED_TRACKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        combined = new_df

    combined.sort_values(["storm_id", "time_utc"], inplace=True)
    combined.to_csv(COMBINED_TRACKS_PATH, index=False)
    return {"rows": len(new_df)}


@app.get("/api/combined/csv")
async def api_combined_csv():
    """
    Download the combined CSV with all saved storm tracks.
    """
    if not COMBINED_TRACKS_PATH.exists():
        csv_content = "storm_id,time_utc,lon,lat,pressure_hpa\n"
        return StreamingResponse(
            io.BytesIO(csv_content.encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=storm_tracks_labeled_web.csv"},
        )
    return FileResponse(
        COMBINED_TRACKS_PATH,
        media_type="text/csv",
        filename="storm_tracks_labeled_web.csv",
    )


# Serve static frontend
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """Serve index.html from static if present."""
    index = Path(__file__).parent / "static" / "index.html"
    if index.exists():
        return Response(content=index.read_text(), media_type="text/html")
    return {"message": "StormTracker ERA5 API. Use /static/index.html for the web UI."}
