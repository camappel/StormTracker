#!/usr/bin/env python3
"""
Migrate StormTracker storm assets to per-storm folders and normalize file names.

Legacy inputs:
  data/era5/ERA5_storm_<id>_<window>.nc
  data/storm_tracks/storm_<id>.json
  data/gtsm/storm_<id>/**/*

Target layout:
  data/storm_<id>/era5/ERA5_<window>.nc
  data/storm_<id>/gtsm/GTSM_subset_<window>.nc
  data/storm_<id>/gtsm/<window>/gtsm_<timestamp>.png
  data/storm_<id>/storm_track/track_<window>.json

Usage:
  python scripts/migrate_storm_data_layout.py            # dry-run
  python scripts/migrate_storm_data_layout.py --apply    # perform moves
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("STORMTRACKER_DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()

ERA5_DIR = DATA_DIR / "era5"
OLD_GTSM_DIR = DATA_DIR / "gtsm"
OLD_TRACK_DIR = DATA_DIR / "storm_tracks"

STORMS_PATH = DATA_DIR / "storms.json"
ERA5_OLD_RE = re.compile(r"^ERA5_storm_(\d+)_(.+)\.nc$")
ERA5_NEW_RE = re.compile(r"^ERA5_(.+)\.nc$")
TRACK_OLD_RE = re.compile(r"^storm_(\d+)\.json$")
TRACK_NEW_RE = re.compile(r"^track_(.+)\.json$")
STORM_DIR_RE = re.compile(r"^storm_(\d+)$")
GTSM_SUBSET_OLD_RE = re.compile(r"^GTSM_subset_storm_(\d+)_(.+)\.nc$")
GTSM_SUBSET_NEW_RE = re.compile(r"^GTSM_subset_(.+)\.nc$")


@dataclass(frozen=True)
class MoveOp:
    src: Path
    dst: Path
    reason: str


def storm_root(storm_id: int) -> Path:
    return DATA_DIR / f"storm_{int(storm_id)}"


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


def _window_token(iso_utc: str) -> str:
    return iso_utc.replace("-", "").replace(":", "").replace("+00:00", "Z").replace(".", "")


def _window_key(start_iso: str, end_iso: str) -> str:
    s = _normalize_iso_utc(start_iso)
    e = _normalize_iso_utc(end_iso)
    if not s or not e:
        raise ValueError("invalid window")
    return f"{_window_token(s)}_{_window_token(e)}"


def _load_storm_window_keys() -> dict[int, str]:
    if not STORMS_PATH.exists():
        return {}
    try:
        payload = json.loads(STORMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, list):
        return {}

    out: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = str(row.get("storm") or "")
        m = re.search(r"(\d+)", name)
        if not m:
            continue
        sid = int(m.group(1))
        storm_window = row.get("storm_window") if isinstance(row.get("storm_window"), dict) else {}
        era5_window = row.get("era5_window") if isinstance(row.get("era5_window"), dict) else {}
        start = storm_window.get("start") or era5_window.get("start")
        end = storm_window.get("end") or era5_window.get("end")
        if not start or not end:
            continue
        try:
            out[sid] = _window_key(start, end)
        except Exception:
            continue
    return out


def _maybe_renamed_subset_name(filename: str, storm_id: int) -> str:
    m_old = GTSM_SUBSET_OLD_RE.match(filename)
    if m_old and int(m_old.group(1)) == int(storm_id):
        return f"GTSM_subset_{m_old.group(2)}.nc"
    return filename


def _collect_legacy_era5_ops(ops: list[MoveOp]) -> None:
    if not ERA5_DIR.exists():
        return
    for src in sorted(ERA5_DIR.glob("*.nc")):
        sid = None
        window = None
        m_old = ERA5_OLD_RE.match(src.name)
        m_new = ERA5_NEW_RE.match(src.name)
        if m_old:
            sid = int(m_old.group(1))
            window = m_old.group(2)
        elif m_new:
            # Cannot infer storm_id from modern filename at legacy root.
            continue
        if sid is None or window is None:
            continue
        dst = storm_root(sid) / "era5" / f"ERA5_{window}.nc"
        ops.append(MoveOp(src=src, dst=dst, reason="era5"))


def _collect_per_storm_era5_renames(ops: list[MoveOp]) -> None:
    for sroot in sorted(DATA_DIR.glob("storm_*")):
        if not sroot.is_dir():
            continue
        m = STORM_DIR_RE.match(sroot.name)
        if not m:
            continue
        sid = int(m.group(1))
        era5_dir = sroot / "era5"
        if not era5_dir.exists():
            continue
        for src in sorted(era5_dir.glob("ERA5_storm_*.nc")):
            mm = ERA5_OLD_RE.match(src.name)
            if not mm or int(mm.group(1)) != sid:
                continue
            dst = era5_dir / f"ERA5_{mm.group(2)}.nc"
            ops.append(MoveOp(src=src, dst=dst, reason="era5_rename"))


def _collect_track_ops(ops: list[MoveOp], storm_window_keys: dict[int, str], src_dir: Path, reason: str) -> None:
    if not src_dir.exists():
        return
    for src in sorted(src_dir.glob("storm_*.json")):
        m = TRACK_OLD_RE.match(src.name)
        if not m:
            continue
        sid = int(m.group(1))
        win = storm_window_keys.get(sid)
        if not win:
            continue
        dst = storm_root(sid) / "storm_track" / f"track_{win}.json"
        ops.append(MoveOp(src=src, dst=dst, reason=reason))


def _collect_per_storm_track_renames(ops: list[MoveOp], storm_window_keys: dict[int, str]) -> None:
    for sroot in sorted(DATA_DIR.glob("storm_*")):
        if not sroot.is_dir():
            continue
        m = STORM_DIR_RE.match(sroot.name)
        if not m:
            continue
        sid = int(m.group(1))
        track_dir = sroot / "storm_track"
        if not track_dir.exists():
            continue
        for src in sorted(track_dir.glob("storm_*.json")):
            mm = TRACK_OLD_RE.match(src.name)
            if not mm or int(mm.group(1)) != sid:
                continue
            win = storm_window_keys.get(sid)
            if not win:
                continue
            dst = track_dir / f"track_{win}.json"
            ops.append(MoveOp(src=src, dst=dst, reason="track_rename"))


def _collect_gtsm_ops_from_legacy(ops: list[MoveOp]) -> None:
    if not OLD_GTSM_DIR.exists():
        return
    for storm_dir in sorted(OLD_GTSM_DIR.glob("storm_*")):
        if not storm_dir.is_dir():
            continue
        m = STORM_DIR_RE.match(storm_dir.name)
        if not m:
            continue
        sid = int(m.group(1))
        for src in sorted(p for p in storm_dir.rglob("*") if p.is_file()):
            rel = src.relative_to(storm_dir)
            rel_parts = list(rel.parts)
            if rel_parts:
                rel_parts[-1] = _maybe_renamed_subset_name(rel_parts[-1], sid)
            dst = storm_root(sid) / "gtsm" / Path(*rel_parts)
            ops.append(MoveOp(src=src, dst=dst, reason="gtsm"))


def _collect_per_storm_gtsm_renames(ops: list[MoveOp]) -> None:
    for sroot in sorted(DATA_DIR.glob("storm_*")):
        if not sroot.is_dir():
            continue
        m = STORM_DIR_RE.match(sroot.name)
        if not m:
            continue
        sid = int(m.group(1))
        gtsm_dir = sroot / "gtsm"
        if not gtsm_dir.exists():
            continue
        for src in sorted(gtsm_dir.glob("GTSM_subset_storm_*.nc")):
            mm = GTSM_SUBSET_OLD_RE.match(src.name)
            if not mm or int(mm.group(1)) != sid:
                continue
            dst = gtsm_dir / f"GTSM_subset_{mm.group(2)}.nc"
            ops.append(MoveOp(src=src, dst=dst, reason="gtsm_rename"))


def collect_ops() -> list[MoveOp]:
    ops: list[MoveOp] = []
    storm_window_keys = _load_storm_window_keys()

    _collect_legacy_era5_ops(ops)
    _collect_per_storm_era5_renames(ops)
    _collect_track_ops(ops, storm_window_keys, OLD_TRACK_DIR, reason="storm_track")
    _collect_per_storm_track_renames(ops, storm_window_keys)
    _collect_gtsm_ops_from_legacy(ops)
    _collect_per_storm_gtsm_renames(ops)

    unique: list[MoveOp] = []
    seen: set[tuple[str, str]] = set()
    for op in ops:
        key = (str(op.src), str(op.dst))
        if key in seen:
            continue
        seen.add(key)
        unique.append(op)
    return unique


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def run(apply: bool) -> int:
    ops = collect_ops()
    if not ops:
        print(f"No candidate files found under {DATA_DIR}")
        return 0

    planned = 0
    moved = 0
    skipped_missing = 0
    skipped_exists = 0

    for op in ops:
        if not op.src.exists():
            skipped_missing += 1
            continue
        if op.dst.exists():
            skipped_exists += 1
            print(f"SKIP exists ({op.reason}): {op.dst}")
            continue

        planned += 1
        print(f"{'MOVE' if apply else 'PLAN'} ({op.reason}): {op.src} -> {op.dst}")
        if apply:
            op.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(op.src), str(op.dst))
            moved += 1

    if apply:
        remove_empty_dirs(ERA5_DIR)
        remove_empty_dirs(OLD_TRACK_DIR)
        remove_empty_dirs(OLD_GTSM_DIR)

    print("")
    print("Summary")
    print(f"  candidates:      {len(ops)}")
    print(f"  actionable:      {planned}")
    print(f"  moved:           {moved}")
    print(f"  skipped_missing: {skipped_missing}")
    print(f"  skipped_exists:  {skipped_exists}")
    print(f"  mode:            {'apply' if apply else 'dry-run'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate StormTracker data into per-storm folders.")
    parser.add_argument("--apply", action="store_true", help="perform file moves (default is dry-run)")
    args = parser.parse_args()
    return run(apply=bool(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
