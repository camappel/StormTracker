#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Set, Tuple

DATASET = "sis-water-level-change-timeseries-cmip6"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
REPO_DIR = PROJECT_DIR.parent


def _ym_from_iso(iso_ts: str | None) -> Tuple[int, int] | None:
    if not iso_ts:
        return None
    # Expected forms like 2021-01-30T00:00:00Z
    try:
        y = int(iso_ts[0:4])
        m = int(iso_ts[5:7])
        if 1 <= m <= 12:
            return y, m
    except Exception:
        pass
    return None


def collect_needed_months(storms_path: Path) -> list[Tuple[int, int]]:
    storms = json.loads(storms_path.read_text(encoding="utf-8"))
    months: Set[Tuple[int, int]] = set()

    for storm in storms:
        # Primary source: closures
        for c in storm.get("closures", []):
            ym = _ym_from_iso(c.get("start"))
            if ym:
                months.add(ym)
            ym = _ym_from_iso(c.get("end"))
            if ym:
                months.add(ym)

        # Safety fallback: era5_window, if closures incomplete
        era5 = storm.get("era5_window", {}) or {}
        ym = _ym_from_iso(era5.get("start"))
        if ym:
            months.add(ym)
        ym = _ym_from_iso(era5.get("end"))
        if ym:
            months.add(ym)

    return sorted(months)


def ensure_extracted(nc_path: Path) -> Path:
    with nc_path.open("rb") as f:
        magic = f.read(4)

    # Normal netcdf starts with CDF; CDS sometimes returns zip in .nc file
    if magic != b"PK\x03\x04":
        return nc_path

    extract_dir = nc_path.parent / f"{nc_path.stem}_extracted"
    marker = extract_dir / ".extracted_ok"
    if not marker.exists():
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(nc_path, "r") as zf:
            zf.extractall(extract_dir)
        marker.write_text("ok", encoding="utf-8")

    nc_files = sorted(extract_dir.rglob("*.nc"))
    if not nc_files:
        raise RuntimeError(f"No .nc found after extracting: {nc_path}")
    return nc_files[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download all CODEC GTSM months needed for Thames storms."
    )
    parser.add_argument(
        "--storms-path",
        type=Path,
        default=PROJECT_DIR / "data" / "thames" / "storms.json",
        help="Path to Thames storms.json",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_DIR / "Objective1" / "2_DATA" / "3_CODEC_GTSM_API",
        help="Directory to store GTSM_YYYY_MM.nc files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print required months and missing files",
    )
    args = parser.parse_args()

    storms_path = args.storms_path.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    if not storms_path.exists():
        raise FileNotFoundError(
            f"Storm metadata file not found: {storms_path}. "
            "Pass --storms-path explicitly if your data is elsewhere."
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    needed = collect_needed_months(storms_path)
    print(f"Needed year-month pairs: {len(needed)}")

    missing = []
    for y, m in needed:
        p = out_dir / f"GTSM_{y}_{m:02d}.nc"
        if not p.exists():
            missing.append((y, m))

    print(f"Already present: {len(needed) - len(missing)}")
    print(f"Missing: {len(missing)}")
    if missing:
        print("Missing list:", ", ".join(f"{y}-{m:02d}" for y, m in missing))

    if args.dry_run or not missing:
        return 0

    import cdsapi

    client = cdsapi.Client()

    for y, m in missing:
        target = out_dir / f"GTSM_{y}_{m:02d}.nc"
        request = {
            "variable": ["storm_surge_residual"],
            "experiment": "reanalysis",
            "temporal_aggregation": ["10_min"],
            "year": [str(y)],
            "month": [f"{m:02d}"],
            "version": ["v3"],
        }
        print(f"Downloading {y}-{m:02d} -> {target.name}")
        client.retrieve(DATASET, request).download(target=str(target))

        openable = ensure_extracted(target)
        print(f"  openable file: {openable}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())