#!/usr/bin/env python3
"""
Bootstrap storms metadata and water-level series for StormTracker.

Generates:
- StormTracker/data/eastern_scheldt/storms.json
- StormTracker/data/eastern_scheldt/water_level_series.json (flat full time series)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_ANALYSIS_OUTPUT_DIR = PROJECT_DIR.parent / "Objective1" / "3a_ANALYSIS_Python" / "output"
ANALYSIS_OUTPUT_DIR = Path(
    os.getenv(
        "STORMTRACKER_ANALYSIS_OUTPUT_DIR",
        str(LEGACY_ANALYSIS_OUTPUT_DIR if LEGACY_ANALYSIS_OUTPUT_DIR.exists() else PROJECT_DIR / "data"),
    )
).expanduser()
MAST1_PATH = ANALYSIS_OUTPUT_DIR / "mast1.pkl"
DEFAULT_DATA_ROOT = Path(os.getenv("STORMTRACKER_DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
DATA_DIR = Path(
    os.getenv("STORMTRACKER_EASTERN_SCHELDT_DATA_DIR", str(DEFAULT_DATA_ROOT / "eastern_scheldt"))
).expanduser()
STORMS_PATH = DATA_DIR / "storms.json"
WATER_LEVEL_SERIES_PATH = DATA_DIR / "water_level_series.json"


def amsterdam_to_utc_iso(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tz is None:
        t = t.tz_localize("Europe/Amsterdam", ambiguous=False)
    t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    if not MAST1_PATH.exists():
        raise FileNotFoundError(f"mast1.pkl not found at: {MAST1_PATH}")

    payload = pd.read_pickle(MAST1_PATH)
    storm_df = payload["storm_df"]
    tsp = pd.to_datetime(np.asarray(payload["TSP"]))
    wlp = np.asarray(payload["WLP"], dtype=float)
    sup = np.asarray(payload["SUP"], dtype=float)
    tip = np.asarray(payload["TIP"], dtype=float) if "TIP" in payload else np.full_like(wlp, np.nan)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storms = []
    for sid in sorted(storm_df["Storm"].unique()):
        group = storm_df[storm_df["Storm"] == sid].copy()
        group["Start of Closure"] = pd.to_datetime(group["Start of Closure"])
        group["End of Closure"] = pd.to_datetime(group["End of Closure"])

        closures = []
        for _, row in group.iterrows():
            c_start = pd.Timestamp(row["Start of Closure"])
            c_end = pd.Timestamp(row["End of Closure"]) if pd.notna(row["End of Closure"]) else c_start + pd.Timedelta(days=1)
            closures.append({"start": amsterdam_to_utc_iso(c_start), "end": amsterdam_to_utc_iso(c_end)})

        closures = sorted(closures, key=lambda c: c["start"])
        first_start = closures[0]["start"] if closures else None
        last_end = closures[-1]["end"] if closures else None
        default_start = (
            (pd.Timestamp(first_start).tz_convert("UTC") - pd.Timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if first_start
            else None
        )
        default_end = (
            (pd.Timestamp(last_end).tz_convert("UTC") + pd.Timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if last_end
            else None
        )
        storm_name = f"storm_{int(sid)}"
        storms.append(
            {
                "storm": storm_name,
                "storm_type": None,
                "era5_window": {"start": default_start, "end": default_end},
                "storm_window": {"start": None, "end": None},
                "closures": closures,
            }
        )

    water = []
    for i, t in enumerate(tsp):
        water.append(
            {
                "time_utc": amsterdam_to_utc_iso(pd.Timestamp(t)),
                "water_level": float(wlp[i]) if not np.isnan(wlp[i]) else None,
                "surge": float(sup[i]) if not np.isnan(sup[i]) else None,
                "tide": float(tip[i]) if not np.isnan(tip[i]) else None,
            }
        )

    STORMS_PATH.write_text(json.dumps(storms, indent=2), encoding="utf-8")
    WATER_LEVEL_SERIES_PATH.write_text(json.dumps(water, indent=2), encoding="utf-8")
    print(f"Wrote {STORMS_PATH}")
    print(f"Wrote {WATER_LEVEL_SERIES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
