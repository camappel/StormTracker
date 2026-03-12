"""
StormTracker ERA5 Web App — FastAPI backend.
Upload ERA5 NetCDF, step through time, draw storm track by clicking, download CSV.
"""
import io
import json
import os
import re
import tempfile
import time
import uuid
import warnings
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

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
XB_GTSM = [-10, 10]
YB_GTSM = [45, 60]

# Session TTL (seconds); optional cleanup not implemented in MVP
SESSION_TTL = 3600

# Paths
ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "StormTracker" / "data"
STORMS_PATH = DATA_DIR / "storms.json"
WATER_LEVEL_SERIES_PATH = DATA_DIR / "water_level_series.json"
ERA5_DIR = DATA_DIR / "era5"
GTSM_DIR = DATA_DIR / "gtsm"
CODEC_GTSM_DIRS = [
    ROOT_DIR / "Objective1" / "2_DATA" / "3_CODEC_GTSM_API",
    ROOT_DIR / "Objective1" / "2_Data" / "3_CODEC_GTSM_API",
]

ANALYSIS_OUTPUT_DIR = ROOT_DIR / "Objective1" / "3a_ANALYSIS_Python" / "output"
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

def _json_load(path: Path):
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Required data file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {path}: {exc}")


def _json_save(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize_iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_window(start_value: str | datetime, end_value: str | datetime) -> tuple[str, str]:
    start_iso = _normalize_iso_utc(start_value.isoformat() if isinstance(start_value, datetime) else str(start_value))
    end_iso = _normalize_iso_utc(end_value.isoformat() if isinstance(end_value, datetime) else str(end_value))
    if not start_iso or not end_iso:
        raise HTTPException(status_code=400, detail="Invalid ERA5/GTSM window")
    return start_iso, end_iso


def _window_token(iso_utc: str) -> str:
    return iso_utc.replace("-", "").replace(":", "").replace("+00:00", "Z").replace("T", "T").replace(".", "")


def _window_key(start_iso: str, end_iso: str) -> str:
    s, e = _canonical_window(start_iso, end_iso)
    return f"{_window_token(s)}_{_window_token(e)}"


def _resolve_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p


def _rel_path_str(path: Path) -> str:
    return str(path.relative_to(ROOT_DIR) if path.is_absolute() else path)


def _storm_int_from_name(storm_name: str) -> int:
    digits = re.findall(r"\d+", storm_name or "")
    if not digits:
        raise HTTPException(status_code=400, detail=f"Storm name has no numeric id: {storm_name!r}")
    return int(digits[-1])


def _load_storms_metadata() -> list[dict]:
    payload = _json_load(STORMS_PATH)
    if not isinstance(payload, list):
        raise HTTPException(status_code=500, detail="storms.json must be a JSON list")
    cleaned = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("storm"):
            continue
        era5_window = row.get("era5_window") or {}
        storm_window = row.get("storm_window") or {}
        closures = row.get("closures") if isinstance(row.get("closures"), list) else []
        cleaned.append(
            {
                "storm": str(row["storm"]),
                "era5_window": {
                    "start": _normalize_iso_utc(era5_window.get("start")),
                    "end": _normalize_iso_utc(era5_window.get("end")),
                },
                "storm_window": {
                    "start": _normalize_iso_utc(storm_window.get("start")),
                    "end": _normalize_iso_utc(storm_window.get("end")),
                },
                "era5_path": str(row.get("era5_path") or ""),
                "era5_files": [
                    {
                        "start": _normalize_iso_utc(item.get("start")),
                        "end": _normalize_iso_utc(item.get("end")),
                        "path": str(item.get("path") or ""),
                    }
                    for item in (row.get("era5_files") or [])
                    if isinstance(item, dict)
                ],
                "gtsm_files": [
                    {
                        "start": _normalize_iso_utc(item.get("start")),
                        "end": _normalize_iso_utc(item.get("end")),
                        "path": str(item.get("path") or ""),
                    }
                    for item in (row.get("gtsm_files") or [])
                    if isinstance(item, dict)
                ],
                "closures": [
                    {
                        "start": _normalize_iso_utc(c.get("start")),
                        "end": _normalize_iso_utc(c.get("end")),
                    }
                    for c in closures
                    if isinstance(c, dict)
                ],
            }
        )
    return cleaned


def _save_storms_metadata(rows: list[dict]) -> None:
    _json_save(STORMS_PATH, rows)


def _find_storm_meta(storm_id: int) -> tuple[int, dict]:
    rows = _load_storms_metadata()
    for idx, row in enumerate(rows):
        try:
            if _storm_int_from_name(row["storm"]) == int(storm_id):
                return idx, row
        except HTTPException:
            continue
    raise HTTPException(status_code=404, detail=f"Storm {storm_id} not found in storms.json")


def _pick_default_window(meta: dict) -> tuple[str | None, str | None]:
    storm_win = meta.get("storm_window") or {}
    era5_win = meta.get("era5_window") or {}
    start = storm_win.get("start") or era5_win.get("start")
    end = storm_win.get("end") or era5_win.get("end")
    return start, end


def _upsert_window_file(meta_row: dict, key: str, start_iso: str, end_iso: str, path: Path) -> None:
    files = meta_row.get(key)
    if not isinstance(files, list):
        files = []
    rel = _rel_path_str(path)
    replaced = False
    for item in files:
        if item.get("start") == start_iso and item.get("end") == end_iso:
            item["path"] = rel
            replaced = True
            break
    if not replaced:
        files.append({"start": start_iso, "end": end_iso, "path": rel})
    meta_row[key] = files


def _has_exact_cached_file(meta: dict, key: str, start_iso: str | None, end_iso: str | None) -> bool:
    if not start_iso or not end_iso:
        return False
    for item in meta.get(key) or []:
        if item.get("start") != start_iso or item.get("end") != end_iso:
            continue
        p = _resolve_path(item.get("path"))
        if p and p.exists():
            return True
    return False


def _load_water_level_series_all() -> dict:
    payload = _json_load(WATER_LEVEL_SERIES_PATH)
    if isinstance(payload, dict):
        storms = payload.get("storms")
        if isinstance(storms, dict):
            return storms
    if isinstance(payload, list):
        out = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = row.get("storm")
            series = row.get("series")
            if name and isinstance(series, list):
                out[str(name)] = series
        return out
    raise HTTPException(status_code=500, detail="water_level_series.json must contain storm series data")


def _series_for_storm(storm_id: int, storm_name: str) -> list[dict]:
    all_series = _load_water_level_series_all()
    keys = [storm_name, str(storm_id), f"storm_{storm_id}"]
    for key in keys:
        if key in all_series and isinstance(all_series[key], list):
            return all_series[key]
    raise HTTPException(status_code=404, detail=f"No water-level series found for storm {storm_id}")


def _get_storm_catalog() -> list[dict]:
    rows = []
    for storm in _load_storms_metadata():
        sid = _storm_int_from_name(storm["storm"])
        default_start, default_end = _pick_default_window(storm)
        closures = storm.get("closures") or []
        c_start = closures[0]["start"] if closures else None
        c_end = closures[-1]["end"] if closures else None
        rows.append(
            {
                "storm_id": sid,
                "storm": storm["storm"],
                "label": storm["storm"].replace("_", " ").title(),
                "storm_start_local": c_start,
                "storm_end_local": c_end,
                "default_start_utc": default_start,
                "default_end_utc": default_end,
                "era5_window": storm.get("era5_window") or {},
                "storm_window": storm.get("storm_window") or {},
                "closures": closures,
                "has_water_level_series": True,
            }
        )
    rows.sort(key=lambda r: r["storm_id"])
    return rows


@app.get("/api/storms")
async def api_storms():
    """
    Return storm catalog with suggested ERA5 download windows and
    whether a pre-generated water level plot exists.
    """
    return {"storms": _get_storm_catalog()}


@app.get("/api/storms/{storm_id}/water_level_series")
async def api_storm_water_level_series(
    storm_id: int,
):
    _, meta = _find_storm_meta(storm_id)
    series = _series_for_storm(storm_id, meta["storm"])
    normalized = []
    for p in series:
        if not isinstance(p, dict):
            continue
        t_utc = _normalize_iso_utc(p.get("time_utc") or p.get("time"))
        if not t_utc:
            continue
        normalized.append(
            {
                "time_utc": t_utc,
                "water_level": p.get("water_level", p.get("water_level_m")),
                "surge": p.get("surge", p.get("surge_m")),
                "tide": p.get("tide", p.get("predicted_tide_m")),
            }
        )
    normalized.sort(key=lambda p: p["time_utc"])
    default_start, default_end = _pick_default_window(meta)
    full_start = normalized[0]["time_utc"] if normalized else default_start
    full_end = normalized[-1]["time_utc"] if normalized else default_end
    default_window_cached = _has_exact_cached_file(meta, "era5_files", default_start, default_end)
    if not default_window_cached and default_start and default_end:
        # Backward compatibility: legacy single era5_path + era5_window fields
        legacy_match = (meta.get("era5_window") or {}).get("start") == default_start and (meta.get("era5_window") or {}).get("end") == default_end
        if legacy_match:
            lp = _resolve_path(meta.get("era5_path"))
            default_window_cached = bool(lp and lp.exists())
    return {
        "series": normalized,
        "full_series_start_utc": full_start,
        "full_series_end_utc": full_end,
        "default_start_utc": default_start or full_start,
        "default_end_utc": default_end or full_end,
        "default_window_cached": default_window_cached,
        "storm_windows": [
            {"start_utc": c.get("start"), "end_utc": c.get("end")}
            for c in (meta.get("closures") or [])
            if c.get("start") and c.get("end")
        ],
        "storm": meta["storm"],
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
        "gtsm_available": any(p.exists() for p in CODEC_GTSM_DIRS),
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
        s = dt_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Backward compatibility: treat naive timestamps as UTC.
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {dt_str!r}")


def _build_era5_month_segments(start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Build one ERA5 request segment per month in the selected window."""
    segments = []
    cur_day = start_dt.date()
    end_day = end_dt.date()
    while cur_day <= end_day:
        y = cur_day.year
        m = cur_day.month
        day_start = cur_day.day
        day_end = cur_day.day
        probe = cur_day + timedelta(days=1)
        while probe <= end_day and probe.year == y and probe.month == m:
            day_end = probe.day
            probe = probe + timedelta(days=1)
        segments.append(
            {
                "year": y,
                "month": m,
                "days": list(range(day_start, day_end + 1)),
                "hours": list(range(24)),
            }
        )
        cur_day = probe
    return segments


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
    start_iso, end_iso = _canonical_window(t_start, t_end)
    win_key = _window_key(start_iso, end_iso)

    meta_rows = _load_storms_metadata()
    meta_idx, meta = _find_storm_meta(storm_id)
    ERA5_DIR.mkdir(parents=True, exist_ok=True)
    target_era5_path = ERA5_DIR / f"ERA5_storm_{storm_id}_{win_key}.nc"
    era5_path = None
    for item in meta.get("era5_files") or []:
        if item.get("start") == start_iso and item.get("end") == end_iso:
            candidate = _resolve_path(item.get("path"))
            if candidate and candidate.exists():
                era5_path = candidate
                break
    if era5_path is None and meta.get("era5_window", {}).get("start") == start_iso and meta.get("era5_window", {}).get("end") == end_iso:
        candidate = _resolve_path(meta.get("era5_path"))
        if candidate and candidate.exists():
            era5_path = candidate

    if era5_path is None:
        era5_path = target_era5_path
        if not era5_path.exists():
            try:
                import cdsapi  # type: ignore
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="cdsapi is not installed on the server; cannot download ERA5 automatically.",
                )
            client = cdsapi.Client()
            segments = _build_era5_month_segments(t_start, t_end)
            if not segments:
                raise HTTPException(status_code=400, detail="Invalid time range")
            part_paths = []
            for i, seg in enumerate(segments):
                request = {
                    "product_type": "reanalysis",
                    "variable": [
                        "mean_sea_level_pressure",
                        "10m_u_component_of_wind",
                        "10m_v_component_of_wind",
                    ],
                    "year": [f"{seg['year']:04d}"],
                    "month": [f"{seg['month']:02d}"],
                    "day": [f"{d:02d}" for d in seg["days"]],
                    "time": [f"{h:02d}:00" for h in seg["hours"]],
                    "area": [YB_MET[1], XB_MET[0], YB_MET[0], XB_MET[1]],
                    "format": "netcdf",
                }
                part_path = ERA5_DIR / f"ERA5_part_storm_{storm_id}_{win_key}_{i:02d}.nc"
                client.retrieve("reanalysis-era5-single-levels", request).download(str(part_path))
                part_paths.append(part_path)
            valid_time_start = np.datetime64(t_start)
            valid_time_end = np.datetime64(t_end)
            ds_list = [xr.open_dataset(p) for p in part_paths]
            try:
                combined = xr.concat(sorted(ds_list, key=lambda d: d["valid_time"].values[0]), dim="valid_time")
                combined = combined.sortby("valid_time")
                combined = combined.sel(valid_time=slice(valid_time_start, valid_time_end))
                if int(combined.sizes.get("valid_time", 0)) == 0:
                    raise HTTPException(status_code=500, detail="Downloaded ERA5 data does not cover requested time range.")
                combined.to_netcdf(era5_path)
                combined.close()
            finally:
                for ds in ds_list:
                    ds.close()
                for p in part_paths:
                    p.unlink(missing_ok=True)

    # Normalize cached naming so exact-window files always include full time tokens.
    if era5_path != target_era5_path:
        if not target_era5_path.exists():
            target_era5_path.write_bytes(era5_path.read_bytes())
        era5_path = target_era5_path

    meta_rows[meta_idx]["era5_path"] = _rel_path_str(era5_path)
    meta_rows[meta_idx]["era5_window"] = {"start": start_iso, "end": end_iso}
    _upsert_window_file(meta_rows[meta_idx], "era5_files", start_iso, end_iso, era5_path)
    _save_storms_metadata(meta_rows)

    session_payload = _create_session_from_era5(era5_path, storm_id=storm_id)
    sid = session_payload["session_id"]
    sessions[sid]["window_start_utc"] = start_iso
    sessions[sid]["window_end_utc"] = end_iso
    _build_session_gtsm_subset(sessions[sid])
    return session_payload


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


def _codec_gtsm_root() -> Path | None:
    for root in CODEC_GTSM_DIRS:
        if root.exists():
            return root
    return None


def _get_codec_nc_path(ts: pd.Timestamp) -> Path | None:
    root = _codec_gtsm_root()
    if root is None:
        return None
    y = ts.year
    m = ts.month
    # Prefer extracted monthly NetCDF; these are the reliable files in this repo.
    extracted_dir = root / f"GTSM_{y}_{m:02d}_extracted"
    if extracted_dir.exists():
        ncs = sorted(extracted_dir.glob("*.nc"))
        if ncs:
            return ncs[0]
    raw = root / f"GTSM_{y}_{m:02d}.nc"
    if raw.exists():
        return raw
    return None


def _gtsm_time_and_var(ds: xr.Dataset) -> tuple[str, str] | None:
    time_name = "time" if "time" in ds.coords else ("valid_time" if "valid_time" in ds.coords else None)
    if not time_name:
        return None
    surge_var = "storm_surge_residual" if "storm_surge_residual" in ds.data_vars else None
    if surge_var is None:
        surge_var = next((v for v in ds.data_vars if "surge" in v.lower()), None)
    if surge_var is None and ds.data_vars:
        surge_var = list(ds.data_vars)[0]
    if not surge_var:
        return None
    return time_name, surge_var


def _build_gtsm_subset_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    frame_times: list[pd.Timestamp],
    meta_rows: list[dict],
    meta_idx: int,
) -> Path | None:
    # Reuse exact cached subset first.
    meta_row = meta_rows[meta_idx]
    for item in meta_row.get("gtsm_files") or []:
        if item.get("start") == start_iso and item.get("end") == end_iso:
            p = _resolve_path(item.get("path"))
            if p and p.exists():
                return p

    win_key = _window_key(start_iso, end_iso)
    subset_dir = GTSM_DIR / f"storm_{storm_id}"
    subset_dir.mkdir(parents=True, exist_ok=True)
    subset_path = subset_dir / f"GTSM_subset_storm_{storm_id}_{win_key}.nc"
    if subset_path.exists():
        _upsert_window_file(meta_row, "gtsm_files", start_iso, end_iso, subset_path)
        _save_storms_metadata(meta_rows)
        return subset_path

    month_targets: dict[tuple[int, int], list[pd.Timestamp]] = {}
    for t in frame_times:
        key = (t.year, t.month)
        month_targets.setdefault(key, []).append(pd.Timestamp(t))

    pieces: list[xr.Dataset] = []
    for (year, month), targets in month_targets.items():
        sample_ts = pd.Timestamp(datetime(year, month, 1))
        nc_path = _get_codec_nc_path(sample_ts)
        if not nc_path or not nc_path.exists():
            continue
        try:
            ds = xr.open_dataset(nc_path, engine="netcdf4")
        except Exception:
            continue
        try:
            tv = _gtsm_time_and_var(ds)
            if not tv:
                continue
            time_name, _ = tv
            times = pd.to_datetime(ds[time_name].values)
            if len(times) == 0:
                continue
            indices = []
            for target in targets:
                diffs = np.abs(times - pd.Timestamp(target))
                idx = int(np.argmin(diffs))
                if diffs[idx] <= pd.Timedelta(minutes=10):
                    indices.append(idx)
            if not indices:
                continue
            uniq = sorted(set(indices))
            piece = ds.isel({time_name: uniq}).load()
            pieces.append(piece)
        finally:
            ds.close()

    if not pieces:
        return None

    tv0 = _gtsm_time_and_var(pieces[0])
    if not tv0:
        return None
    time_name0, _ = tv0
    combined = xr.concat(pieces, dim=time_name0).sortby(time_name0)
    tvals = pd.to_datetime(combined[time_name0].values)
    _, uniq_idx = np.unique(np.asarray(tvals), return_index=True)
    uniq_idx = sorted(int(i) for i in uniq_idx)
    combined = combined.isel({time_name0: uniq_idx})
    combined.to_netcdf(subset_path)
    combined.close()

    _upsert_window_file(meta_row, "gtsm_files", start_iso, end_iso, subset_path)
    _save_storms_metadata(meta_rows)
    return subset_path


def _render_gtsm_frame_from_subset(subset_path: Path, ts: pd.Timestamp, target_path: Path) -> bool:
    if not subset_path.exists():
        return False
    try:
        ds = xr.open_dataset(subset_path, engine="netcdf4")
    except Exception:
        return False
    try:
        tv = _gtsm_time_and_var(ds)
        if not tv:
            return False
        time_name, surge_var = tv
        times = pd.to_datetime(ds[time_name].values)
        if len(times) == 0:
            return False
        idx = int(np.argmin(np.abs(times - pd.Timestamp(ts))))
        su = np.asarray(ds[surge_var].values)

        lon_span = float(XB_GTSM[1] - XB_GTSM[0])
        lat_span = float(YB_GTSM[1] - YB_GTSM[0])
        map_ratio = lon_span / lat_span if lat_span else 1.5
        fig_height = 8.0
        fig = plt.figure(figsize=(fig_height * map_ratio, fig_height))
        ax = fig.add_axes([0.0, 0.0, 1.0, 1.0], projection=ccrs.PlateCarree())
        add_map_base(ax, XB_GTSM, YB_GTSM)

        if ("latitude" in ds.coords or "latitude" in ds.dims) and ("longitude" in ds.coords or "longitude" in ds.dims):
            lat = np.asarray(ds["latitude"].values)
            lon = np.asarray(ds["longitude"].values)
            if su.ndim == 3 and su.shape[0] == len(times):
                grid = su[idx, :, :]
            elif su.ndim == 3 and su.shape[-1] == len(times):
                grid = su[:, :, idx]
            else:
                plt.close(fig)
                return False
            xx, yy = np.meshgrid(lon, lat)
            ax.pcolormesh(xx, yy, grid, cmap="seismic", shading="auto", transform=ccrs.PlateCarree())
        else:
            x_name = "station_x_coordinate" if "station_x_coordinate" in ds else "lon"
            y_name = "station_y_coordinate" if "station_y_coordinate" in ds else "lat"
            if x_name not in ds or y_name not in ds:
                plt.close(fig)
                return False
            xs = np.asarray(ds[x_name].values)
            ys = np.asarray(ds[y_name].values)
            vals = su[idx, :] if su.ndim == 2 and su.shape[0] == len(times) else su[:, idx]
            mask = (xs >= XB_GTSM[0]) & (xs <= XB_GTSM[1]) & (ys >= YB_GTSM[0]) & (ys <= YB_GTSM[1])
            ax.scatter(xs[mask], ys[mask], c=vals[mask], s=12, cmap="seismic", transform=ccrs.PlateCarree())

        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=100, bbox_inches=None, pad_inches=0)
        plt.close(fig)
        return True
    finally:
        ds.close()


def _build_session_gtsm_subset(session: dict) -> Path | None:
    storm_id = session.get("storm_id")
    if storm_id is None:
        return None
    start_iso = session.get("window_start_utc")
    end_iso = session.get("window_end_utc")
    if not start_iso or not end_iso:
        frame_list = session.get("frame_list") or []
        if not frame_list:
            return None
        start_iso = _normalize_iso_utc(pd.Timestamp(frame_list[0][1]).isoformat())
        end_iso = _normalize_iso_utc(pd.Timestamp(frame_list[-1][1]).isoformat())
    if not start_iso or not end_iso:
        return None
    frame_times = [pd.Timestamp(t) for _, t in (session.get("frame_list") or [])]
    meta_rows = _load_storms_metadata()
    meta_idx, _ = _find_storm_meta(int(storm_id))
    subset = _build_gtsm_subset_for_window(int(storm_id), start_iso, end_iso, frame_times, meta_rows, meta_idx)
    if subset:
        session["gtsm_subset_path"] = str(subset)
    return subset


@app.get("/api/gtsm/frame/{session_id}/{time_index}")
async def api_gtsm_frame(session_id: str, time_index: int):
    session = get_session(session_id)
    session["last_access"] = time.time()
    frame_list = session.get("frame_list") or []
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    storm_id = session.get("storm_id")
    if storm_id is None:
        raise HTTPException(status_code=400, detail="GTSM frame endpoint requires a storm session")
    _, ts = frame_list[time_index]
    ts_pd = pd.Timestamp(ts)
    start_iso = session.get("window_start_utc")
    end_iso = session.get("window_end_utc")
    if not start_iso or not end_iso:
        start_iso = _normalize_iso_utc(pd.Timestamp(frame_list[0][1]).isoformat())
        end_iso = _normalize_iso_utc(pd.Timestamp(frame_list[-1][1]).isoformat())
    if not start_iso or not end_iso:
        raise HTTPException(status_code=500, detail="Unable to resolve session window for GTSM")
    win_key = _window_key(start_iso, end_iso)
    out_dir = GTSM_DIR / f"storm_{int(storm_id)}" / win_key
    out_name = f"gtsm_{_window_token(_normalize_iso_utc(ts_pd.isoformat()) or ts_pd.strftime('%Y%m%dT%H%M%SZ'))}.png"
    out_path = out_dir / out_name
    if not out_path.exists():
        subset_path = _resolve_path(session.get("gtsm_subset_path"))
        if not subset_path or not subset_path.exists():
            subset_path = _build_session_gtsm_subset(session)
        if not subset_path or not subset_path.exists():
            raise HTTPException(status_code=404, detail="No matching GTSM subset available for this window")
        rendered = _render_gtsm_frame_from_subset(subset_path, ts_pd, out_path)
        if not rendered:
            raise HTTPException(status_code=404, detail="No matching GTSM frame available for this timestamp")
    return FileResponse(out_path, media_type="image/png")


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

    # Persist derived storm_window = labelled frame range ±3h to storms.json
    start_idx, end_idx = _labelled_window_indices(session, pad_hours=3)
    frame_list = session["frame_list"]
    _, start_ts = frame_list[start_idx]
    _, end_ts = frame_list[end_idx]
    start_iso = pd.Timestamp(start_ts).to_pydatetime().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = pd.Timestamp(end_ts).to_pydatetime().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    rows = _load_storms_metadata()
    idx, _ = _find_storm_meta(int(storm_id))
    rows[idx]["storm_window"] = {"start": start_iso, "end": end_iso}
    _save_storms_metadata(rows)
    return {"rows": len(new_df), "storm_window": {"start": start_iso, "end": end_iso}}


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
