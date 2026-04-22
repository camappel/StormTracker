"""Centralized environment parsing and shared constants."""

from __future__ import annotations

import os
from pathlib import Path


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


# Project paths
APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
ROOT_DIR = PROJECT_DIR
LEGACY_OBJECTIVE_DIR = PROJECT_DIR.parent / "Objective1"

# Shared service/app constants
WATER_SERIES_PAD_HOURS = 62

# Rendering bounds/constants
XB_MET = [-30, 15]
YB_MET = [40, 70]
INT_SKIP = 3
VMIN, VMAX = 967, 1020
XB_GTSM = [-10, 10]
YB_GTSM = [45, 60]

# Barrier reference markers (lon, lat) used on rendered maps.
# Keep these centralized so both ERA5 and GTSM views stay consistent.
BARRIER_MARKERS = {
    "thames": {"lon": 0.05, "lat": 51.49, "label": "Thames Barrier"},
    "eastern_scheldt": {"lon": 3.70, "lat": 51.62, "label": "Eastern Scheldt"},
}

# Storm tracing tuning knobs
TRACK_ANCHOR_ROLES = ("start", "closure_start", "end")
ANCHOR_SNAP_MAX_DISTANCE_DEG = 2.5
FRAME_CANDIDATE_COUNT = 5
MINIMA_PRESSURE_WEIGHT = 1.0
MINIMA_LABEL_WEIGHT = 0.65
MINIMA_LABEL_DISTANCE_SCALE_DEG = 3.0
MINIMA_LABEL_TIME_DECAY_PER_FRAME = 0.35

# Supported enums
SUPPORTED_BARRIERS = ("eastern_scheldt", "thames")
ALLOWED_STORM_TYPES = ("Channel Rat", "North Sea Storm")

# STORMTRACKER_* env handling
DATA_ROOT_DIR = _env_path("STORMTRACKER_DATA_DIR", PROJECT_DIR / "data")
DEFAULT_BARRIER = os.getenv("STORMTRACKER_DEFAULT_BARRIER", "eastern_scheldt").strip() or "eastern_scheldt"
DEFAULT_CODEC_GTSM_DIRS = [
    PROJECT_DIR / "data" / "codec_gtsm",
    LEGACY_OBJECTIVE_DIR / "2_DATA" / "3_CODEC_GTSM_API",
    LEGACY_OBJECTIVE_DIR / "2_Data" / "3_CODEC_GTSM_API",
]
CODEC_GTSM_DIRS = _env_path_list("STORMTRACKER_CODEC_GTSM_DIRS", DEFAULT_CODEC_GTSM_DIRS)
