#!/usr/bin/env python3
"""
Build Thames Barrier water-level series for StormTracker from raw GESLA data.

Pipeline (aligned with Objective1/3a_ANALYSIS_Python/cloures.qmd):
1) Load Sheerness GESLA station data (prefer extracted file, fallback to zip archive).
2) Keep use_flag == 1 and valid sea levels, then filter to analysis years.
3) Reindex to a 10-minute target grid using nearest-neighbor (15 min tolerance).
4) Run yearly harmonic analysis with pytides and predict astronomical tide.
5) Compute surge = observed water level - predicted tide.
6) Build one flat hourly series and write
   StormTracker/data/thames/water_level_series.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path(os.getenv("STORMTRACKER_DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
DEFAULT_THAMES_DIR = Path(
    os.getenv("STORMTRACKER_THAMES_DATA_DIR", str(DEFAULT_DATA_ROOT / "thames"))
).expanduser()
DEFAULT_OUT_PATH = DEFAULT_THAMES_DIR / "water_level_series.json"

DEFAULT_GESLA_DIR = PROJECT_DIR.parent / "RTides" / "data" / "GESLA4_ALL"
DEFAULT_GESLA_ZIP = PROJECT_DIR.parent / "RTides" / "data" / "GESLA4_ALL.zip"
DEFAULT_STATION = "sheerness-she-gbr-bodc"


def _import_tide_class():
    """
    Import pytides.Tide.

    Falls back to the vendored repository copy at FLOOD-CDT/pytides when the
    environment package is unavailable or broken.
    """
    try:
        from pytides.tide import Tide  # type: ignore

        return Tide
    except Exception:
        local_pytides_root = PROJECT_DIR.parent / "pytides"
        if local_pytides_root.exists():
            sys.path.insert(0, str(local_pytides_root))
            try:
                from pytides.tide import Tide  # type: ignore

                return Tide
            except Exception as exc:
                raise RuntimeError(
                    "Could not import pytides from environment or local repo copy."
                ) from exc
        raise RuntimeError(
            "pytides is required. Install it or ensure FLOOD-CDT/pytides is available."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Thames water_level_series.json from GESLA.")
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUT_PATH)
    parser.add_argument("--gesla-dir", type=Path, default=DEFAULT_GESLA_DIR)
    parser.add_argument("--gesla-zip", type=Path, default=DEFAULT_GESLA_ZIP)
    parser.add_argument("--station", type=str, default=DEFAULT_STATION)
    parser.add_argument("--start-year", type=int, default=1982)
    parser.add_argument("--end-year", type=int, default=2024)
    parser.add_argument("--quality-threshold", type=float, default=60.0)
    parser.add_argument("--min-reference-points", type=int, default=1000)
    return parser.parse_args()


def _iter_gesla_lines(gesla_dir: Path, gesla_zip: Path, station: str):
    station_path = gesla_dir / station
    if station_path.exists():
        with station_path.open("r", encoding="utf-8", errors="ignore") as f:
            yield from f
        return

    if gesla_zip.exists():
        with ZipFile(gesla_zip) as zf:
            if station not in zf.namelist():
                raise FileNotFoundError(
                    f"Station '{station}' not found in zip archive: {gesla_zip}"
                )
            with zf.open(station) as f:
                for raw in f:
                    yield raw.decode("utf-8", errors="ignore")
        return

    raise FileNotFoundError(
        f"Could not find GESLA station data at '{station_path}' or '{gesla_zip}'."
    )


def load_gesla_observed_series(
    gesla_dir: Path,
    gesla_zip: Path,
    station: str,
    start_year: int,
    end_year: int,
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    dates: list[pd.Timestamp] = []
    levels: list[float] = []
    use_flags: list[int] = []

    for line in _iter_gesla_lines(gesla_dir, gesla_zip, station):
        if not line or line.startswith("#"):
            continue
        parts = line.strip().split()
        if len(parts) < 5:
            continue

        date_str, time_str, wl_str, _qc_flag, use_flag_str = parts[:5]
        try:
            dt = pd.Timestamp(datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M:%S"))
            wl = float(wl_str)
            use_flag = int(use_flag_str)
        except Exception:
            continue

        if wl == -99.9999 or abs(wl) > 100:
            wl = float("nan")

        dates.append(dt)
        levels.append(wl)
        use_flags.append(use_flag)

    if not dates:
        raise RuntimeError("No parsable observations found in GESLA input.")

    idx = pd.DatetimeIndex(dates)
    wl = np.asarray(levels, dtype=float)
    uf = np.asarray(use_flags, dtype=int)

    mask_use = uf == 1
    idx = idx[mask_use]
    wl = wl[mask_use]

    period_start = pd.Timestamp(year=start_year, month=1, day=1, hour=0, minute=0, second=0)
    period_end = pd.Timestamp(year=end_year, month=12, day=31, hour=23, minute=59, second=59)
    mask_period = (idx >= period_start) & (idx <= period_end)
    idx = idx[mask_period]
    wl = wl[mask_period]

    if len(idx) == 0:
        raise RuntimeError("No GESLA data in requested analysis period.")

    # Ensure monotonic unique index before reindexing.
    s = pd.Series(wl, index=idx).sort_index()
    s = s[~s.index.duplicated(keep="first")]

    target_index = pd.date_range(
        start=period_start,
        end=pd.Timestamp(year=end_year, month=12, day=31, hour=23, minute=50, second=0),
        freq="10min",
    )

    s_10min = s.reindex(target_index, method="nearest", tolerance=pd.Timedelta(minutes=15))
    return target_index, s_10min.to_numpy(dtype=float)


def predict_tides_with_pytides(
    time_index: pd.DatetimeIndex,
    water_level: np.ndarray,
    start_year: int,
    end_year: int,
    quality_threshold: float,
    min_reference_points: int,
) -> tuple[np.ndarray, np.ndarray]:
    Tide = _import_tide_class()
    years = np.arange(start_year, end_year + 1, dtype=int)
    dq = np.full((len(years), 3), np.nan, dtype=float)  # year, quality, reference_year
    dq[:, 0] = years

    for i, y in enumerate(years):
        y0 = pd.Timestamp(year=y, month=1, day=1)
        y1 = pd.Timestamp(year=y + 1, month=1, day=1)
        mask = (time_index >= y0) & (time_index < y1)
        total = int(np.sum(mask))
        valid = int(np.sum(mask & ~np.isnan(water_level)))
        dq[i, 1] = (100.0 * valid / total) if total else 0.0

    tip = np.full_like(water_level, np.nan, dtype=float)
    for i, y in enumerate(years):
        if dq[i, 1] >= quality_threshold:
            ref_year = int(y)
        else:
            good_mask = dq[:, 1] >= quality_threshold
            if np.any(good_mask):
                candidate_years = dq[good_mask, 0].astype(int)
                ref_year = int(candidate_years[np.argmin(np.abs(candidate_years - y))])
            else:
                ref_year = int(dq[np.argmax(dq[:, 1]), 0])
        dq[i, 2] = ref_year

        ref_start = pd.Timestamp(year=ref_year, month=1, day=1, hour=0, minute=0, second=0)
        ref_end = pd.Timestamp(year=ref_year + 1, month=1, day=2, hour=0, minute=0, second=0)
        ref_mask = (time_index >= ref_start) & (time_index < ref_end)
        if not np.any(ref_mask):
            continue

        ref_times = time_index[ref_mask].to_pydatetime()
        ref_levels = water_level[ref_mask]
        valid_mask = ~np.isnan(ref_levels)
        if int(np.sum(valid_mask)) < min_reference_points:
            continue

        model = Tide.decompose(heights=ref_levels[valid_mask], t=np.asarray(ref_times)[valid_mask])

        pred_start = pd.Timestamp(year=y, month=1, day=1, hour=0, minute=0, second=0)
        pred_end = pd.Timestamp(year=y, month=12, day=31, hour=23, minute=50, second=0)
        pred_mask = (time_index >= pred_start) & (time_index <= pred_end)
        pred_times = time_index[pred_mask].to_pydatetime()
        if len(pred_times) == 0:
            continue

        tip[pred_mask] = np.asarray(model.at(pred_times), dtype=float)

    return tip, dq


def build_flat_water_series(
    time_index: pd.DatetimeIndex,
    water_level: np.ndarray,
    tide: np.ndarray,
    surge: np.ndarray,
) -> list[dict]:
    # Resample to hourly using nearest 10-min sample around each hour.
    # Using pandas reindex here avoids edge-case failures in manual index search.
    hourly = pd.date_range(
        start=pd.Timestamp(time_index[0]).floor("h"),
        end=pd.Timestamp(time_index[-1]).ceil("h"),
        freq="h",
    )
    obs_hourly = pd.Series(water_level, index=time_index).reindex(
        hourly, method="nearest", tolerance=pd.Timedelta(minutes=31)
    )
    tide_hourly = pd.Series(tide, index=time_index).reindex(
        hourly, method="nearest", tolerance=pd.Timedelta(minutes=31)
    )
    surge_hourly = pd.Series(surge, index=time_index).reindex(
        hourly, method="nearest", tolerance=pd.Timedelta(minutes=31)
    )

    series: list[dict] = []
    for t, wl, td, sg in zip(hourly, obs_hourly.to_numpy(), tide_hourly.to_numpy(), surge_hourly.to_numpy()):

        series.append(
            {
                "time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "water_level": None if np.isnan(wl) else float(wl),
                "surge": None if np.isnan(sg) else float(sg),
                "tide": None if np.isnan(td) else float(td),
            }
        )
    return series


def main() -> int:
    args = parse_args()

    time_index, water_level = load_gesla_observed_series(
        gesla_dir=args.gesla_dir,
        gesla_zip=args.gesla_zip,
        station=args.station,
        start_year=args.start_year,
        end_year=args.end_year,
    )
    print(f"Loaded and reindexed observed series: {len(time_index):,} points")
    print(f"  Start: {time_index[0]}")
    print(f"  End:   {time_index[-1]}")
    print(f"  Valid observed points: {int(np.sum(~np.isnan(water_level))):,}")

    tide, dq = predict_tides_with_pytides(
        time_index=time_index,
        water_level=water_level,
        start_year=args.start_year,
        end_year=args.end_year,
        quality_threshold=args.quality_threshold,
        min_reference_points=args.min_reference_points,
    )
    surge = water_level - tide
    print(f"Predicted tide points: {int(np.sum(~np.isnan(tide))):,}")
    print(f"Computed surge points: {int(np.sum(~np.isnan(surge))):,}")
    print(
        f"Years meeting quality threshold ({args.quality_threshold:g}%): "
        f"{int(np.sum(dq[:, 1] >= args.quality_threshold))}/{len(dq)}"
    )

    payload = build_flat_water_series(
        time_index=time_index,
        water_level=water_level,
        tide=tide,
        surge=surge,
    )
    unique_times = len({row["time_utc"] for row in payload})
    if unique_times <= 1 and len(payload) > 1:
        raise RuntimeError(
            "Generated output has <=1 unique timestamp; aborting write to avoid corrupt series."
        )

    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
