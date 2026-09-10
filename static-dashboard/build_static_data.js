#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const STATIC_DIR = __dirname;
const PROJECT_DIR = path.resolve(STATIC_DIR, "..");
const DATA_DIR = path.join(PROJECT_DIR, "data");
const PAD_HOURS = 62;
const SUPPORTED_BARRIERS = ["thames", "eastern_scheldt"];
const FRAME_PRODUCTS = ["era5", "wind", "gtsm"];
const FRAMES_DIR = path.join(STATIC_DIR, "frames");
const GTSM_SERIES_PATH = path.join(STATIC_DIR, "data", "gtsm-gauge-series.json");

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function normalizeIso(value) {
  if (!value) return null;
  let text = String(value).trim();
  if (!text) return null;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text)) {
    text = text.replace(" ", "T") + "Z";
  }
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(text)) {
    text = text + "Z";
  }
  const date = new Date(text);
  if (!Number.isFinite(date.getTime())) return null;
  return date.toISOString().replace(".000Z", "Z");
}

function isoToken(value) {
  const iso = normalizeIso(value);
  if (!iso) return null;
  return iso
    .replace(/-/g, "")
    .replace(/:/g, "")
    .replace(".000", "");
}

function windowKey(start, end) {
  const s = isoToken(start);
  const e = isoToken(end);
  return s && e ? `${s}_${e}` : null;
}

function stormIdFromName(name) {
  const match = String(name || "").match(/(\d+)$/);
  return match ? Number(match[1]) : null;
}

function barrierClassFromClosures(closures) {
  const found = new Set();
  (closures || []).forEach((closure) => {
    const barrier = String(closure.barrier || "").toLowerCase();
    if (barrier === "thames") found.add("thames");
    if (barrier === "eastern_scheldt") found.add("es");
  });
  if (found.has("thames") && found.has("es")) return "both";
  if (found.has("thames")) return "thames";
  if (found.has("es")) return "es";
  return "unknown";
}

function barrierClassLabel(value) {
  if (value === "thames") return "Thames";
  if (value === "es") return "ES";
  if (value === "both") return "Both";
  return "Unknown";
}

function uniqueBarriers(closures) {
  const out = [];
  (closures || []).forEach((closure) => {
    const barrier = String(closure.barrier || "").trim().toLowerCase();
    if (SUPPORTED_BARRIERS.includes(barrier) && !out.includes(barrier)) out.push(barrier);
  });
  return out;
}

function addHours(iso, hours) {
  const date = new Date(iso);
  date.setUTCHours(date.getUTCHours() + hours);
  return date.toISOString().replace(".000Z", "Z");
}

function pickWindow(storm) {
  const stormWindow = storm.storm_window || {};
  const era5Window = storm.era5_window || {};
  const closureStarts = [];
  const closureEnds = [];
  (storm.closures || []).forEach((closure) => {
    const start = normalizeIso(closure.start);
    const end = normalizeIso(closure.end);
    if (start) closureStarts.push(start);
    if (end) closureEnds.push(end);
  });
  const start = normalizeIso(stormWindow.start) || normalizeIso(era5Window.start) || closureStarts.sort()[0] || null;
  const end = normalizeIso(stormWindow.end) || normalizeIso(era5Window.end) || closureEnds.sort().slice(-1)[0] || null;
  return { start, end };
}

function waterWindow(storm) {
  const times = [];
  (storm.closures || []).forEach((closure) => {
    [closure.start, closure.end, closure.high_water_time].forEach((value) => {
      const iso = normalizeIso(value);
      if (iso) times.push(iso);
    });
  });
  if (times.length) {
    times.sort();
    return {
      start: addHours(times[0], -PAD_HOURS),
      end: addHours(times[times.length - 1], PAD_HOURS)
    };
  }
  const picked = pickWindow(storm);
  return {
    start: picked.start ? addHours(picked.start, -PAD_HOURS) : null,
    end: picked.end ? addHours(picked.end, PAD_HOURS) : null
  };
}

function normalizeTrackPoint(point) {
  const time = normalizeIso(point.time_utc || point.time);
  const lon = Number(point.lon);
  const lat = Number(point.lat);
  const pressure = point.pressure_hpa == null ? null : Number(point.pressure_hpa);
  if (!time || !Number.isFinite(lon) || !Number.isFinite(lat)) return null;
  return {
    time_utc: time,
    lon: round(lon, 4),
    lat: round(lat, 4),
    pressure_hpa: Number.isFinite(pressure) ? round(pressure, 2) : null
  };
}

function loadTracks() {
  const dir = path.join(DATA_DIR, "storm_track");
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .filter((name) => /^track_\d{8}T\d{6}Z_\d{8}T\d{6}Z\.json$/.test(name))
    .map((name) => {
      const match = name.match(/^track_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)\.json$/);
      const start = tokenToIso(match[1]);
      const end = tokenToIso(match[2]);
      const points = readJson(path.join(dir, name))
        .map(normalizeTrackPoint)
        .filter(Boolean)
        .sort((a, b) => Date.parse(a.time_utc) - Date.parse(b.time_utc));
      return { name, start, end, points };
    });
}

function tokenToIso(token) {
  return `${token.slice(0, 4)}-${token.slice(4, 6)}-${token.slice(6, 8)}T${token.slice(9, 11)}:${token.slice(11, 13)}:${token.slice(13, 15)}Z`;
}

function findTrackForStorm(storm, tracks) {
  const picked = pickWindow(storm);
  const key = windowKey(picked.start, picked.end);
  const exactName = key ? `track_${key}.json` : null;
  const exact = exactName ? tracks.find((track) => track.name === exactName) : null;
  if (exact) return exact;
  if (!picked.start || !picked.end) return null;
  const targetStart = Date.parse(picked.start);
  const targetEnd = Date.parse(picked.end);
  let best = null;
  tracks.forEach((track) => {
    const start = Date.parse(track.start);
    const end = Date.parse(track.end);
    const overlap = Math.min(targetEnd, end) - Math.max(targetStart, start);
    if (overlap <= 0) return;
    const union = Math.max(targetEnd, end) - Math.min(targetStart, start);
    const score = union > 0 ? overlap / union : 0;
    if (!best || score > best.score) best = { track, score };
  });
  return best ? best.track : null;
}

function loadSeries(barrier) {
  const filePath = path.join(DATA_DIR, `water_level_series_${barrier}.json`);
  if (!fs.existsSync(filePath)) return [];
  return readJson(filePath)
    .map((row) => ({
      time_utc: normalizeIso(row.time_utc || row.time),
      water_level: numberOrNull(row.water_level ?? row.water_level_m),
      tide: numberOrNull(row.tide ?? row.predicted_tide_m),
      surge: numberOrNull(row.surge ?? row.surge_m)
    }))
    .filter((row) => row.time_utc)
    .sort((a, b) => Date.parse(a.time_utc) - Date.parse(b.time_utc));
}

function sliceSeries(rows, startIso, endIso) {
  if (!rows.length || !startIso || !endIso) return [];
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  const out = [];
  for (const row of rows) {
    const time = Date.parse(row.time_utc);
    if (time < start) continue;
    if (time > end) break;
    out.push([
      row.time_utc,
      roundOrNull(row.water_level, 3),
      roundOrNull(row.tide, 3),
      roundOrNull(row.surge, 3)
    ]);
  }
  return out;
}

function summarizeTrack(points) {
  const stats = {
    point_count: points.length,
    min_pressure_hpa: null,
    max_pressure_hpa: null,
    min_pressure_time: null
  };
  points.forEach((point) => {
    const pressure = Number(point.pressure_hpa);
    if (!Number.isFinite(pressure)) return;
    if (stats.min_pressure_hpa == null || pressure < stats.min_pressure_hpa) {
      stats.min_pressure_hpa = pressure;
      stats.min_pressure_time = point.time_utc;
    }
    if (stats.max_pressure_hpa == null || pressure > stats.max_pressure_hpa) {
      stats.max_pressure_hpa = pressure;
    }
  });
  return stats;
}

function summarizeSeries(points) {
  const stats = {
    point_count: points.length,
    max_water_level: null,
    max_water_level_time: null,
    max_surge: null,
    max_surge_time: null,
    min_surge: null
  };
  points.forEach((point) => {
    const water = numberOrNull(point[1]);
    const surge = numberOrNull(point[3]);
    if (water != null && (stats.max_water_level == null || water > stats.max_water_level)) {
      stats.max_water_level = water;
      stats.max_water_level_time = point[0];
    }
    if (surge != null && (stats.max_surge == null || surge > stats.max_surge)) {
      stats.max_surge = surge;
      stats.max_surge_time = point[0];
    }
    if (surge != null && (stats.min_surge == null || surge < stats.min_surge)) {
      stats.min_surge = surge;
    }
  });
  Object.keys(stats).forEach((key) => {
    if (typeof stats[key] === "number") stats[key] = round(stats[key], 3);
  });
  return stats;
}

function summarizeGaugeContext(rows) {
  const waterPoints = rows
    .filter((row) => row.time_utc && row.water_level != null)
    .map((row) => ({ time_utc: row.time_utc, water_level: Number(row.water_level) }))
    .filter((row) => Number.isFinite(row.water_level));
  const stats = {
    point_count: waterPoints.length,
    start_time: waterPoints.length ? waterPoints[0].time_utc : null,
    end_time: waterPoints.length ? waterPoints[waterPoints.length - 1].time_utc : null,
    max_water_level: null,
    max_water_level_time: null,
    mean_high_water: null,
    high_water_count: 0
  };
  const highWaters = [];
  waterPoints.forEach((point) => {
    if (stats.max_water_level == null || point.water_level > stats.max_water_level) {
      stats.max_water_level = point.water_level;
      stats.max_water_level_time = point.time_utc;
    }
  });
  for (let i = 1; i < waterPoints.length - 1; i += 1) {
    const previous = waterPoints[i - 1].water_level;
    const current = waterPoints[i].water_level;
    const next = waterPoints[i + 1].water_level;
    if (current >= previous && current > next) {
      highWaters.push(current);
    }
  }
  if (highWaters.length) {
    stats.mean_high_water = highWaters.reduce((sum, value) => sum + value, 0) / highWaters.length;
    stats.high_water_count = highWaters.length;
  }
  Object.keys(stats).forEach((key) => {
    if (typeof stats[key] === "number" && key !== "point_count" && key !== "high_water_count") {
      stats[key] = round(stats[key], 3);
    }
  });
  return stats;
}

function normalizeClosure(closure) {
  return {
    start: normalizeIso(closure.start),
    end: normalizeIso(closure.end),
    barrier: String(closure.barrier || "").trim().toLowerCase(),
    has_barrier_window: Boolean(closure.has_barrier_window),
    high_water_time: normalizeIso(closure.high_water_time),
    closure_id: closure.closure_id == null ? null : Number(closure.closure_id)
  };
}

function durationHours(start, end) {
  if (!start || !end) return null;
  const ms = Date.parse(end) - Date.parse(start);
  return Number.isFinite(ms) && ms > 0 ? round(ms / 36e5, 1) : null;
}

function numberOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function roundOrNull(value, digits) {
  const number = numberOrNull(value);
  return number == null ? null : round(number, digits);
}

function round(value, digits) {
  const scale = 10 ** digits;
  return Math.round(Number(value) * scale) / scale;
}

function relativeStaticPath(filePath) {
  return path.relative(STATIC_DIR, filePath).split(path.sep).join("/");
}

function normalizeFrameEntry(entry, manifestDir) {
  if (!entry || typeof entry !== "object") return null;
  const time = normalizeIso(entry.time_utc || entry.time);
  const srcText = String(entry.src || entry.file || "").trim();
  if (!time || !srcText) return null;
  const absoluteSrc = path.isAbsolute(srcText) ? srcText : path.join(manifestDir, srcText);
  return {
    time_utc: time,
    src: relativeStaticPath(absoluteSrc)
  };
}

function loadFrameProductManifest(stormId, product) {
  const manifestPath = path.join(FRAMES_DIR, `storm_${stormId}`, product, "manifest.json");
  if (!fs.existsSync(manifestPath)) return null;
  const manifest = readJson(manifestPath);
  const frames = (manifest.frames || [])
    .map((entry) => normalizeFrameEntry(entry, path.dirname(manifestPath)))
    .filter(Boolean)
    .sort((a, b) => Date.parse(a.time_utc) - Date.parse(b.time_utc));
  if (!frames.length) return null;
  return {
    product,
    label: manifest.label || product,
    frames
  };
}

function loadFrameManifest(stormId) {
  const products = {};
  FRAME_PRODUCTS.forEach((product) => {
    const manifest = loadFrameProductManifest(stormId, product);
    if (manifest) products[product] = manifest;
  });
  const times = Array.from(new Set(Object.keys(products).flatMap((key) => {
    return products[key].frames.map((frame) => frame.time_utc);
  }))).sort((a, b) => Date.parse(a) - Date.parse(b));
  return {
    times,
    products
  };
}

function loadGtsmSeriesCatalog() {
  if (!fs.existsSync(GTSM_SERIES_PATH)) return {};
  const payload = readJson(GTSM_SERIES_PATH);
  return payload && payload.storms && typeof payload.storms === "object" ? payload.storms : {};
}

function loadGtsmSeriesForStorm(stormId, catalog) {
  if (stormId == null) return null;
  const entry = catalog[String(stormId)];
  if (!entry || !entry.gauges || typeof entry.gauges !== "object") return null;
  return entry;
}

function buildPayload() {
  const storms = readJson(path.join(DATA_DIR, "storms.json"));
  const tracks = loadTracks();
  const seriesByBarrier = Object.fromEntries(SUPPORTED_BARRIERS.map((barrier) => [barrier, loadSeries(barrier)]));
  const gaugeStats = Object.fromEntries(
    SUPPORTED_BARRIERS.map((barrier) => [barrier, summarizeGaugeContext(seriesByBarrier[barrier])])
  );
  const gtsmSeriesByStorm = loadGtsmSeriesCatalog();

  const outputStorms = storms.map((storm) => {
    const stormId = stormIdFromName(storm.storm);
    const closures = (storm.closures || []).map(normalizeClosure);
    const picked = pickWindow({ ...storm, closures });
    const water = waterWindow({ ...storm, closures });
    const track = findTrackForStorm({ ...storm, closures }, tracks);
    const trackPoints = track ? track.points : [];
    const series = {};
    SUPPORTED_BARRIERS.forEach((barrier) => {
      const points = sliceSeries(seriesByBarrier[barrier], water.start, water.end);
      if (points.length) {
        series[barrier] = {
          points,
          stats: summarizeSeries(points)
        };
      }
    });
    const barrierClass = barrierClassFromClosures(closures);
    const displayStart = picked.start || water.start;
    const displayEnd = picked.end || water.end;
    return {
      storm_id: stormId,
      storm: storm.storm,
      label: String(storm.storm || "").replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      year: displayStart ? new Date(displayStart).getUTCFullYear() : null,
      storm_type: storm.storm_type || null,
      barrier_class: barrierClass,
      barrier_class_label: barrierClassLabel(barrierClass),
      barriers: uniqueBarriers(closures),
      display_start: displayStart,
      display_end: displayEnd,
      duration_hours: durationHours(displayStart, displayEnd),
      era5_window: {
        start: normalizeIso(storm.era5_window && storm.era5_window.start),
        end: normalizeIso(storm.era5_window && storm.era5_window.end)
      },
      storm_window: {
        start: normalizeIso(storm.storm_window && storm.storm_window.start),
        end: normalizeIso(storm.storm_window && storm.storm_window.end)
      },
      water_window: water,
      closures,
      track_source: track ? track.name : null,
      track: trackPoints,
      track_stats: summarizeTrack(trackPoints),
      series,
      gtsm_series: loadGtsmSeriesForStorm(stormId, gtsmSeriesByStorm),
      frames: loadFrameManifest(stormId)
    };
  }).filter((storm) => storm.storm_id != null)
    .sort((a, b) => a.storm_id - b.storm_id);

  return {
    generated_at: new Date().toISOString().replace(".000Z", "Z"),
    source: "StormTracker/data",
    pad_hours: PAD_HOURS,
    gauge_stats: gaugeStats,
    storms: outputStorms
  };
}

function main() {
  const payload = buildPayload();
  const outDir = path.join(STATIC_DIR, "data");
  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, "storm-dashboard-data.js");
  fs.writeFileSync(
    outPath,
    `/* Generated by static-dashboard/build_static_data.js. */\nwindow.STORM_DASHBOARD_DATA = ${JSON.stringify(payload)};\n`,
    "utf8"
  );
  const bytes = fs.statSync(outPath).size;
  console.log(`Wrote ${path.relative(PROJECT_DIR, outPath)} (${payload.storms.length} storms, ${bytes} bytes)`);
}

main();
