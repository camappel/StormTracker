"""
StormTracker ERA5 Web App — FastAPI backend.
Upload ERA5 NetCDF, step through time, draw storm track by clicking, download CSV.
"""
import io
import tempfile
import time
import uuid
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
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Display bounds (same as correct_storm_tracks.py)
XB_MET = [-30, 15]
YB_MET = [40, 70]
INT_SKIP = 3
VMIN, VMAX = 967, 1020
STORM_ID_DEFAULT = 1

# Session TTL (seconds); optional cleanup not implemented in MVP
SESSION_TTL = 3600

app = FastAPI(title="StormTracker ERA5 Web App")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# session_id -> { path, LON, LAT, P_MET, U_MET, V_MET, ts_valid, frame_list, track, last_access }
sessions: dict = {}
# Temp directory for uploads
UPLOAD_DIR = Path(tempfile.gettempdir()) / "stormtracker_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


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
    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="white")
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor="lightgray", alpha=0.3)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.8, color="white")
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.5, color="white")
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.gridlines(draw_labels=True, linewidth=0.5, color="gray", alpha=0.5, linestyle="--")


def render_frame(session: dict, time_index: int, track: list) -> bytes:
    """Render one time step as PNG: pressure + wind + track overlay."""
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
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
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
        ax.scatter(
            track_lons,
            track_lats,
            c="darkred",
            s=30,
            zorder=11,
            transform=ccrs.PlateCarree(),
        )
    ts_str = pd.Timestamp(grid_time).strftime("%Y-%m-%d %H:%M UTC")
    ax.set_title(f"StormTracker — {ts_str} (step {time_index + 1}/{len(frame_list)})", fontsize=12)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
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


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".nc"):
        raise HTTPException(status_code=400, detail="Upload a .nc (NetCDF) file")
    contents = await file.read()
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}.nc"
    path.write_bytes(contents)
    try:
        data = parse_era5(path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    session_id = uuid.uuid4().hex
    sessions[session_id] = {
        "path": path,
        **data,
        "track": [],
        "last_access": time.time(),
    }
    frame_list = data["frame_list"]
    times = [{"index": i, "time_utc": pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")} for i, t in frame_list]
    return {
        "session_id": session_id,
        "times": times,
        "bounds": {
            "lon_min": data["lon_min"],
            "lon_max": data["lon_max"],
            "lat_min": data["lat_min"],
            "lat_max": data["lat_max"],
        },
    }


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
    if not track:
        csv_content = "storm_id,time_utc,lon,lat,pressure_hpa\n"
    else:
        df = pd.DataFrame(track)
        df = df.rename(columns={"pressure_hpa": "pressure_hpa"})
        df.insert(0, "storm_id", STORM_ID_DEFAULT)
        df = df[["storm_id", "time_utc", "lon", "lat", "pressure_hpa"]]
        csv_content = df.to_csv(index=False)
    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=storm_track.csv"},
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
