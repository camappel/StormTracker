#!/usr/bin/env python3
"""
Generate a local NAO-vs-closure chart for StormTracker.

Reads:
- StormTracker/data/storms.json (master closures catalog)
- StormTracker/data/nao.dat (fallback: Objective1/2_DATA/nao.dat)

Writes:
- StormTracker/data/figures/nao_vs_closures.png (default)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from scipy import stats as scipy_stats  # type: ignore
except Exception:  # pragma: no cover
    scipy_stats = None


PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_OBJECTIVE_DIR = PROJECT_DIR.parent / "Objective1"
DATA_ROOT_DIR = PROJECT_DIR / "data"

DEFAULT_STORMS_PATH = DATA_ROOT_DIR / "storms.json"
DEFAULT_NAO_PATH = DATA_ROOT_DIR / "nao.dat"
FALLBACK_NAO_PATH = LEGACY_OBJECTIVE_DIR / "2_DATA" / "nao.dat"
DEFAULT_OUTPUT_PATH = DATA_ROOT_DIR / "figures" / "nao_vs_closures.png"

SUPPORTED_BARRIERS = {"eastern_scheldt", "thames"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate NAO vs closure count chart (local Python).")
    parser.add_argument("--storms-path", type=Path, default=DEFAULT_STORMS_PATH)
    parser.add_argument("--nao-path", type=Path, default=DEFAULT_NAO_PATH)
    parser.add_argument("--nao-fallback-path", type=Path, default=FALLBACK_NAO_PATH)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--start-water-year", type=int, default=1986)
    parser.add_argument("--dpi", type=int, default=200)
    return parser.parse_args()


def water_year_label(ts: pd.Timestamp) -> str:
    year = int(ts.year)
    if int(ts.month) >= 7:
        return f"{year}/{str(year + 1)[2:]}"
    return f"{year - 1}/{str(year)[2:]}"


def load_nao_monthly_values(nao_path: Path, nao_fallback_path: Path) -> dict[int, dict[int, float]]:
    source = nao_path if nao_path.exists() else nao_fallback_path
    if not source.is_file():
        raise FileNotFoundError(f"NAO file not found: {nao_path} (fallback: {nao_fallback_path})")

    monthly_by_year: dict[int, dict[int, float]] = {}
    for raw_line in source.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 13:
            continue
        try:
            year = int(parts[0])
            month_values = []
            for token in parts[1:13]:
                value = float(token)
                month_values.append(np.nan if value <= -99.0 else value)
        except Exception:
            continue
        monthly_by_year[year] = {month: month_values[month - 1] for month in range(1, 13)}
    if not monthly_by_year:
        raise RuntimeError(f"No NAO monthly data parsed from: {source}")
    return monthly_by_year


def build_nao_closure_rows(storms_path: Path, nao_monthly: dict[int, dict[int, float]], start_water_year: int) -> list[dict]:
    try:
        storms = json.loads(storms_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in storms file: {storms_path}") from exc
    if not isinstance(storms, list):
        raise ValueError(f"Expected JSON list in storms file: {storms_path}")

    closure_counts: dict[str, int] = {}
    for storm in storms:
        if not isinstance(storm, dict):
            continue
        closures = storm.get("closures")
        if not isinstance(closures, list):
            continue
        for closure in closures:
            if not isinstance(closure, dict):
                continue
            barrier = str(closure.get("barrier") or "").strip().lower()
            closure_start = closure.get("start")
            if barrier not in SUPPORTED_BARRIERS or not closure_start:
                continue
            try:
                ts = pd.Timestamp(closure_start)
            except Exception:
                continue
            wy = water_year_label(ts)
            closure_counts[wy] = int(closure_counts.get(wy, 0)) + 1

    max_nao_start_year = max(nao_monthly.keys()) - 1
    closure_start_years = [int(wy.split("/")[0]) for wy in closure_counts]
    max_closure_start_year = max(closure_start_years) if closure_start_years else start_water_year
    end_water_year = min(max_nao_start_year, max_closure_start_year)

    rows: list[dict] = []
    for start_year in range(start_water_year, end_water_year + 1):
        water_year = f"{start_year}/{str(start_year + 1)[2:]}"
        dec = nao_monthly.get(start_year, {}).get(12, np.nan)
        jan = nao_monthly.get(start_year + 1, {}).get(1, np.nan)
        feb = nao_monthly.get(start_year + 1, {}).get(2, np.nan)
        if np.isnan(dec) or np.isnan(jan) or np.isnan(feb):
            continue
        rows.append(
            {
                "water_year": water_year,
                "winter_nao": float(np.mean([dec, jan, feb])),
                "closure_count": int(closure_counts.get(water_year, 0)),
            }
        )
    return rows


def pearson_stats(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 2:
        return np.nan, np.nan
    if scipy_stats is not None:
        try:
            r, p = scipy_stats.pearsonr(x, y)
            return float(r), float(p)
        except Exception:
            pass
    r = np.corrcoef(x, y)[0, 1] if np.std(x) > 0 and np.std(y) > 0 else np.nan
    return float(r), np.nan


def render_chart(rows: list[dict], output_path: Path, dpi: int) -> None:
    if not rows:
        raise RuntimeError("No NAO/closure rows available for plotting.")

    x = np.asarray([r["winter_nao"] for r in rows], dtype=float)
    y = np.asarray([r["closure_count"] for r in rows], dtype=float)

    slope = np.nan
    intercept = np.nan
    fit_x = np.array([], dtype=float)
    fit_y = np.array([], dtype=float)
    if len(x) >= 2 and np.std(x) > 0:
        slope, intercept = np.polyfit(x, y, 1)
        fit_x = np.linspace(float(np.min(x)), float(np.max(x)), 200)
        fit_y = slope * fit_x + intercept

    pearson_r, pearson_p = pearson_stats(x, y)

    fig, ax = plt.subplots(figsize=(10.5, 6.5))
    ax.scatter(
        x,
        y,
        s=52,
        alpha=0.9,
        color="#1565c0",
        edgecolors="white",
        linewidths=0.6,
        label="Water-year observations",
        zorder=3,
    )
    if fit_x.size:
        ax.plot(fit_x, fit_y, color="#111111", linewidth=2.0, label="Least-squares regression", zorder=2)

    ax.set_title("Winter NAO vs Storm-Barrier Closures", fontsize=15, pad=12)
    ax.set_xlabel("Winter NAO (DJF mean)", fontsize=12)
    ax.set_ylabel("Number of closures per water year", fontsize=12)
    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.35)
    ax.set_axisbelow(True)
    ax.set_ylim(bottom=min(-0.5, float(np.min(y) - 0.5)))
    ax.legend(loc="best", frameon=False)

    p_text = f"{pearson_p:.3f}" if np.isfinite(pearson_p) else "n/a"
    stats_text = (
        f"n = {len(rows)} water years\n"
        f"Pearson r = {pearson_r:.3f}\n"
        f"p-value = {p_text}"
    )
    ax.text(
        0.02,
        0.98,
        stats_text,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=10.5,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "#dddddd", "boxstyle": "round,pad=0.35"},
    )

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    if not args.storms_path.is_file():
        raise FileNotFoundError(f"Storms file not found: {args.storms_path}")

    nao_monthly = load_nao_monthly_values(args.nao_path, args.nao_fallback_path)
    rows = build_nao_closure_rows(args.storms_path, nao_monthly, args.start_water_year)
    render_chart(rows, args.output_path, args.dpi)
    print(f"Wrote chart: {args.output_path}")
    print(f"Rows plotted: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
