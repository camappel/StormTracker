#!/usr/bin/env python3
"""Export cached StormTracker ERA5/GTSM frames as static image sequences."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

import pandas as pd


STATIC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = STATIC_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
WEBAPP_DIR = PROJECT_DIR / "webapp"
WINDOW_RE = re.compile(r"(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)")
PRODUCT_LABELS = {
    "era5": "ERA5 MSLP",
    "wind": "ERA5 wind",
    "gtsm": "GTSM surge",
}

MPLCONFIG_DIR = Path(tempfile.gettempdir()) / "stormtracker_matplotlib"
MPLCONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(MPLCONFIG_DIR))
warnings.filterwarnings("ignore", message='facecolor will have no effect.*', module="cartopy")

sys.path.insert(0, str(WEBAPP_DIR))

from services.era5_service import parse_era5  # noqa: E402
from services.rendering_service import (  # noqa: E402
    render_frame,
    render_gtsm_frame_from_subset,
    render_wind_frame,
)


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


def find_window_file(directory: Path, prefix: str, suffix: str, start_iso: str, end_iso: str) -> Path | None:
    exact = directory / f"{prefix}{window_key(start_iso, end_iso)}{suffix}"
    if exact.exists():
        return exact
    best_path = None
    best_score = 0.0
    for candidate in directory.glob(f"{prefix}*{suffix}"):
        parsed = parse_window_from_name(candidate)
        if not parsed:
            continue
        score = overlap_score(start_iso, end_iso, parsed[0], parsed[1])
        if score > best_score:
            best_score = score
            best_path = candidate
    return best_path


def load_track(data_dir: Path, start_iso: str, end_iso: str, frame_list: list[tuple[int, pd.Timestamp]]) -> list[dict]:
    track_path = find_window_file(data_dir / "storm_track", "track_", ".json", start_iso, end_iso)
    if not track_path:
        return []
    frame_lookup = {
        normalize_iso(frame_time): display_index
        for display_index, (_, frame_time) in enumerate(frame_list)
    }
    track = []
    for point in read_json(track_path):
        time_utc = normalize_iso(point.get("time_utc") or point.get("time"))
        lon = point.get("lon")
        lat = point.get("lat")
        if time_utc is None or lon is None or lat is None:
            continue
        pressure = point.get("pressure_hpa")
        track.append(
            {
                "time_utc": time_utc.replace("T", " ").replace("Z", ""),
                "time_index": frame_lookup.get(time_utc),
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "pressure_hpa": round(float(pressure), 2) if pressure is not None else None,
            }
        )
    return sorted(track, key=lambda item: item["time_utc"])


def selected_storms(storms: list[dict], ids: list[int] | None, all_storms: bool) -> list[dict]:
    if all_storms:
        return storms
    if not ids:
        raise SystemExit("Choose --all or at least one --storm id.")
    wanted = set(ids)
    return [storm for storm in storms if storm_id_from_name(storm.get("storm")) in wanted]


def frame_indices(frame_count: int, stride: int, limit: int | None) -> list[int]:
    indices = list(range(0, frame_count, max(1, stride)))
    if limit is not None:
        indices = indices[: max(0, limit)]
    return indices


def write_image(path: Path, png_bytes: bytes, image_format: str, webp_quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if image_format == "png":
        path.write_bytes(png_bytes)
        return
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is required for WebP output. Use --format png or install Pillow.") from exc
    with Image.open(io.BytesIO(png_bytes)) as image:
        image.save(path, "WEBP", quality=max(1, min(100, webp_quality)), method=6)


def render_product_frame(product: str, session: dict, frame_index: int, gtsm_path: Path | None) -> bytes | None:
    if product == "era5":
        return render_frame(session, frame_index, session["track"])
    if product == "wind":
        return render_wind_frame(session, frame_index, session["track"])
    if product == "gtsm":
        if not gtsm_path:
            return None
        _, frame_time = session["frame_list"][frame_index]
        return render_gtsm_frame_from_subset(
            gtsm_path,
            pd.Timestamp(frame_time),
            time_index=frame_index,
            total_steps=len(session["frame_list"]),
            track=session["track"],
        )
    raise ValueError(f"Unsupported product: {product}")


def export_storm(
    storm: dict,
    *,
    data_dir: Path,
    output_dir: Path,
    products: list[str],
    image_format: str,
    webp_quality: int,
    stride: int,
    limit: int | None,
    overwrite: bool,
) -> dict:
    storm_id = storm_id_from_name(storm.get("storm"))
    start_iso, end_iso = pick_window(storm)
    if storm_id is None or not start_iso or not end_iso:
        return {"storm": storm.get("storm"), "status": "skipped", "reason": "missing storm window"}

    era5_path = find_window_file(data_dir / "era5", "ERA5_", ".nc", start_iso, end_iso)
    if not era5_path:
        return {"storm": storm.get("storm"), "status": "skipped", "reason": "missing cached ERA5 file"}

    parsed = parse_era5(era5_path)
    parsed["storm_id"] = storm_id
    parsed["track"] = load_track(data_dir, start_iso, end_iso, parsed["frame_list"])
    gtsm_path = find_window_file(data_dir / "gtsm", "GTSM_subset_", ".nc", start_iso, end_iso)
    indices = frame_indices(len(parsed["frame_list"]), stride, limit)
    if not indices:
        return {"storm": storm.get("storm"), "status": "skipped", "reason": "no frame indices selected"}

    counts = {}
    for product in products:
        if product == "gtsm" and not gtsm_path:
            counts[product] = 0
            continue
        product_dir = output_dir / f"storm_{storm_id}" / product
        manifest_frames = []
        extension = "png" if image_format == "png" else "webp"
        for sequence_index, frame_index in enumerate(indices):
            _, frame_time = parsed["frame_list"][frame_index]
            file_name = f"frame_{sequence_index:04d}.{extension}"
            target = product_dir / file_name
            if overwrite or not target.exists():
                png_bytes = render_product_frame(product, parsed, frame_index, gtsm_path)
                if not png_bytes:
                    continue
                write_image(target, png_bytes, image_format, webp_quality)
            manifest_frames.append(
                {
                    "index": sequence_index,
                    "time_index": frame_index,
                    "time_utc": normalize_iso(frame_time),
                    "src": file_name,
                }
            )
        write_json(
            product_dir / "manifest.json",
            {
                "storm_id": storm_id,
                "storm": storm.get("storm"),
                "product": product,
                "label": PRODUCT_LABELS[product],
                "format": image_format,
                "source_window": {
                    "start": start_iso,
                    "end": end_iso,
                    "era5_file": str(era5_path.relative_to(PROJECT_DIR)),
                    "gtsm_file": str(gtsm_path.relative_to(PROJECT_DIR)) if gtsm_path else None,
                },
                "stride": stride,
                "frames": manifest_frames,
            },
        )
        counts[product] = len(manifest_frames)
    return {"storm": storm.get("storm"), "status": "exported", "frames": counts}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storm", action="append", type=int, help="Storm id to export. Repeat for multiple storms.")
    parser.add_argument("--all", action="store_true", help="Export every catalogued storm.")
    parser.add_argument("--products", default="era5,wind,gtsm", help="Comma-separated products: era5, wind, gtsm.")
    parser.add_argument("--format", choices=("png", "webp"), default="png", help="Output image format.")
    parser.add_argument("--webp-quality", type=int, default=82, help="WebP quality when --format webp is used.")
    parser.add_argument("--stride", type=int, default=1, help="Export every Nth frame.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum frames per product per storm.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing frame files.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="StormTracker data directory.")
    parser.add_argument("--output-dir", type=Path, default=STATIC_DIR / "frames", help="Static frame output directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    products = [part.strip().lower() for part in args.products.split(",") if part.strip()]
    invalid = [product for product in products if product not in PRODUCT_LABELS]
    if invalid:
        raise SystemExit(f"Unsupported products: {', '.join(invalid)}")
    storms = read_json(args.data_dir / "storms.json")
    targets = selected_storms(storms, args.storm, args.all)
    if not targets:
        raise SystemExit("No matching storms found.")
    results = [
        export_storm(
            storm,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            products=products,
            image_format=args.format,
            webp_quality=args.webp_quality,
            stride=max(1, args.stride),
            limit=args.limit,
            overwrite=args.overwrite,
        )
        for storm in targets
    ]
    for result in results:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
