"""
StormTracker ERA5 Web App — FastAPI backend.
Upload ERA5 NetCDF, step through time, draw storm track by clicking, update persisted track data.
"""
import json
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from services.era5_service import (
    create_session_from_era5 as era5_create_session_from_era5,
    ensure_era5_window_cached,
    parse_iso_utc as era5_parse_iso_utc,
    shared_cached_era5_paths_for_window as era5_shared_cached_paths,
)
from services.gtsm_service import (
    build_gtsm_subset_for_window as gtsm_build_subset_for_window,
)
from services.export_service import (
    build_export_frames,
    encode_animation,
    resolve_export_indices,
)
from services.rendering_service import (
    interpolate_pressure,
    render_frame,
    render_gtsm_frame_from_subset,
)
from config import (
    ALLOWED_STORM_TYPES,
    CODEC_GTSM_DIRS,
    DATA_ROOT_DIR,
    DEFAULT_BARRIER,
    ROOT_DIR,
    SESSION_TTL,
    SUPPORTED_BARRIERS,
    WATER_SERIES_PAD_HOURS,
    XB_MET,
    YB_MET,
)
import storm_metadata as sm
from stormtracer import (
    FRAME_CANDIDATE_COUNT,
    anchor_snap_to_nearest_local_minimum,
    labelled_window_indices,
    local_minima_candidates,
    marker_anchors_from_track_or_422,
    set_track_anchor,
    track_segment_between_anchors,
)
def _normalize_barrier(barrier: str | None) -> str:
    resolved = (barrier or DEFAULT_BARRIER).strip().lower()
    if resolved not in SUPPORTED_BARRIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid barrier '{barrier}'. Supported barriers: {', '.join(SUPPORTED_BARRIERS)}",
        )
    return resolved


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
    return sm.normalize_storm_type(value, ALLOWED_STORM_TYPES)


def _validate_storm_type_input(value: str | None) -> str | None:
    return sm.validate_storm_type_input(value, ALLOWED_STORM_TYPES)


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
    return sm.storm_int_from_name(storm_name)


def _load_storms_metadata(dataset: str | None = None) -> list[dict]:
    _ = dataset
    return _load_master_storms_metadata()


def _load_master_storms_metadata() -> list[dict]:
    return sm.load_master_storms_metadata(
        json_load=_json_load,
        master_storms_path=_master_storms_path,
        normalize_storm_type_cb=_normalize_storm_type,
        normalize_iso_utc=_normalize_iso_utc,
    )


def _save_storms_metadata(rows: list[dict], dataset: str | None = None) -> None:
    _ = dataset
    _save_master_storms_metadata(rows)


def _save_master_storms_metadata(rows: list[dict]) -> None:
    sm.save_master_storms_metadata(
        rows=rows,
        json_save=_json_save,
        master_storms_path=_master_storms_path,
        normalize_storm_type_cb=_normalize_storm_type,
        normalize_iso_utc=_normalize_iso_utc,
    )


def _find_storm_meta(storm_id: int, dataset: str | None = None) -> tuple[int, dict]:
    _ = dataset
    return sm.find_storm_meta(
        storm_id=storm_id,
        load_master_storms_metadata_cb=_load_master_storms_metadata,
        storm_int_from_name_cb=_storm_int_from_name,
    )


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


def _load_water_level_series_all(barrier: str | None = None) -> list[dict]:
    return sm.load_water_level_series_all(
        json_load=_json_load,
        water_level_series_path=_water_level_series_path,
        barrier=barrier,
    )

def _series_for_storm_with_window(
    storm_id: int,
    meta: dict,
    barrier: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> list[dict]:
    return sm.series_for_storm(
        storm_id=storm_id,
        meta=meta,
        barrier=barrier,
        load_water_level_series_all_cb=_load_water_level_series_all,
        normalize_iso_utc=_normalize_iso_utc,
        canonical_window=_canonical_window,
        pick_default_window=_pick_default_window,
        pad_hours=WATER_SERIES_PAD_HOURS,
        requested_start_utc=start_utc,
        requested_end_utc=end_utc,
    )


def _get_storm_catalog(barrier: str | None = None) -> list[dict]:
    return sm.get_storm_catalog(
        barrier=barrier,
        normalize_barrier=_normalize_barrier,
        load_master_storms_metadata_cb=_load_master_storms_metadata,
        storm_int_from_name_cb=_storm_int_from_name,
        pick_default_window=_pick_default_window,
    )


def _get_closure_catalog(barrier: str | None = None) -> list[dict]:
    return sm.get_closure_catalog(
        barrier=barrier,
        supported_barriers=SUPPORTED_BARRIERS,
        load_master_storms_metadata_cb=_load_master_storms_metadata,
        storm_int_from_name_cb=_storm_int_from_name,
        pick_default_window=_pick_default_window,
        normalize_iso_utc=_normalize_iso_utc,
    )


def _resolve_closure_to_storm(barrier: str, start_utc: str, end_utc: str) -> tuple[int, dict]:
    return sm.resolve_closure_to_storm(
        barrier=barrier,
        start_utc=start_utc,
        end_utc=end_utc,
        normalize_barrier=_normalize_barrier,
        normalize_iso_utc=_normalize_iso_utc,
        load_master_storms_metadata_cb=_load_master_storms_metadata,
        storm_int_from_name_cb=_storm_int_from_name,
    )


def _find_master_storm_by_closure(
    barrier: str, closure_start_utc: str, closure_end_utc: str
) -> tuple[int, dict]:
    return sm.find_master_storm_by_closure(
        barrier=barrier,
        closure_start_utc=closure_start_utc,
        closure_end_utc=closure_end_utc,
        normalize_barrier=_normalize_barrier,
        normalize_iso_utc=_normalize_iso_utc,
        load_master_storms_metadata_cb=_load_master_storms_metadata,
    )


def _get_storm_type_from_master(
    barrier: str, closure_start_utc: str, closure_end_utc: str
) -> str | None:
    return sm.get_storm_type_from_master(
        barrier=barrier,
        closure_start_utc=closure_start_utc,
        closure_end_utc=closure_end_utc,
        find_master_storm_by_closure_cb=_find_master_storm_by_closure,
    )


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
    start_utc: str | None = None,
    end_utc: str | None = None,
):
    _, meta = _find_storm_meta(storm_id, None)
    closures = [c for c in (meta.get("closures") or []) if isinstance(c, dict) and c.get("start") and c.get("end")]
    selected_barrier = _normalize_barrier(barrier or dataset) if (barrier or dataset) else None
    if not selected_barrier:
        selected_barrier = str((closures[0] if closures else {}).get("barrier") or DEFAULT_BARRIER)
        selected_barrier = _normalize_barrier(selected_barrier)
    req_start = _normalize_iso_utc(start_utc)
    req_end = _normalize_iso_utc(end_utc)
    if bool(req_start) != bool(req_end):
        raise HTTPException(status_code=400, detail="start_utc and end_utc must be provided together")
    if req_start and req_end and pd.Timestamp(req_end) <= pd.Timestamp(req_start):
        raise HTTPException(status_code=400, detail="end_utc must be after start_utc")
    normalized = _series_for_storm_with_window(
        storm_id,
        meta,
        selected_barrier,
        req_start,
        req_end,
    )
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
        "default_start_utc": default_start,
        "default_end_utc": default_end,
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


class TrackAnchorSetBody(BaseModel):
    session_id: str
    role: str
    lon: float
    lat: float
    time_index: int | None = None


class ExportBody(BaseModel):
    product: Literal["era5", "gtsm", "side_by_side"]
    format: Literal["gif", "mp4"] = "gif"
    fps: int = 4


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
    closure_start_utc: str | None = None,
) -> dict:
    return era5_create_session_from_era5(
        path=path,
        storm_id=storm_id,
        barrier=barrier,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        track_window_start_utc=track_window_start_utc,
        track_window_end_utc=track_window_end_utc,
        closure_start_utc=closure_start_utc,
        normalize_barrier=_normalize_barrier,
        default_barrier=DEFAULT_BARRIER,
        load_persisted_track=_load_persisted_track,
        normalize_iso_utc=_normalize_iso_utc,
        sessions=sessions,
        codec_gtsm_dirs=CODEC_GTSM_DIRS,
        xb_met=XB_MET,
        yb_met=YB_MET,
    )


class StartStormSessionBody(BaseModel):
    storm_id: int
    start_utc: str
    end_utc: str
    barrier: str | None = None
    dataset: str | None = None
    closure_start_utc: str | None = None


def _parse_iso_utc(dt_str: str) -> datetime:
    return era5_parse_iso_utc(dt_str, _normalize_iso_utc)


def _shared_cached_era5_paths_for_window(start_iso: str, end_iso: str) -> list[Path]:
    return era5_shared_cached_paths(
        start_iso,
        end_iso,
        _shared_era5_dir(),
        _parse_era5_filename_to_window,
        _window_overlaps,
    )


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

    ensure_era5_window_cached(
        storm_id=storm_id,
        t_start=t_start,
        t_end=t_end,
        start_iso=start_iso,
        end_iso=end_iso,
        win_key=win_key,
        era5_path=era5_path,
        xb_met=XB_MET,
        yb_met=YB_MET,
        shared_cached_paths=_shared_cached_era5_paths_for_window(start_iso, end_iso),
    )

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
        closure_start_utc=body.closure_start_utc,
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


@app.get("/api/track/candidates/{session_id}/{time_index}")
async def api_track_candidates(session_id: str, time_index: int):
    session = get_session(session_id)
    session["last_access"] = time.time()
    frame_list = session.get("frame_list") or []
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    _, grid_time = frame_list[time_index]
    candidates = local_minima_candidates(session, time_index, max_candidates=FRAME_CANDIDATE_COUNT)
    return {
        "session_id": session_id,
        "time_index": int(time_index),
        "time_utc": pd.Timestamp(grid_time).strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": candidates,
        "count": len(candidates),
    }


def _build_gtsm_subset_for_window(
    storm_id: int,
    start_iso: str,
    end_iso: str,
    frame_times: list[pd.Timestamp],
    dataset: str | None = None,
) -> Path | None:
    return gtsm_build_subset_for_window(
        storm_id=storm_id,
        start_iso=start_iso,
        end_iso=end_iso,
        frame_times=frame_times,
        dataset=dataset,
        codec_gtsm_dirs=CODEC_GTSM_DIRS,
        resolve_subset_path_for_window=_resolve_gtsm_subset_path_for_window,
    )


def _render_gtsm_frame_from_subset(
    subset_path: Path,
    ts: pd.Timestamp,
    *,
    time_index: int | None = None,
    total_steps: int | None = None,
) -> bytes | None:
    return render_gtsm_frame_from_subset(
        subset_path,
        ts,
        time_index=time_index,
        total_steps=total_steps,
    )


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


def _gtsm_png_bytes_for_index(session: dict, time_index: int) -> bytes:
    frame_list = session.get("frame_list") or []
    if time_index < 0 or time_index >= len(frame_list):
        raise HTTPException(status_code=404, detail="Invalid time_index")
    subset_path = _resolve_path(session.get("gtsm_subset_path"))
    if not subset_path or not subset_path.exists():
        subset_path = _build_session_gtsm_subset(session)
    if not subset_path or not subset_path.exists():
        raise HTTPException(status_code=404, detail="No matching GTSM subset available for this window")
    _, ts = frame_list[time_index]
    png_bytes = _render_gtsm_frame_from_subset(
        subset_path,
        pd.Timestamp(ts),
        time_index=time_index,
        total_steps=len(frame_list),
    )
    if not png_bytes:
        raise HTTPException(status_code=404, detail="No matching GTSM frame available for this timestamp")
    return png_bytes


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
    png_bytes = _gtsm_png_bytes_for_index(session, time_index)
    return Response(content=png_bytes, media_type="image/png")


@app.post("/api/export/{session_id}")
async def api_export_animation(session_id: str, body: ExportBody):
    session = get_session(session_id)
    session["last_access"] = time.time()
    if not (session.get("track") or []):
        raise HTTPException(status_code=400, detail="Track is empty; cannot export labelled window")
    fps = int(max(1, min(30, body.fps)))
    product = str(body.product)
    fmt = str(body.format)
    frame_indices = resolve_export_indices(
        session,
        product,
        pad_frames=3,
        labelled_window_indices=labelled_window_indices,
    )
    if not frame_indices:
        raise HTTPException(status_code=400, detail="No frames available for export")
    frames = build_export_frames(
        session=session,
        frame_indices=frame_indices,
        product=product,
        render_frame_png=render_frame,
        gtsm_png_bytes_for_index=_gtsm_png_bytes_for_index,
    )

    out_path = _export_path_for_session(session, session_id, product, fmt, frame_indices, fps)
    encode_animation(frames, out_path, fmt, fps)
    media_type = "image/gif" if fmt == "gif" else "video/mp4"
    return FileResponse(out_path, media_type=media_type, filename=out_path.name)


@app.get("/api/track/{session_id}")
async def api_get_track(session_id: str):
    session = get_session(session_id)
    session["last_access"] = time.time()
    return {"track": session["track"]}


@app.get("/api/track/anchors/{session_id}")
async def api_get_track_anchors(session_id: str):
    session = get_session(session_id)
    session["last_access"] = time.time()
    return {"track_anchors": session.get("track_anchors") or {}}


@app.post("/api/track/anchors/set")
async def api_set_track_anchor(body: TrackAnchorSetBody):
    session = get_session(body.session_id)
    session["last_access"] = time.time()
    anchor, snap = set_track_anchor(
        session,
        role=body.role,
        lon=float(body.lon),
        lat=float(body.lat),
        time_index=body.time_index,
    )
    return {
        "track_anchors": session.get("track_anchors") or {},
        "anchor": anchor,
        "snap": snap,
    }


@app.post("/api/track/autolabel/{session_id}")
async def api_track_autolabel(session_id: str):
    session = get_session(session_id)
    session["last_access"] = time.time()
    marker_anchors = marker_anchors_from_track_or_422(session)
    full_track = [marker_anchors[0]]
    for end_anchor in marker_anchors[1:]:
        start_anchor = full_track[-1]
        segment = track_segment_between_anchors(session, start_anchor, end_anchor)
        full_track.extend(segment[1:])
    full_track.sort(key=lambda p: int(p["time_index"]))
    session["track"] = full_track
    return {"track": full_track}


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
    snap = anchor_snap_to_nearest_local_minimum(session, time_index, float(lon), float(lat))
    snapped_lon = float((snap.get("snapped") or {}).get("lon", lon))
    snapped_lat = float((snap.get("snapped") or {}).get("lat", lat))
    _, grid_time = frame_list[time_index]
    pressure_hpa = interpolate_pressure(session, time_index, snapped_lon, snapped_lat)
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
        "lon": round(float(snapped_lon), 6),
        "lat": round(float(snapped_lat), 6),
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
    start_idx, end_idx = labelled_window_indices(session, pad_hours=0)
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
