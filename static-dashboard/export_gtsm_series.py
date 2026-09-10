#!/usr/bin/env python3
"""Export cached GTSM nearest-gauge surge series for the static dashboard."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd


STATIC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = STATIC_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
WEBAPP_DIR = PROJECT_DIR / "webapp"
WINDOW_RE = re.compile(r"(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)")
GAUGE_LABELS = {
    "southend": "Southend",
    "rpbu": "Roompot Buiten",
}

MPLCONFIG_DIR = Path(tempfile.gettempdir()) / "stormtracker_matplotlib"
MPLCONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(MPLCONFIG_DIR))

sys.path.insert(0, str(WEBAPP_DIR))

from services.rendering_service import extract_gtsm_gauge_series  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def normalize_iso(value) -> str | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(str(value).strip())
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def token_to_iso(token: str) -> str:
    return f"{token[0:4]}-{token[4:6]}-{token[6:8]}T{token[9:11]}:{token[11:13]}:{token[13:15]}Z"


def iso_token(value: str) -> str:
    iso = normalize_iso(value)
    if not iso:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return iso.replace("-", "").replace(":", "")


def window_key(start_iso: str, end_iso: str) -> str:
    return f"{iso_token(start_iso)}_{iso_token(end_iso)}"


def storm_id_from_name(name: str) -> int | None:
    match = re.search(r"(\d+)$", str(name or ""))
    return int(match.group(1)) if match else None


def pick_window(storm: dict) -> tuple[str | None, str | None]:
    for key in ("storm_window", "era5_window"):
        window = storm.get(key) if isinstance(storm.get(key), dict) else {}
        start = normalize_iso(window.get("start"))
        end = normalize_iso(window.get("end"))
        if start and end:
            return start, end
    starts = []
    ends = []
    for closure in storm.get("closures") or []:
        start = normalize_iso(closure.get("start"))
        end = normalize_iso(closure.get("end"))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None)


def parse_window_from_name(path: Path) -> tuple[str, str] | None:
    match = WINDOW_RE.search(path.name)
    if not match:
        return None
    return token_to_iso(match.group(1)), token_to_iso(match.group(2))


def overlap_score(a_start: str, a_end: str, b_start: str, b_end: str) -> float:
    a0, a1 = pd.Timestamp(a_start), pd.Timestamp(a_end)
    b0, b1 = pd.Timestamp(b_start), pd.Timestamp(b_end)
    inter = (min(a1, b1) - max(a0, b0)).total_seconds()
    if inter <= 0:
        return 0.0
    union = (max(a1, b1) - min(a0, b0)).total_seconds()
    return inter / union if union > 0 else 0.0


def find_gtsm_subset(data_dir: Path, start_iso: str, end_iso: str) -> tuple[Path | None, float]:
    directory = data_dir / "gtsm"
    exact = directory / f"GTSM_subset_{window_key(start_iso, end_iso)}.nc"
    if exact.exists():
        return exact, 1.0
    best_path = None
    best_score = 0.0
    for candidate in directory.glob("GTSM_subset_*.nc"):
        parsed = parse_window_from_name(candidate)
        if not parsed:
            continue
        score = overlap_score(start_iso, end_iso, parsed[0], parsed[1])
        if score > best_score:
            best_score = score
            best_path = candidate
    return best_path, best_score


def round_or_null(value, digits: int):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return round(number, digits)


def pack_gauge_rows(rows: list[dict]) -> list[list]:
    points = []
    for row in rows or []:
        time_utc = normalize_iso(row.get("time_utc") or row.get("time"))
        if not time_utc:
            continue
        points.append([time_utc, round_or_null(row.get("surge"), 3)])
    return points


def summarize_points(points: list[list]) -> dict:
    stats = {"point_count": len(points), "max_surge": None, "max_surge_time": None, "min_surge": None}
    for time_utc, value in points:
        if value is None:
            continue
        if stats["max_surge"] is None or value > stats["max_surge"]:
            stats["max_surge"] = value
            stats["max_surge_time"] = time_utc
        if stats["min_surge"] is None or value < stats["min_surge"]:
            stats["min_surge"] = value
    return stats


def export_storm(storm: dict, data_dir: Path) -> tuple[int | None, dict]:
    storm_id = storm_id_from_name(storm.get("storm"))
    start_iso, end_iso = pick_window(storm)
    if storm_id is None or not start_iso or not end_iso:
        return storm_id, {"status": "skipped", "reason": "missing storm window"}
    subset_path, score = find_gtsm_subset(data_dir, start_iso, end_iso)
    if not subset_path or score <= 0:
        return storm_id, {"status": "skipped", "reason": "missing cached GTSM subset"}

    extracted = extract_gtsm_gauge_series(subset_path)
    gauges = {}
    for key, label in GAUGE_LABELS.items():
        points = pack_gauge_rows(extracted.get(key, []))
        gauges[key] = {
            "label": label,
            "points": points,
            "stats": summarize_points(points),
        }

    return storm_id, {
        "status": "exported",
        "storm": storm.get("storm"),
        "source_window": {
            "start": start_iso,
            "end": end_iso,
            "gtsm_file": str(subset_path.relative_to(PROJECT_DIR)),
            "match_score": round(score, 3),
        },
        "gauges": gauges,
    }


def selected_storms(storms: list[dict], ids: list[int] | None, all_storms: bool) -> list[dict]:
    if all_storms:
        return storms
    if not ids:
        raise SystemExit("Choose --all or at least one --storm id.")
    wanted = set(ids)
    return [storm for storm in storms if storm_id_from_name(storm.get("storm")) in wanted]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storm", action="append", type=int, help="Storm id to export. Repeat for multiple storms.")
    parser.add_argument("--all", action="store_true", help="Export every catalogued storm.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="StormTracker data directory.")
    parser.add_argument(
        "--output",
        type=Path,
        default=STATIC_DIR / "data" / "gtsm-gauge-series.json",
        help="Static GTSM gauge series JSON output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    storms = read_json(args.data_dir / "storms.json")
    targets = selected_storms(storms, args.storm, args.all)
    if not targets:
        raise SystemExit("No matching storms found.")

    exported = {}
    summaries = []
    for storm in targets:
        storm_id, result = export_storm(storm, args.data_dir)
        if storm_id is not None and result.get("status") == "exported":
            exported[str(storm_id)] = result
        summaries.append({"storm": storm.get("storm"), **result})

    payload = {
        "generated_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "StormTracker/data/gtsm",
        "storms": exported,
    }
    write_json(args.output, payload)
    for summary in summaries:
        printable = dict(summary)
        gauges = printable.pop("gauges", None)
        if gauges:
            printable["gauge_points"] = {
                key: len((value or {}).get("points") or [])
                for key, value in gauges.items()
            }
        print(json.dumps(printable, sort_keys=True))
    print(json.dumps({"output": str(args.output.relative_to(PROJECT_DIR)), "storms": len(exported)}, sort_keys=True))


if __name__ == "__main__":
    main()
