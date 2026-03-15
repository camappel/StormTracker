"""
StormTracker ERA5 Web App — FastAPI backend.
Upload ERA5 NetCDF, step through time, draw storm track by clicking, update persisted track data.
"""
import io
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
import warnings
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Literal

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
from fastapi.responses import FileResponse, Response
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
WATER_SERIES_PAD_HOURS = 62

# Paths
APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
ROOT_DIR = PROJECT_DIR


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    return Path(raw).expanduser()


def _env_path_list(name: str, defaults: list[Path]) -> list[Path]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return defaults
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


LEGACY_OBJECTIVE_DIR = PROJECT_DIR.parent / "Objective1"
DEFAULT_CODEC_GTSM_DIRS = [
    PROJECT_DIR / "data" / "codec_gtsm",
    LEGACY_OBJECTIVE_DIR / "2_DATA" / "3_CODEC_GTSM_API",
    LEGACY_OBJECTIVE_DIR / "2_Data" / "3_CODEC_GTSM_API",
]

DATA_ROOT_DIR = _env_path("STORMTRACKER_DATA_DIR", PROJECT_DIR / "data")
SUPPORTED_BARRIERS = ("eastern_scheldt", "thames")
DEFAULT_BARRIER = os.getenv("STORMTRACKER_DEFAULT_BARRIER", "eastern_scheldt").strip() or "eastern_scheldt"
ALLOWED_STORM_TYPES = ("Channel Rat", "North Sea Storm")
CODEC_GTSM_DIRS = _env_path_list("STORMTRACKER_CODEC_GTSM_DIRS", DEFAULT_CODEC_GTSM_DIRS)
GTSM_SUBSET_STORM_FILE_RE = re.compile(r"^GTSM_subset_storm_(\d+)_(.+)\.nc$")


def _normalize_barrier(barrier: str | None) -> str:
    resolved = (barrier or DEFAULT_BARRIER).strip().lower()
    if resolved not in SUPPORTED_BARRIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid barrier '{barrier}'. Supported barriers: {', '.join(SUPPORTED_BARRIERS)}",
        )
    return resolved


def _normalize_dataset(dataset: str | None) -> str:
    """Backward-compatible alias; dataset now maps to barrier."""
    return _normalize_barrier(dataset)


def _master_storms_path() -> Path:
    return DATA_ROOT_DIR / "storms.json"


def _water_level_series_path(barrier: str | None = None) -> Path:
    barrier_key = _normalize_barrier(barrier)
    return DATA_ROOT_DIR / f"water_level_series_{barrier_key}.json"


def _storm_root_dir(storm_id: int) -> Path:
    return DATA_ROOT_DIR / f"storm_{int(storm_id)}"


def _storm_era5_dir(storm_id: int) -> Path:
    return _storm_root_dir(storm_id) / "era5"


def _shared_era5_dir() -> Path:
    return DATA_ROOT_DIR / "era5"


def _shared_gtsm_dir() -> Path:
    return DATA_ROOT_DIR / "gtsm"


def _shared_storm_track_dir() -> Path:
    return DATA_ROOT_DIR / "storm_track"


def _legacy_storm_gtsm_dir(storm_id: int) -> Path:
    return _storm_root_dir(storm_id) / "gtsm"


def _era5_file_name(start_iso: str, end_iso: str) -> str:
    return f"ERA5_{_window_key(start_iso, end_iso)}.nc"


def _shared_era5_path_for_window(start_iso: str, end_iso: str) -> Path:
    return _shared_era5_dir() / _era5_file_name(start_iso, end_iso)


def _gtsm_subset_file_name(start_iso: str, end_iso: str) -> str:
    return f"GTSM_subset_{_window_key(start_iso, end_iso)}.nc"


def _storm_track_file_name(start_iso: str, end_iso: str) -> str:
    return f"track_{_window_key(start_iso, end_iso)}.json"


def _shared_gtsm_subset_path_for_window(start_iso: str, end_iso: str) -> Path:
    return _shared_gtsm_dir() / _gtsm_subset_file_name(start_iso, end_iso)


def _shared_storm_track_path_for_window(start_iso: str, end_iso: str) -> Path:
    return _shared_storm_track_dir() / _storm_track_file_name(start_iso, end_iso)

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


def _normalize_storm_type(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in ALLOWED_STORM_TYPES:
        return s
    lowered = s.lower()
    if lowered == "channel rat":
        return "Channel Rat"
    if lowered == "north sea storm":
        return "North Sea Storm"
    return None


def _validate_storm_type_input(value: str | None) -> str | None:
    normalized = _normalize_storm_type(value)
    if normalized is not None:
        return normalized
    if value is None or not str(value).strip():
        return None
    allowed = ", ".join(ALLOWED_STORM_TYPES)
    raise HTTPException(status_code=400, detail=f"Invalid storm_type. Allowed values: {allowed}")


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
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        # Keep absolute paths for externally configured storage roots.
        return str(path)


def _storm_int_from_name(storm_name: str) -> int:
    digits = re.findall(r"\d+", storm_name or "")
    if not digits:
        raise HTTPException(status_code=400, detail=f"Storm name has no numeric id: {storm_name!r}")
    return int(digits[-1])


def _load_storms_metadata(dataset: str | None = None) -> list[dict]:
    _ = dataset
    return _load_master_storms_metadata()


def _load_master_storms_metadata() -> list[dict]:
    payload = _json_load(_master_storms_path())
    if not isinstance(payload, list):
        raise HTTPException(status_code=500, detail="master storms.json must be a JSON list")
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
                "storm_type": _normalize_storm_type(row.get("storm_type")),
                "era5_window": {
                    "start": _normalize_iso_utc(era5_window.get("start")),
                    "end": _normalize_iso_utc(era5_window.get("end")),
                },
                "storm_window": {
                    "start": _normalize_iso_utc(storm_window.get("start")),
                    "end": _normalize_iso_utc(storm_window.get("end")),
                },
                "closures": [
                    {
                        "start": _normalize_iso_utc(c.get("start")),
                        "end": _normalize_iso_utc(c.get("end")),
                        "barrier": str(c.get("barrier") or "").strip().lower(),
                    }
                    for c in closures
                    if isinstance(c, dict)
                ],
            }
        )
    return cleaned


def _save_storms_metadata(rows: list[dict], dataset: str | None = None) -> None:
    _ = dataset
    _save_master_storms_metadata(rows)


def _save_master_storms_metadata(rows: list[dict]) -> None:
    # Persist master storms.json with per-closure barrier metadata.
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("storm"):
            continue
        era5_window = row.get("era5_window") or {}
        storm_window = row.get("storm_window") or {}
        closures = row.get("closures") if isinstance(row.get("closures"), list) else []
        normalized_rows.append(
            {
                "storm": str(row["storm"]),
                "storm_type": _normalize_storm_type(row.get("storm_type")),
                "era5_window": {
                    "start": _normalize_iso_utc(era5_window.get("start")),
                    "end": _normalize_iso_utc(era5_window.get("end")),
                },
                "storm_window": {
                    "start": _normalize_iso_utc(storm_window.get("start")),
                    "end": _normalize_iso_utc(storm_window.get("end")),
                },
                "closures": [
                    {
                        "start": _normalize_iso_utc(c.get("start")),
                        "end": _normalize_iso_utc(c.get("end")),
                        "barrier": str(c.get("barrier") or "").strip().lower(),
                    }
                    for c in closures
                    if isinstance(c, dict)
                ],
            }
        )
    _json_save(_master_storms_path(), normalized_rows)


def _find_storm_meta(storm_id: int, dataset: str | None = None) -> tuple[int, dict]:
    _ = dataset
    rows = _load_master_storms_metadata()
    for idx, row in enumerate(rows):
        try:
            if _storm_int_from_name(row["storm"]) == int(storm_id):
                return idx, row
        except HTTPException:
            continue
    raise HTTPException(status_code=404, detail=f"Storm {storm_id} not found in storms.json")


def _legacy_storm_track_path(storm_id: int, start_iso: str, end_iso: str, dataset: str | None = None) -> Path:
    sid = int(storm_id)
    _ = dataset
    return _storm_root_dir(sid) / "storm_track" / _storm_track_file_name(start_iso, end_iso)


def _resolve_storm_track_path_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    dataset: str | None = None,
) -> Path:
    shared = _shared_storm_track_path_for_window(start_iso, end_iso)
    if shared.exists():
        return shared
    legacy = _legacy_storm_track_path(storm_id, start_iso, end_iso, dataset)
    if legacy.exists():
        return legacy
    return shared


def _storm_track_path(storm_id: int, start_iso: str, end_iso: str, dataset: str | None = None) -> Path:
    _ = (storm_id, dataset)
    return _shared_storm_track_path_for_window(start_iso, end_iso)


def _export_path_for_session(
    session: dict,
    session_id: str,
    product: str,
    fmt: str,
    frame_indices: list[int],
    fps: int,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    storm_id = session.get("storm_id")
    if storm_id is not None:
        base_dir = _storm_root_dir(int(storm_id)) / "exports"
    else:
        base_dir = DATA_ROOT_DIR / "exports" / f"session_{session_id}"
    start_idx = int(frame_indices[0])
    end_idx = int(frame_indices[-1])
    name = f"{product}_{start_idx:04d}_{end_idx:04d}_{fps}fps_{stamp}.{fmt}"
    return base_dir / name


def _serialise_track_for_storage(track: list[dict]) -> list[dict]:
    out = []
    for point in track:
        time_utc = point.get("time_utc")
        lon = point.get("lon")
        lat = point.get("lat")
        pressure_hpa = point.get("pressure_hpa")
        if time_utc is None or lon is None or lat is None:
            continue
        out.append(
            {
                "time_utc": str(time_utc),
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "pressure_hpa": round(float(pressure_hpa), 2) if pressure_hpa is not None else None,
            }
        )
    out.sort(key=lambda p: pd.Timestamp(p["time_utc"]))
    return out


def _normalise_stored_track_for_session(stored_track: list[dict], frame_list: list[tuple[int, pd.Timestamp]]) -> list[dict]:
    if not stored_track or not frame_list:
        return []

    frame_lookup: dict[str, tuple[int, pd.Timestamp]] = {}
    for frame_idx, (_, frame_time) in enumerate(frame_list):
        key = pd.Timestamp(frame_time).strftime("%Y-%m-%d %H:%M:%S")
        frame_lookup[key] = (frame_idx, pd.Timestamp(frame_time))

    normalised = []
    for point in stored_track:
        if not isinstance(point, dict):
            continue
        raw_time = point.get("time_utc")
        if raw_time is None:
            continue
        try:
            ts = pd.Timestamp(raw_time)
        except Exception:
            continue
        key = ts.strftime("%Y-%m-%d %H:%M:%S")
        mapped = frame_lookup.get(key)
        if not mapped:
            continue
        frame_idx, frame_ts = mapped
        lon = point.get("lon")
        lat = point.get("lat")
        if lon is None or lat is None:
            continue
        pressure_hpa = point.get("pressure_hpa")
        normalised.append(
            {
                "time_utc": frame_ts.strftime("%Y-%m-%d %H:%M:%S"),
                "time_index": frame_idx,
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "pressure_hpa": round(float(pressure_hpa), 2) if pressure_hpa is not None else None,
            }
        )
    normalised.sort(key=lambda p: p["time_index"])
    return normalised


def _load_persisted_track(
    storm_id: int,
    start_iso: str | None,
    end_iso: str | None,
    frame_list: list[tuple[int, pd.Timestamp]],
    dataset: str | None = None,
) -> list[dict]:
    if not start_iso or not end_iso:
        return []
    path = _resolve_storm_track_path_for_window(storm_id, start_iso, end_iso, dataset)
    if not path.exists():
        return []
    payload = _json_load(path)
    if not isinstance(payload, list):
        return []
    return _normalise_stored_track_for_session(payload, frame_list)


def _pick_default_window(meta: dict) -> tuple[str | None, str | None]:
    era5_win = meta.get("era5_window") or {}
    return era5_win.get("start"), era5_win.get("end")


def _era5_path_for_window(storm_id: int, start_iso: str, end_iso: str, dataset: str | None = None) -> Path:
    sid = int(storm_id)
    _ = dataset
    return _storm_era5_dir(sid) / _era5_file_name(start_iso, end_iso)


def _parse_era5_filename_to_window(file_name: str) -> tuple[str, str] | None:
    match = re.match(r"^ERA5_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)\.nc$", file_name or "")
    if not match:
        return None
    start_token, end_token = match.group(1), match.group(2)

    def _token_to_iso(token: str) -> str:
        return (
            f"{token[0:4]}-{token[4:6]}-{token[6:8]}"
            f"T{token[9:11]}:{token[11:13]}:{token[13:15]}Z"
        )

    try:
        return _canonical_window(_token_to_iso(start_token), _token_to_iso(end_token))
    except HTTPException:
        return None


def _resolve_era5_path_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    dataset: str | None = None,
) -> Path:
    # Shared-only storage policy.
    return _shared_era5_path_for_window(start_iso, end_iso)


def _legacy_gtsm_subset_path_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    dataset: str | None = None,
) -> Path:
    _ = dataset
    return _legacy_storm_gtsm_dir(storm_id) / _gtsm_subset_file_name(start_iso, end_iso)


def _resolve_gtsm_subset_path_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    dataset: str | None = None,
) -> Path:
    shared = _shared_gtsm_subset_path_for_window(start_iso, end_iso)
    if shared.exists():
        return shared
    legacy = _legacy_gtsm_subset_path_for_window(storm_id, start_iso, end_iso, dataset)
    if legacy.exists():
        return legacy
    return shared


def _gtsm_frame_cache_name(ts: pd.Timestamp) -> str:
    token = _window_token(_normalize_iso_utc(ts.isoformat()) or ts.strftime("%Y%m%dT%H%M%SZ"))
    # Cache version in filename so rendered overlays can evolve safely.
    return f"gtsm_v2_{token}.png"


def _legacy_gtsm_frame_cache_path(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    ts: pd.Timestamp,
    dataset: str | None = None,
) -> Path:
    win_key = _window_key(start_iso, end_iso)
    out_name = _gtsm_frame_cache_name(ts)
    _ = dataset
    return _legacy_storm_gtsm_dir(storm_id) / win_key / out_name


def _shared_gtsm_frame_cache_path(start_iso: str, end_iso: str, ts: pd.Timestamp) -> Path:
    win_key = _window_key(start_iso, end_iso)
    out_name = _gtsm_frame_cache_name(ts)
    return _shared_gtsm_dir() / win_key / out_name


def _resolve_gtsm_frame_cache_path(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    ts: pd.Timestamp,
    dataset: str | None = None,
) -> Path:
    shared = _shared_gtsm_frame_cache_path(start_iso, end_iso, ts)
    if shared.exists():
        return shared
    legacy = _legacy_gtsm_frame_cache_path(storm_id, start_iso, end_iso, ts, dataset)
    if legacy.exists():
        return legacy
    return shared


def _window_overlaps(
    a_start_utc: str | None,
    a_end_utc: str | None,
    b_start_utc: str | None,
    b_end_utc: str | None,
) -> bool:
    if not a_start_utc or not a_end_utc or not b_start_utc or not b_end_utc:
        return False
    a_start = pd.Timestamp(a_start_utc)
    a_end = pd.Timestamp(a_end_utc)
    b_start = pd.Timestamp(b_start_utc)
    b_end = pd.Timestamp(b_end_utc)
    return max(a_start, b_start) <= min(a_end, b_end)


def _storm_closure_window(meta: dict) -> tuple[str | None, str | None]:
    closures = [c for c in (meta.get("closures") or []) if isinstance(c, dict)]
    starts = [pd.Timestamp(c["start"]) for c in closures if c.get("start")]
    ends = [pd.Timestamp(c["end"]) for c in closures if c.get("end")]
    if not starts or not ends:
        return None, None
    return _canonical_window(min(starts).to_pydatetime(), max(ends).to_pydatetime())


def _list_cached_era5_windows_for_storm(
    storm_id: int,
    meta: dict,
    dataset: str | None = None,
) -> list[dict]:
    windows: set[tuple[str, str]] = set()
    base_dir = _shared_era5_dir()
    if base_dir.exists():
        for path in base_dir.glob("ERA5_*.nc"):
            parsed = _parse_era5_filename_to_window(path.name)
            if parsed:
                windows.add(parsed)

    if not windows:
        return []

    default_start, default_end = _pick_default_window(meta)
    closure_start, closure_end = _storm_closure_window(meta)
    filtered = []
    for start_utc, end_utc in windows:
        if not default_start and not default_end and not closure_start and not closure_end:
            filtered.append((start_utc, end_utc))
            continue
        if _window_overlaps(start_utc, end_utc, default_start, default_end):
            filtered.append((start_utc, end_utc))
            continue
        if _window_overlaps(start_utc, end_utc, closure_start, closure_end):
            filtered.append((start_utc, end_utc))
            continue

    filtered.sort(key=lambda r: (r[0], r[1]))
    return [{"start_utc": start_utc, "end_utc": end_utc} for start_utc, end_utc in filtered]


def _migrate_legacy_era5_to_shared() -> None:
    """
    One-time migration for legacy per-storm ERA5 files:
    - Move data/*/storm_*/era5/ERA5_*.nc into data/era5/
    - Keep only one shared copy per ERA5_<window_key>.nc
    - Remove empty legacy era5 directories (and empty storm directories)
    """
    shared_dir = _shared_era5_dir()
    shared_dir.mkdir(parents=True, exist_ok=True)

    legacy_paths = sorted(DATA_ROOT_DIR.glob("*/storm_*/era5/ERA5_*.nc"))
    for src_path in legacy_paths:
        target_path = shared_dir / src_path.name
        if target_path.exists():
            src_size = src_path.stat().st_size
            target_size = target_path.stat().st_size
            if src_size != target_size:
                raise RuntimeError(
                    "ERA5 migration conflict for "
                    f"{src_path.name}: shared file size {target_size} != legacy file size {src_size} "
                    f"({src_path})"
                )
            src_path.unlink(missing_ok=True)
            continue
        src_path.replace(target_path)

    # Cleanup empty legacy era5 directories and empty storm directories.
    for era5_dir in sorted(DATA_ROOT_DIR.glob("*/storm_*/era5")):
        try:
            era5_dir.rmdir()
        except OSError:
            continue
        storm_dir = era5_dir.parent
        try:
            storm_dir.rmdir()
        except OSError:
            continue


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_file_to_shared_or_raise(src_path: Path, target_path: Path, label: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        src_size = src_path.stat().st_size
        target_size = target_path.stat().st_size
        if src_size != target_size:
            raise RuntimeError(
                f"{label} migration conflict for {target_path.name}: "
                f"shared file size {target_size} != legacy file size {src_size} ({src_path})"
            )
        if _sha256_file(src_path) != _sha256_file(target_path):
            raise RuntimeError(
                f"{label} migration conflict for {target_path.name}: "
                f"shared and legacy content differ ({src_path})"
            )
        src_path.unlink(missing_ok=True)
        return
    src_path.replace(target_path)


def _normalize_legacy_gtsm_filename(name: str) -> str:
    mm = GTSM_SUBSET_STORM_FILE_RE.match(name or "")
    if not mm:
        return name
    return f"GTSM_subset_{mm.group(2)}.nc"


def _migrate_legacy_gtsm_to_shared() -> None:
    """
    One-time migration for legacy per-storm GTSM files:
    - Move data/*/storm_*/gtsm/** into data/gtsm/**
    - Keep one shared copy per target file, validating exact content on collisions
    """
    shared_dir = _shared_gtsm_dir()
    shared_dir.mkdir(parents=True, exist_ok=True)

    for gtsm_dir in sorted(DATA_ROOT_DIR.glob("*/storm_*/gtsm")):
        for src_path in sorted(p for p in gtsm_dir.rglob("*") if p.is_file()):
            rel = src_path.relative_to(gtsm_dir)
            rel_parts = list(rel.parts)
            if rel_parts:
                rel_parts[-1] = _normalize_legacy_gtsm_filename(rel_parts[-1])
            target_path = shared_dir / Path(*rel_parts)
            _move_file_to_shared_or_raise(src_path, target_path, label="GTSM")

    for gtsm_dir in sorted(DATA_ROOT_DIR.glob("*/storm_*/gtsm")):
        try:
            gtsm_dir.rmdir()
        except OSError:
            continue


def _migrate_legacy_storm_track_to_shared() -> None:
    """
    One-time migration for legacy per-storm track files:
    - Move data/*/storm_*/storm_track/track_*.json into data/storm_track/
    - Keep one shared copy per track_<window>.json, validating exact content on collisions
    """
    shared_dir = _shared_storm_track_dir()
    shared_dir.mkdir(parents=True, exist_ok=True)

    for track_dir in sorted(DATA_ROOT_DIR.glob("*/storm_*/storm_track")):
        for src_path in sorted(track_dir.glob("track_*.json")):
            target_path = shared_dir / src_path.name
            _move_file_to_shared_or_raise(src_path, target_path, label="storm_track")

    for track_dir in sorted(DATA_ROOT_DIR.glob("*/storm_*/storm_track")):
        try:
            track_dir.rmdir()
        except OSError:
            continue


def _load_water_level_series_all(barrier: str | None = None) -> list[dict]:
    payload = _json_load(_water_level_series_path(barrier))
    if isinstance(payload, list):
        return payload
    raise HTTPException(
        status_code=500,
        detail="water_level_series.json must be a flat JSON list.",
    )


def _storm_series_window(meta: dict, pad_hours: int = WATER_SERIES_PAD_HOURS) -> tuple[str | None, str | None]:
    closures = [c for c in (meta.get("closures") or []) if isinstance(c, dict)]
    starts = [pd.Timestamp(c["start"]) for c in closures if c.get("start")]
    ends = [pd.Timestamp(c["end"]) for c in closures if c.get("end")]
    if starts and ends:
        start = (min(starts) - pd.Timedelta(hours=pad_hours)).to_pydatetime()
        end = (max(ends) + pd.Timedelta(hours=pad_hours)).to_pydatetime()
        return _canonical_window(start, end)

    storm_window = meta.get("storm_window") or {}
    sw_start = _normalize_iso_utc(storm_window.get("start"))
    sw_end = _normalize_iso_utc(storm_window.get("end"))
    if sw_start and sw_end:
        start = (pd.Timestamp(sw_start) - pd.Timedelta(hours=pad_hours)).to_pydatetime()
        end = (pd.Timestamp(sw_end) + pd.Timedelta(hours=pad_hours)).to_pydatetime()
        return _canonical_window(start, end)

    return _pick_default_window(meta)


def _normalized_series_rows(series_rows: list[dict]) -> list[dict]:
    normalized = []
    for p in series_rows:
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
    return normalized


def _slice_flat_series_for_storm(series_rows: list[dict], meta: dict) -> list[dict]:
    normalized = _normalized_series_rows(series_rows)
    if not normalized:
        return []
    win_start, win_end = _storm_series_window(meta)
    if not win_start or not win_end:
        return normalized
    return [p for p in normalized if win_start <= p["time_utc"] <= win_end]


def _series_for_storm(storm_id: int, meta: dict, barrier: str | None = None) -> list[dict]:
    _ = storm_id
    payload = _load_water_level_series_all(barrier)
    sliced = _slice_flat_series_for_storm(payload, meta)
    return sliced


def _get_storm_catalog(barrier: str | None = None) -> list[dict]:
    barrier_key = _normalize_barrier(barrier) if barrier else None
    rows = []
    for storm in _load_master_storms_metadata():
        if barrier_key:
            closures = [c for c in (storm.get("closures") or []) if isinstance(c, dict)]
            if not any(str(c.get("barrier") or "").strip().lower() == barrier_key for c in closures):
                continue
        sid = _storm_int_from_name(storm["storm"])
        default_start, default_end = _pick_default_window(storm)
        closures = storm.get("closures") or []
        c_start = closures[0]["start"] if closures else None
        c_end = closures[-1]["end"] if closures else None
        rows.append(
            {
                "storm_id": sid,
                "storm": storm["storm"],
                "storm_type": storm.get("storm_type"),
                "label": storm["storm"].replace("_", " ").title(),
                "storm_start_utc": c_start,
                "storm_end_utc": c_end,
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


def _get_closure_catalog(barrier: str | None = None) -> list[dict]:
    barrier_key = (barrier or "").strip().lower() or None
    if barrier_key and barrier_key not in SUPPORTED_BARRIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid barrier '{barrier}'. Supported barriers: {', '.join(SUPPORTED_BARRIERS)}",
        )
    rows = []
    for storm in _load_master_storms_metadata():
        master_storm = str(storm.get("storm") or "")
        if not master_storm:
            continue
        try:
            master_storm_id = _storm_int_from_name(master_storm)
        except HTTPException:
            continue
        default_start, default_end = _pick_default_window(storm)
        for c in storm.get("closures") or []:
            if not isinstance(c, dict):
                continue
            closure_barrier = str(c.get("barrier") or "").strip().lower()
            if closure_barrier not in SUPPORTED_BARRIERS:
                continue
            if barrier_key and closure_barrier != barrier_key:
                continue
            c_start = _normalize_iso_utc(c.get("start"))
            c_end = _normalize_iso_utc(c.get("end"))
            if not c_start or not c_end:
                continue
            rows.append(
                {
                    "master_storm": master_storm,
                    "master_storm_id": master_storm_id,
                    "storm_type": storm.get("storm_type"),
                    "closure_start_utc": c_start,
                    "closure_end_utc": c_end,
                    "barrier": closure_barrier,
                    "default_start_utc": default_start,
                    "default_end_utc": default_end,
                }
            )
    rows.sort(key=lambda r: (r["closure_start_utc"], r["closure_end_utc"], r["barrier"]))
    return rows


def _resolve_closure_to_storm(barrier: str, start_utc: str, end_utc: str) -> tuple[int, dict]:
    barrier_key = _normalize_barrier(barrier)
    c_start = _normalize_iso_utc(start_utc)
    c_end = _normalize_iso_utc(end_utc)
    if not c_start or not c_end:
        raise HTTPException(status_code=400, detail="Invalid closure start/end timestamp")

    for row in _load_master_storms_metadata():
        try:
            sid = _storm_int_from_name(str(row.get("storm") or ""))
        except HTTPException:
            continue
        for c in row.get("closures") or []:
            if not isinstance(c, dict):
                continue
            closure_barrier = str(c.get("barrier") or "").strip().lower()
            if (
                closure_barrier == barrier_key
                and _normalize_iso_utc(c.get("start")) == c_start
                and _normalize_iso_utc(c.get("end")) == c_end
            ):
                return sid, row
    raise HTTPException(
        status_code=404,
        detail=f"No storm mapping found for closure {c_start}..{c_end} in barrier '{barrier_key}'",
    )


def _find_master_storm_by_closure(
    barrier: str, closure_start_utc: str, closure_end_utc: str
) -> tuple[int, dict]:
    """Find master storm that contains the given closure. Returns (index, row). Raises 404 if not found."""
    barrier_key = _normalize_barrier(barrier)
    c_start = _normalize_iso_utc(closure_start_utc)
    c_end = _normalize_iso_utc(closure_end_utc)
    if not c_start or not c_end:
        raise HTTPException(status_code=400, detail="Invalid closure start/end timestamp")
    rows = _load_master_storms_metadata()
    for idx, row in enumerate(rows):
        for closure in row.get("closures") or []:
            if not isinstance(closure, dict):
                continue
            if (
                str(closure.get("barrier") or "").strip().lower() == barrier_key
                and _normalize_iso_utc(closure.get("start")) == c_start
                and _normalize_iso_utc(closure.get("end")) == c_end
            ):
                return idx, row
    raise HTTPException(
        status_code=404,
        detail=f"No master storm found for closure {c_start}..{c_end} in barrier '{barrier_key}'",
    )


def _get_storm_type_from_master(
    barrier: str, closure_start_utc: str, closure_end_utc: str
) -> str | None:
    """Return storm_type from master for the given closure, or None if not found."""
    try:
        _, row = _find_master_storm_by_closure(barrier, closure_start_utc, closure_end_utc)
        return row.get("storm_type")
    except HTTPException:
        return None


class StormTypeUpdateBody(BaseModel):
    storm_type: str | None = None
    dataset: str | None = None
    barrier: str | None = None
    closure_start_utc: str | None = None
    closure_end_utc: str | None = None


@app.get("/api/storms")
async def api_storms(barrier: str | None = None, dataset: str | None = None):
    """
    Return storm catalog with suggested ERA5 download windows and
    whether a pre-generated water level plot exists.
    """
    barrier_key = _normalize_barrier(barrier or dataset) if (barrier or dataset) else None
    return {"barrier": barrier_key or "all", "storms": _get_storm_catalog(barrier_key)}


@app.post("/api/storms/{storm_id}/storm-type")
async def api_update_storm_type(storm_id: int, body: StormTypeUpdateBody):
    barrier = _normalize_barrier(body.barrier or body.dataset) if (body.barrier or body.dataset) else None
    closure_start_utc = _normalize_iso_utc(body.closure_start_utc)
    closure_end_utc = _normalize_iso_utc(body.closure_end_utc)
    if not barrier or not closure_start_utc or not closure_end_utc:
        raise HTTPException(
            status_code=400,
            detail="barrier, closure_start_utc, and closure_end_utc are required to update storm type",
        )
    storm_type = _validate_storm_type_input(body.storm_type)
    master_rows = _load_master_storms_metadata()
    idx, m_row = _find_master_storm_by_closure(barrier, closure_start_utc, closure_end_utc)
    master_rows[idx]["storm_type"] = storm_type
    _save_master_storms_metadata(master_rows)
    master_storm_id = _storm_int_from_name(m_row["storm"])
    return {
        "barrier": barrier,
        "storm_id": master_storm_id,
        "storm": m_row.get("storm"),
        "storm_type": storm_type,
    }


@app.get("/api/closures")
async def api_closures(barrier: str | None = None):
    rows = _get_closure_catalog(barrier=barrier)
    return {"closures": rows, "barrier": (barrier or "").strip().lower() or "all"}


@app.get("/api/closures/resolve")
async def api_closure_resolve(
    barrier: str,
    start_utc: str,
    end_utc: str,
):
    storm_id, meta = _resolve_closure_to_storm(barrier=barrier, start_utc=start_utc, end_utc=end_utc)
    default_start, default_end = _pick_default_window(meta)
    storm_type = _get_storm_type_from_master(barrier, start_utc, end_utc)
    return {
        "barrier": _normalize_barrier(barrier),
        "storm_id": int(storm_id),
        "default_start_utc": default_start,
        "default_end_utc": default_end,
        "storm": meta.get("storm"),
        "storm_type": storm_type,
    }


@app.get("/api/storms/{storm_id}/water_level_series")
async def api_storm_water_level_series(
    storm_id: int,
    barrier: str | None = None,
    dataset: str | None = None,
):
    _, meta = _find_storm_meta(storm_id, None)
    closures = [c for c in (meta.get("closures") or []) if isinstance(c, dict) and c.get("start") and c.get("end")]
    selected_barrier = _normalize_barrier(barrier or dataset) if (barrier or dataset) else None
    if not selected_barrier:
        selected_barrier = str((closures[0] if closures else {}).get("barrier") or DEFAULT_BARRIER)
        selected_barrier = _normalize_barrier(selected_barrier)
    normalized = _series_for_storm(storm_id, meta, selected_barrier)
    default_start, default_end = _pick_default_window(meta)
    full_start = normalized[0]["time_utc"] if normalized else default_start
    full_end = normalized[-1]["time_utc"] if normalized else default_end
    available_era5_windows = _list_cached_era5_windows_for_storm(storm_id, meta, selected_barrier)
    default_window_cached = False
    if default_start and default_end:
        default_window_cached = _resolve_era5_path_for_window(
            storm_id,
            default_start,
            default_end,
            selected_barrier,
        ).exists()
    first_closure = closures[0] if closures else {}
    storm_type = None
    if first_closure.get("start") and first_closure.get("end"):
        storm_type = _get_storm_type_from_master(
            str(first_closure.get("barrier") or selected_barrier), first_closure["start"], first_closure["end"]
        )
    return {
        "series": normalized,
        "full_series_start_utc": full_start,
        "full_series_end_utc": full_end,
        "default_start_utc": default_start or full_start,
        "default_end_utc": default_end or full_end,
        "default_window_cached": default_window_cached,
        "available_era5_windows": available_era5_windows,
        "storm_windows": [
            {
                "start_utc": c.get("start"),
                "end_utc": c.get("end"),
                "barrier": str(c.get("barrier") or "").strip().lower() or selected_barrier,
            }
            for c in closures
            if c.get("start") and c.get("end")
        ],
        "storm": meta["storm"],
        "storm_type": storm_type,
        "barrier": selected_barrier,
    }


class TrackAddBody(BaseModel):
    session_id: str
    time_index: int
    lon: float
    lat: float


class TrackDeleteBody(BaseModel):
    session_id: str
    time_index: int = -1


class ExportBody(BaseModel):
    product: Literal["era5", "gtsm", "side_by_side"]
    format: Literal["gif", "mp4"] = "gif"
    fps: int = 4


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
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=1.4, color="white")
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=1.2, color="white")
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
            color="white",
            linewidth=4,
            transform=ccrs.PlateCarree(),
            zorder=10,
        )
        if show_track_points:
            ax.scatter(
                track_lons,
                track_lats,
                c="white",
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


def _create_session_from_era5(
    path: Path,
    storm_id: int | None = None,
    barrier: str | None = None,
    window_start_utc: str | None = None,
    window_end_utc: str | None = None,
    track_window_start_utc: str | None = None,
    track_window_end_utc: str | None = None,
) -> dict:
    barrier_key = _normalize_barrier(barrier) if barrier else DEFAULT_BARRIER
    data = parse_era5(path)
    track_start = track_window_start_utc or window_start_utc
    track_end = track_window_end_utc or window_end_utc
    persisted_track = (
        _load_persisted_track(
            int(storm_id),
            track_start,
            track_end,
            data["frame_list"],
            dataset=barrier_key,
        )
        if storm_id is not None
        else []
    )
    session_id = uuid.uuid4().hex
    sessions[session_id] = {
        "path": path,
        "storm_id": int(storm_id) if storm_id is not None else None,
        "barrier": barrier_key,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        **data,
        "track": persisted_track,
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
        "track": persisted_track,
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
    barrier: str | None = None
    dataset: str | None = None


def _parse_iso_utc(dt_str: str) -> datetime:
    iso_utc = _normalize_iso_utc(dt_str)
    if not iso_utc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {dt_str!r}")
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


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


def _era5_time_name(ds: xr.Dataset) -> str:
    if "valid_time" in ds.coords or "valid_time" in ds.variables:
        return "valid_time"
    if "time" in ds.coords or "time" in ds.variables:
        return "time"
    raise HTTPException(status_code=500, detail="ERA5 dataset has no time coordinate")


def _expected_era5_hourly_times(start_dt: datetime, end_dt: datetime) -> list[datetime]:
    start_hour = pd.Timestamp(start_dt).ceil("h")
    end_hour = pd.Timestamp(end_dt).floor("h")
    if end_hour < start_hour:
        return []
    return [ts.to_pydatetime().replace(tzinfo=None) for ts in pd.date_range(start_hour, end_hour, freq="h")]


def _build_era5_sparse_segments(missing_times: list[datetime]) -> list[dict]:
    grouped: dict[tuple[int, int, int], set[int]] = {}
    for t in missing_times:
        key = (t.year, t.month, t.day)
        grouped.setdefault(key, set()).add(int(t.hour))
    segments = []
    for (year, month, day), hours in sorted(grouped.items()):
        segments.append(
            {
                "year": year,
                "month": month,
                "day": day,
                "hours": sorted(hours),
            }
        )
    return segments


def _shared_cached_era5_paths_for_window(start_iso: str, end_iso: str) -> list[Path]:
    shared_dir = _shared_era5_dir()
    if not shared_dir.exists():
        return []
    paths = []
    for path in shared_dir.glob("ERA5_*.nc"):
        parsed = _parse_era5_filename_to_window(path.name)
        if not parsed:
            continue
        p_start, p_end = parsed
        if _window_overlaps(p_start, p_end, start_iso, end_iso):
            paths.append(path)
    return sorted(paths)


@app.post("/api/storm/start-session")
async def api_start_storm_session(body: StartStormSessionBody):
    """
    Download ERA5 file for the given storm + time window and create a
    StormTracker session bound to that storm_id.

    ERA5 files are stored in a shared cache under data/era5 and reused across storms/sessions.
    """
    storm_id = int(body.storm_id)
    barrier_key = _normalize_barrier(body.barrier or body.dataset)
    t_start = _parse_iso_utc(body.start_utc)
    t_end = _parse_iso_utc(body.end_utc)
    if t_end <= t_start:
        raise HTTPException(status_code=400, detail="end_utc must be after start_utc")
    start_iso, end_iso = _canonical_window(t_start, t_end)
    win_key = _window_key(start_iso, end_iso)

    meta_rows = _load_storms_metadata(None)
    meta_idx, meta_row = _find_storm_meta(storm_id, None)
    era5_path = _resolve_era5_path_for_window(storm_id, start_iso, end_iso, barrier_key)

    if not era5_path.exists():
        try:
            import cdsapi  # type: ignore
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="cdsapi is not installed on the server; cannot download ERA5 automatically.",
            )
        client = cdsapi.Client()
        valid_time_start = np.datetime64(t_start)
        valid_time_end = np.datetime64(t_end)
        expected_times = _expected_era5_hourly_times(t_start, t_end)
        if not expected_times:
            raise HTTPException(status_code=400, detail="Requested ERA5 window contains no full hourly frames.")

        cached_pieces: list[xr.Dataset] = []
        existing_times: set[pd.Timestamp] = set()
        for cached_path in _shared_cached_era5_paths_for_window(start_iso, end_iso):
            if cached_path == era5_path:
                continue
            ds_cached = xr.open_dataset(cached_path)
            try:
                time_name = _era5_time_name(ds_cached)
                sliced = ds_cached.sel({time_name: slice(valid_time_start, valid_time_end)})
                if int(sliced.sizes.get(time_name, 0)) == 0:
                    continue
                loaded = sliced.load()
                for t in pd.to_datetime(loaded[time_name].values):
                    existing_times.add(pd.Timestamp(t).tz_localize(None))
                cached_pieces.append(loaded)
            finally:
                ds_cached.close()

        missing_times = [t for t in expected_times if pd.Timestamp(t) not in existing_times]
        segments = _build_era5_sparse_segments(missing_times)

        downloaded_pieces: list[xr.Dataset] = []
        with tempfile.TemporaryDirectory(prefix="stormtracker_era5_parts_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            part_paths: list[Path] = []
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
                    "day": [f"{seg['day']:02d}"],
                    "time": [f"{h:02d}:00" for h in seg["hours"]],
                    "area": [YB_MET[1], XB_MET[0], YB_MET[0], XB_MET[1]],
                    "format": "netcdf",
                }
                part_path = tmp_path / f"ERA5_part_storm_{storm_id}_{win_key}_{i:02d}.nc"
                client.retrieve("reanalysis-era5-single-levels", request).download(str(part_path))
                part_paths.append(part_path)

            for part_path in part_paths:
                ds_part = xr.open_dataset(part_path)
                try:
                    time_name = _era5_time_name(ds_part)
                    sliced = ds_part.sel({time_name: slice(valid_time_start, valid_time_end)})
                    if int(sliced.sizes.get(time_name, 0)) == 0:
                        continue
                    downloaded_pieces.append(sliced.load())
                finally:
                    ds_part.close()

        all_pieces = cached_pieces + downloaded_pieces
        if not all_pieces:
            raise HTTPException(status_code=500, detail="No ERA5 frames available for requested time range.")

        time_name = _era5_time_name(all_pieces[0])
        try:
            combined = xr.concat(sorted(all_pieces, key=lambda d: d[time_name].values[0]), dim=time_name)
            combined = combined.sortby(time_name)
            tvals = pd.to_datetime(combined[time_name].values)
            _, uniq_idx = np.unique(np.asarray(tvals), return_index=True)
            uniq_idx = sorted(int(i) for i in uniq_idx)
            combined = combined.isel({time_name: uniq_idx})
            combined = combined.sel({time_name: slice(valid_time_start, valid_time_end)})
            present_times = {pd.Timestamp(t).tz_localize(None) for t in pd.to_datetime(combined[time_name].values)}
            missing_after_merge = [t for t in expected_times if pd.Timestamp(t) not in present_times]
            if missing_after_merge:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"ERA5 cache+download does not cover requested window. "
                        f"Missing {len(missing_after_merge)} hourly frame(s)."
                    ),
                )
            era5_path.parent.mkdir(parents=True, exist_ok=True)
            combined.to_netcdf(era5_path)
            combined.close()
        finally:
            for ds_piece in all_pieces:
                ds_piece.close()

    meta_rows[meta_idx]["era5_window"] = {"start": start_iso, "end": end_iso}
    _save_storms_metadata(meta_rows, None)

    track_window = meta_row.get("storm_window") if isinstance(meta_row.get("storm_window"), dict) else {}
    track_window_start = _normalize_iso_utc(track_window.get("start"))
    track_window_end = _normalize_iso_utc(track_window.get("end"))

    session_payload = _create_session_from_era5(
        era5_path,
        storm_id=storm_id,
        barrier=barrier_key,
        window_start_utc=start_iso,
        window_end_utc=end_iso,
        track_window_start_utc=track_window_start,
        track_window_end_utc=track_window_end,
    )
    sid = session_payload["session_id"]
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
    dataset: str | None = None,
) -> Path | None:
    subset_path = _resolve_gtsm_subset_path_for_window(storm_id, start_iso, end_iso, dataset)
    if subset_path.exists():
        return subset_path
    subset_path.parent.mkdir(parents=True, exist_ok=True)

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

    return subset_path


def _render_gtsm_frame_from_subset(
    subset_path: Path,
    ts: pd.Timestamp,
    target_path: Path,
    *,
    time_index: int | None = None,
    total_steps: int | None = None,
) -> bool:
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

        ts_str = pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
        if time_index is not None and total_steps is not None and total_steps > 0:
            label = f"{ts_str} (step {int(time_index) + 1}/{int(total_steps)})"
        else:
            label = ts_str
        ax.text(
            0.01,
            0.99,
            label,
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=11,
            color="white",
            bbox={"facecolor": "black", "alpha": 0.35, "pad": 3, "edgecolor": "none"},
            zorder=20,
        )

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
    subset = _build_gtsm_subset_for_window(
        int(storm_id),
        start_iso,
        end_iso,
        frame_times,
        dataset=session.get("barrier"),
    )
    if subset:
        session["gtsm_subset_path"] = str(subset)
    return subset


def _gtsm_frame_cache_path(session: dict, time_index: int) -> Path:
    frame_list = session.get("frame_list") or []
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    storm_id = session.get("storm_id")
    if storm_id is None:
        raise HTTPException(status_code=400, detail="GTSM export requires a storm session")
    _, ts = frame_list[time_index]
    ts_pd = pd.Timestamp(ts)
    start_iso = session.get("window_start_utc")
    end_iso = session.get("window_end_utc")
    if not start_iso or not end_iso:
        start_iso = _normalize_iso_utc(pd.Timestamp(frame_list[0][1]).isoformat())
        end_iso = _normalize_iso_utc(pd.Timestamp(frame_list[-1][1]).isoformat())
    if not start_iso or not end_iso:
        raise HTTPException(status_code=500, detail="Unable to resolve session window for GTSM")
    return _resolve_gtsm_frame_cache_path(
        int(storm_id),
        start_iso,
        end_iso,
        ts_pd,
        dataset=session.get("barrier"),
    )


def _resolve_export_indices(session: dict, product: str, pad_frames: int = 3) -> list[int]:
    start_idx, end_idx = _labelled_window_indices(session, pad_hours=0)
    frame_count = len(session.get("frame_list") or [])
    if frame_count == 0:
        raise HTTPException(status_code=400, detail="No session frames available")
    base_indices = list(range(start_idx, end_idx + 1))
    if not base_indices:
        raise HTTPException(status_code=400, detail="No frames available in labelled window")
    ext_start = max(0, start_idx - int(max(0, pad_frames)))
    ext_end = min(frame_count - 1, end_idx + int(max(0, pad_frames)))
    ext_indices = list(range(ext_start, ext_end + 1))
    if product not in {"gtsm", "side_by_side"}:
        return ext_indices

    extra_indices = [i for i in ext_indices if i < start_idx or i > end_idx]
    if not extra_indices:
        return ext_indices
    # User requirement: extend by +3 only when those extra GTSM PNGs are already cached.
    if all(_gtsm_frame_cache_path(session, i).exists() for i in extra_indices):
        return ext_indices
    return base_indices


def _gtsm_png_bytes_for_index(session: dict, time_index: int) -> bytes:
    out_path = _gtsm_frame_cache_path(session, time_index)
    if not out_path.exists():
        subset_path = _resolve_path(session.get("gtsm_subset_path"))
        if not subset_path or not subset_path.exists():
            subset_path = _build_session_gtsm_subset(session)
        if not subset_path or not subset_path.exists():
            raise HTTPException(status_code=404, detail="No matching GTSM subset available for this window")
        frame_list = session.get("frame_list") or []
        _, ts = frame_list[time_index]
        rendered = _render_gtsm_frame_from_subset(
            subset_path,
            pd.Timestamp(ts),
            out_path,
            time_index=time_index,
            total_steps=len(frame_list),
        )
        if not rendered or not out_path.exists():
            raise HTTPException(status_code=404, detail="No matching GTSM frame available for this timestamp")
    return out_path.read_bytes()


def _to_rgb_frame(frame: np.ndarray) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.ndim != 3:
        raise HTTPException(status_code=500, detail="Invalid frame dimensions for export")
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
    if arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return arr


def _compose_side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    l = _to_rgb_frame(left)
    r = _to_rgb_frame(right)
    h = max(l.shape[0], r.shape[0])
    w = l.shape[1] + r.shape[1]
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[: l.shape[0], : l.shape[1], :] = l
    out[: r.shape[0], l.shape[1] : l.shape[1] + r.shape[1], :] = r
    return out


def _encode_animation(frames: list[np.ndarray], target_path: Path, fmt: str, fps: int) -> None:
    if not frames:
        raise HTTPException(status_code=400, detail="No frames to export")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fps = int(max(1, min(30, fps)))
    rgb_frames = [_to_rgb_frame(f) for f in frames]
    try:
        if fmt == "gif":
            duration = 1.0 / float(fps)
            imageio.mimsave(target_path, rgb_frames, format="GIF", duration=duration, loop=0)
            return
        if fmt == "mp4":
            with imageio.get_writer(
                target_path,
                fps=fps,
                format="FFMPEG",
                codec="libx264",
                macro_block_size=None,
            ) as writer:
                for frame in rgb_frames:
                    writer.append_data(frame)
            return
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to encode {fmt.upper()} export: {exc}")
    raise HTTPException(status_code=400, detail=f"Unsupported export format: {fmt}")


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
    out_path = _gtsm_frame_cache_path(session, time_index)
    if not out_path.exists():
        subset_path = _resolve_path(session.get("gtsm_subset_path"))
        if not subset_path or not subset_path.exists():
            subset_path = _build_session_gtsm_subset(session)
        if not subset_path or not subset_path.exists():
            raise HTTPException(status_code=404, detail="No matching GTSM subset available for this window")
        rendered = _render_gtsm_frame_from_subset(
            subset_path,
            ts_pd,
            out_path,
            time_index=time_index,
            total_steps=len(frame_list),
        )
        if not rendered:
            raise HTTPException(status_code=404, detail="No matching GTSM frame available for this timestamp")
    return FileResponse(out_path, media_type="image/png")


@app.post("/api/export/{session_id}")
async def api_export_animation(session_id: str, body: ExportBody):
    session = get_session(session_id)
    session["last_access"] = time.time()
    if not (session.get("track") or []):
        raise HTTPException(status_code=400, detail="Track is empty; cannot export labelled window")
    fps = int(max(1, min(30, body.fps)))
    product = str(body.product)
    fmt = str(body.format)
    frame_indices = _resolve_export_indices(session, product, pad_frames=3)
    if not frame_indices:
        raise HTTPException(status_code=400, detail="No frames available for export")

    frames: list[np.ndarray] = []
    for idx in frame_indices:
        if product == "era5":
            era_png = render_frame(session, idx, session.get("track") or [])
            frames.append(imageio.imread(io.BytesIO(era_png), format="png"))
        elif product == "gtsm":
            gtsm_png = _gtsm_png_bytes_for_index(session, idx)
            frames.append(imageio.imread(io.BytesIO(gtsm_png), format="png"))
        elif product == "side_by_side":
            era_png = render_frame(session, idx, session.get("track") or [])
            gtsm_png = _gtsm_png_bytes_for_index(session, idx)
            frames.append(
                _compose_side_by_side(
                    imageio.imread(io.BytesIO(era_png), format="png"),
                    imageio.imread(io.BytesIO(gtsm_png), format="png"),
                )
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported export product: {product}")

    out_path = _export_path_for_session(session, session_id, product, fmt, frame_indices, fps)
    _encode_animation(frames, out_path, fmt, fps)
    media_type = "image/gif" if fmt == "gif" else "video/mp4"
    return FileResponse(out_path, media_type=media_type, filename=out_path.name)


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


def _labelled_window_indices(session: dict, pad_hours: int = 0) -> tuple[int, int]:
    """Return (start_idx, end_idx) in frame_list covering labelled track (optionally padded).

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


@app.post("/api/track/update/{session_id}")
async def api_track_update(session_id: str):
    """
    Persist this session's track under data/storm_track/track_<window>.json and refresh storm_window.
    """
    session = get_session(session_id)
    session["last_access"] = time.time()
    storm_id = session.get("storm_id")
    barrier = session.get("barrier")
    if storm_id is None:
        raise HTTPException(status_code=400, detail="Session is not associated with a storm_id")
    track = session.get("track") or []
    if not track:
        raise HTTPException(status_code=400, detail="Track is empty; nothing to update")

    # Persist derived storm_window = labelled frame range min..max.
    start_idx, end_idx = _labelled_window_indices(session, pad_hours=0)
    frame_list = session["frame_list"]
    _, start_ts = frame_list[start_idx]
    _, end_ts = frame_list[end_idx]
    start_iso = pd.Timestamp(start_ts).to_pydatetime().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end_iso = pd.Timestamp(end_ts).to_pydatetime().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    track_path = _storm_track_path(int(storm_id), start_iso, end_iso, barrier)
    track_path.parent.mkdir(parents=True, exist_ok=True)
    _json_save(track_path, _serialise_track_for_storage(track))
    rows = _load_storms_metadata(None)
    idx, _ = _find_storm_meta(int(storm_id), None)
    rows[idx]["storm_window"] = {"start": start_iso, "end": end_iso}
    _save_storms_metadata(rows, None)
    return {
        "rows": len(track),
        "track_path": _rel_path_str(track_path),
        "storm_window": {"start": start_iso, "end": end_iso},
    }


# Ensure shared data directories exist and migrate legacy storm-local files.
_migrate_legacy_era5_to_shared()
_migrate_legacy_gtsm_to_shared()
_migrate_legacy_storm_track_to_shared()

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
