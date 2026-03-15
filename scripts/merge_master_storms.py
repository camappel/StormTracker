#!/usr/bin/env python3
"""
Build a canonical master storms.json for StormTracker.

Sources:
- data/eastern_scheldt/storms.json
- data/thames/storms.json

Outputs:
- data/storms.json                           (master source of truth)
- data/eastern_scheldt/storms.json          (compatibility projection)

Master schema adds per-closure barrier metadata:
{
  "storm": "storm_1",
  "storm_type": null,
  "era5_window": {"start": "...", "end": "..."},
  "storm_window": {"start": null, "end": null},
  "closures": [{"start": "...", "end": "...", "barrier": "eastern_scheldt"}]
}
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"

DEFAULT_EASTERN_SOURCE = DATA_DIR / "eastern_scheldt" / "storms.json"
DEFAULT_THAMES_SOURCE = DATA_DIR / "thames" / "storms.json"
DEFAULT_MASTER_OUT = DATA_DIR / "storms.json"
DEFAULT_EASTERN_COMPAT_OUT = DATA_DIR / "eastern_scheldt" / "storms.json"

ALLOWED_BARRIERS = {"eastern_scheldt", "thames"}
ALLOWED_STORM_TYPES = {"Channel Rat", "North Sea Storm"}


def normalize_iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = pd.Timestamp(s)
    if dt.tzinfo is None:
        dt = dt.tz_localize(timezone.utc)
    else:
        dt = dt.tz_convert(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_storm_type(value: str | None) -> str | None:
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


def load_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must be a JSON list")
    rows = [row for row in payload if isinstance(row, dict)]
    return rows


def flatten_source_closures(rows: list[dict[str, Any]], barrier: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for storm_idx, row in enumerate(rows):
        closures = row.get("closures")
        if not isinstance(closures, list):
            continue

        storm_window = row.get("storm_window") if isinstance(row.get("storm_window"), dict) else {}
        storm_window_start = normalize_iso_utc(storm_window.get("start"))
        storm_window_end = normalize_iso_utc(storm_window.get("end"))
        source_storm_type = normalize_storm_type(row.get("storm_type"))
        for closure_idx, closure in enumerate(closures):
            if not isinstance(closure, dict):
                continue
            start = normalize_iso_utc(closure.get("start"))
            end = normalize_iso_utc(closure.get("end"))
            if not start or not end:
                continue
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            if end_ts < start_ts:
                continue
            out.append(
                {
                    "start": start,
                    "end": end,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "barrier": barrier,
                    "source_storm_index": int(storm_idx),
                    "source_closure_index": int(closure_idx),
                    "source_storm_window_start": storm_window_start,
                    "source_storm_window_end": storm_window_end,
                    "source_storm_type": source_storm_type,
                }
            )
    out.sort(key=lambda c: (c["start_ts"], c["end_ts"], c["barrier"]))
    return out


def cluster_overlapping_closures(closures: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_end: pd.Timestamp | None = None
    for closure in closures:
        if not current:
            current = [closure]
            current_end = closure["end_ts"]
            continue
        if closure["start_ts"] <= current_end:
            current.append(closure)
            if closure["end_ts"] > current_end:
                current_end = closure["end_ts"]
            continue
        clusters.append(current)
        current = [closure]
        current_end = closure["end_ts"]
    if current:
        clusters.append(current)
    return clusters


def _cluster_storm_window(cluster: list[dict[str, Any]]) -> dict[str, str | None]:
    starts = [pd.Timestamp(x["source_storm_window_start"]) for x in cluster if x.get("source_storm_window_start")]
    ends = [pd.Timestamp(x["source_storm_window_end"]) for x in cluster if x.get("source_storm_window_end")]
    if starts and ends:
        return {
            "start": min(starts).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": max(ends).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return {"start": None, "end": None}


def build_master_rows(clusters: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    merged_cross_barrier_count = 0
    for i, cluster in enumerate(clusters, start=1):
        unique_closures = {
            (c["start"], c["end"], c["barrier"]): {
                "start": c["start"],
                "end": c["end"],
                "barrier": c["barrier"],
            }
            for c in cluster
        }
        closures = sorted(unique_closures.values(), key=lambda c: (c["start"], c["end"], c["barrier"]))
        barriers_in_cluster = {c["barrier"] for c in closures}
        if len(barriers_in_cluster) > 1:
            merged_cross_barrier_count += 1

        starts = [pd.Timestamp(c["start"]) for c in closures]
        ends = [pd.Timestamp(c["end"]) for c in closures]
        min_start = min(starts)
        max_end = max(ends)
        era5_window = {
            "start": (min_start - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (max_end + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        storm_window = _cluster_storm_window(cluster)
        cluster_storm_types = sorted({c.get("source_storm_type") for c in cluster if c.get("source_storm_type")})
        storm_type = cluster_storm_types[0] if len(cluster_storm_types) == 1 else None
        rows.append(
            {
                "storm": f"storm_{i}",
                "storm_type": storm_type,
                "era5_window": era5_window,
                "storm_window": storm_window,
                "closures": closures,
            }
        )
    return rows, merged_cross_barrier_count


def build_eastern_compat_rows(master_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compat_rows: list[dict[str, Any]] = []
    next_id = 1
    for row in master_rows:
        raw_closures = row.get("closures")
        if not isinstance(raw_closures, list):
            continue
        eastern_closures = [
            {"start": normalize_iso_utc(c.get("start")), "end": normalize_iso_utc(c.get("end"))}
            for c in raw_closures
            if isinstance(c, dict) and c.get("barrier") == "eastern_scheldt"
        ]
        eastern_closures = [c for c in eastern_closures if c["start"] and c["end"]]
        eastern_closures.sort(key=lambda c: (c["start"], c["end"]))
        if not eastern_closures:
            continue

        starts = [pd.Timestamp(c["start"]) for c in eastern_closures]
        ends = [pd.Timestamp(c["end"]) for c in eastern_closures]
        era5_window = {
            "start": (min(starts) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (max(ends) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        compat_rows.append(
            {
                "storm": f"storm_{next_id}",
                "storm_type": normalize_storm_type(row.get("storm_type")),
                "era5_window": era5_window,
                "storm_window": {"start": None, "end": None},
                "closures": eastern_closures,
            }
        )
        next_id += 1
    return compat_rows


def validate_master(rows: list[dict[str, Any]]) -> dict[str, int]:
    storm_count = 0
    closure_count = 0
    barrier_counts: Counter[str] = Counter()

    for row in rows:
        storm_count += 1
        storm_type = normalize_storm_type(row.get("storm_type"))
        if row.get("storm_type") not in (None, "") and storm_type is None:
            raise ValueError(f"Invalid storm_type: {row.get('storm_type')!r}")
        closures = row.get("closures")
        if not isinstance(closures, list):
            raise ValueError("Each storm row must have a list of closures")
        for c in closures:
            if not isinstance(c, dict):
                raise ValueError("Each closure must be an object")
            start = normalize_iso_utc(c.get("start"))
            end = normalize_iso_utc(c.get("end"))
            barrier = str(c.get("barrier") or "")
            if not start or not end:
                raise ValueError("Each closure must have start/end")
            if barrier not in ALLOWED_BARRIERS:
                raise ValueError(f"Invalid closure barrier: {barrier!r}")
            if pd.Timestamp(end) < pd.Timestamp(start):
                raise ValueError("Closure end must not be before start")
            closure_count += 1
            barrier_counts[barrier] += 1
    return {
        "storms": storm_count,
        "closures": closure_count,
        "closures_eastern_scheldt": int(barrier_counts["eastern_scheldt"]),
        "closures_thames": int(barrier_counts["thames"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge Eastern Scheldt + Thames storms into master storms.json")
    parser.add_argument("--eastern-source", type=Path, default=DEFAULT_EASTERN_SOURCE)
    parser.add_argument("--thames-source", type=Path, default=DEFAULT_THAMES_SOURCE)
    parser.add_argument("--master-out", type=Path, default=DEFAULT_MASTER_OUT)
    parser.add_argument("--eastern-compat-out", type=Path, default=DEFAULT_EASTERN_COMPAT_OUT)
    parser.add_argument("--write", action="store_true", help="write output files (default: dry-run report only)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    eastern_rows = load_json_list(args.eastern_source)
    thames_rows = load_json_list(args.thames_source)

    flattened = flatten_source_closures(eastern_rows, "eastern_scheldt")
    flattened += flatten_source_closures(thames_rows, "thames")
    flattened.sort(key=lambda c: (c["start_ts"], c["end_ts"], c["barrier"]))

    clusters = cluster_overlapping_closures(flattened)
    master_rows, merged_cross_barrier_count = build_master_rows(clusters)
    stats = validate_master(master_rows)
    compat_rows = build_eastern_compat_rows(master_rows)

    print("Merge report")
    print(f"  eastern source storms:   {len(eastern_rows)}")
    print(f"  thames source storms:    {len(thames_rows)}")
    print(f"  input closures total:    {len(flattened)}")
    print(f"  merged storm events:     {len(master_rows)}")
    print(f"  cross-barrier merges:    {merged_cross_barrier_count}")
    print(f"  master closures total:   {stats['closures']}")
    print(f"  master eastern closures: {stats['closures_eastern_scheldt']}")
    print(f"  master thames closures:  {stats['closures_thames']}")
    print(f"  eastern compat storms:   {len(compat_rows)}")
    print(f"  mode:                    {'write' if args.write else 'dry-run'}")

    if args.write:
        args.master_out.parent.mkdir(parents=True, exist_ok=True)
        args.master_out.write_text(json.dumps(master_rows, indent=2), encoding="utf-8")
        args.eastern_compat_out.parent.mkdir(parents=True, exist_ok=True)
        args.eastern_compat_out.write_text(json.dumps(compat_rows, indent=2), encoding="utf-8")
        print(f"Wrote {args.master_out}")
        print(f"Wrote {args.eastern_compat_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
