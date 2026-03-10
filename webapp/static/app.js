(function () {
  const API = ''; // same origin
  if (typeof Chart !== 'undefined' && window['chartjs-plugin-annotation']) {
    Chart.register(window['chartjs-plugin-annotation']);
  }
  let state = {
    sessionId: null,
    bounds: null,
    times: [],
    timeIndex: 0,
    track: [],
    mode: 'add',
    storms: [],
    selectedStormId: null,
    waterSeries: [],
    window: { startIndex: 0, endIndex: 0 },
    stormWindows: [],
    barrierStartUtc: null,
    barrierEndUtc: null,
    defaultCurrentUtc: null,
    activeStartIdx: 0,
    activeEndIdx: 0,
    waterTideChart: null,
    surgeChart: null,
    era5StartUtc: null,
    era5EndUtc: null,
    plotStartUtc: null,
    plotEndUtc: null,
    sliderInternalUpdate: false,
    autoStartTimer: null,
    lastAutoStartKey: null,
    autoStartRequestId: 0
  };

  const stormSelect = document.getElementById('storm-select');
  const stormMeta = document.getElementById('storm-meta');
  const waterLevelPlaceholder = document.getElementById('water-level-placeholder');
  const waterLevelLoading = document.getElementById('water-level-loading');
  const waterLevelError = document.getElementById('water-level-error');
  const waterLevelChartWrap = document.getElementById('water-level-chart-wrap');
  const waterTideChartCanvas = document.getElementById('water-tide-chart');
  const surgeChartCanvas = document.getElementById('surge-chart');
  const era5WindowSection = document.getElementById('era5-window-section');
  const chartRangeStartSelect = document.getElementById('chart-range-start');
  const chartRangeEndSelect = document.getElementById('chart-range-end');
  const era5StartInput = document.getElementById('era5-start-input');
  const era5EndInput = document.getElementById('era5-end-input');
  const btnEra5Apply = document.getElementById('btn-era5-apply');
  const activeFirstFrameEl = document.getElementById('active-first-frame');
  const activeCurrentFrameEl = document.getElementById('active-current-frame');
  const activeLastFrameEl = document.getElementById('active-last-frame');
  const activeFrameCountEl = document.getElementById('active-frame-count');
  const startUtcInput = document.getElementById('start-utc');
  const endUtcInput = document.getElementById('end-utc');
  const uploadError = document.getElementById('upload-error');
  const apiStatus = document.getElementById('api-status');
  const mapSection = document.getElementById('map-section');
  const frameImg = document.getElementById('frame-img');
  const clickLayer = document.getElementById('click-layer');
  const timeWindowSliderEl = document.getElementById('time-window-slider');
  const btnDownload = document.getElementById('btn-download');
  const btnSaveCombined = document.getElementById('btn-save-combined');
  const btnDownloadCombined = document.getElementById('btn-download-combined');
  const trackInfo = document.getElementById('track-info');
  const gifSection = document.getElementById('gif-section');
  const btnGenerateGif = document.getElementById('btn-generate-gif');
  const btnDownloadGif = document.getElementById('btn-download-gif');
  const gifStatus = document.getElementById('gif-status');
  const gifPreviewWrap = document.getElementById('gif-preview-wrap');
  const gifImg = document.getElementById('era5-gif');

  function showError(msg) {
    uploadError.textContent = msg;
    uploadError.hidden = false;
  }

  function clearError() {
    uploadError.textContent = '';
    uploadError.hidden = true;
  }

  function showApiStatus(msg) {
    if (apiStatus) {
      apiStatus.textContent = msg;
      apiStatus.hidden = false;
    }
  }

  function clearApiStatus() {
    if (apiStatus) {
      apiStatus.textContent = '';
      apiStatus.hidden = true;
    }
  }

  function pixelToLonLat(x, y) {
    const b = state.bounds;
    if (!b) return null;
    // Use the displayed image bounds (not the full overlay) so mapping stays
    // accurate even if the container includes tiny letterbox bands.
    const rect = frameImg.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (w <= 0 || h <= 0) return null;
    const relX = x - rect.left;
    const relY = y - rect.top;
    if (relX < 0 || relX > w || relY < 0 || relY > h) return null;
    const fracX = relX / w;
    const fracY = relY / h;
    const lon = b.lon_min + fracX * (b.lon_max - b.lon_min);
    const lat = b.lat_max - fracY * (b.lat_max - b.lat_min);
    return { lon, lat };
  }

  function updateTimeLabel() {
    // Current frame is represented by map image + center slider handle.
  }

  function sliderTooltipForIndex(idx) {
    const i = Math.max(0, Math.min(idx, state.times.length - 1));
    const t = state.times[i];
    return t && t.time_utc ? t.time_utc.slice(5, 16).replace('T', ' ') : String(i + 1);
  }

  function isoToLocalInputValue(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const y = d.getUTCFullYear();
    const m = String(d.getUTCMonth() + 1).padStart(2, '0');
    const day = String(d.getUTCDate()).padStart(2, '0');
    const h = String(d.getUTCHours()).padStart(2, '0');
    return `${y}-${m}-${day}T${h}:00`;
  }

  function localInputValueToIso(value) {
    if (!value) return null;
    const d = new Date(`${value}:00Z`);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
  }

  function nearestSeriesIso(targetIso, mode) {
    if (!state.waterSeries.length) return null;
    const targetMs = new Date(targetIso).getTime();
    if (!Number.isFinite(targetMs)) return null;
    if (mode === 'start') {
      for (let i = 0; i < state.waterSeries.length; i++) {
        const ms = new Date(state.waterSeries[i].time_utc).getTime();
        if (Number.isFinite(ms) && ms >= targetMs) return state.waterSeries[i].time_utc;
      }
      return state.waterSeries[state.waterSeries.length - 1].time_utc;
    }
    for (let i = state.waterSeries.length - 1; i >= 0; i--) {
      const ms = new Date(state.waterSeries[i].time_utc).getTime();
      if (Number.isFinite(ms) && ms <= targetMs) return state.waterSeries[i].time_utc;
    }
    return state.waterSeries[0].time_utc;
  }

  function populateChartRangeControls() {
    if (!chartRangeStartSelect || !chartRangeEndSelect) return;
    chartRangeStartSelect.innerHTML = '';
    chartRangeEndSelect.innerHTML = '';
    state.waterSeries.forEach((point) => {
      if (!point.time_utc) return;
      const label = formatIsoForCompactUtc(point.time_utc);
      const startOpt = document.createElement('option');
      startOpt.value = point.time_utc;
      startOpt.textContent = label;
      const endOpt = document.createElement('option');
      endOpt.value = point.time_utc;
      endOpt.textContent = label;
      chartRangeStartSelect.appendChild(startOpt);
      chartRangeEndSelect.appendChild(endOpt);
    });
  }

  function applyChartRange(startIso, endIso) {
    if (!state.waterSeries.length) return;
    let nextStart = nearestSeriesIso(startIso, 'start');
    let nextEnd = nearestSeriesIso(endIso, 'end');
    if (!nextStart || !nextEnd) return;
    if (new Date(nextEnd) <= new Date(nextStart)) {
      nextStart = state.waterSeries[0].time_utc;
      nextEnd = state.waterSeries[state.waterSeries.length - 1].time_utc;
    }
    state.plotStartUtc = nextStart;
    state.plotEndUtc = nextEnd;
    if (chartRangeStartSelect) chartRangeStartSelect.value = nextStart;
    if (chartRangeEndSelect) chartRangeEndSelect.value = nextEnd;
    updateChartViewport(nextStart, nextEnd);
  }

  function updateEra5LineAnnotations(startIso, endIso) {
    const charts = [state.waterTideChart, state.surgeChart];
    charts.forEach((chart) => {
      if (!chart || !chart.options || !chart.options.plugins || !chart.options.plugins.annotation) return;
      const anns = chart.options.plugins.annotation.annotations || {};
      if (startIso && anns['era5-start-line']) {
        anns['era5-start-line'].xMin = startIso;
        anns['era5-start-line'].xMax = startIso;
      }
      if (endIso && anns['era5-end-line']) {
        anns['era5-end-line'].xMin = endIso;
        anns['era5-end-line'].xMax = endIso;
      }
      chart.update('none');
    });
  }

  function lockOuterSliderHandles() {
    // Single-handle slider now controls only current frame.
  }

  function ensureWindowSlider() {
    if (!timeWindowSliderEl || typeof noUiSlider === 'undefined') return;
    if (timeWindowSliderEl.noUiSlider) return;
    noUiSlider.create(timeWindowSliderEl, {
      start: [0],
      connect: [true, false],
      step: 1,
      behaviour: 'tap-drag',
      range: { min: 0, max: 1 },
      tooltips: [{ to: (v) => sliderTooltipForIndex(Math.round(v)) }],
      format: {
        to: (v) => String(Math.round(v)),
        from: (v) => Number(v)
      }
    });
    lockOuterSliderHandles();
    timeWindowSliderEl.noUiSlider.on('update', (values) => {
      if (state.sliderInternalUpdate || !state.times.length) return;
      const n = state.times.length;
      let currentIdx = Math.round(Number(values[0]));
      const startIdx = Math.max(0, Math.min(state.activeStartIdx, n - 1));
      const endIdx = Math.max(startIdx, Math.min(state.activeEndIdx, n - 1));
      if (currentIdx < startIdx) currentIdx = startIdx;
      if (currentIdx > endIdx) currentIdx = endIdx;
      state.timeIndex = currentIdx;
      updateTimeLabel();
      updateFrameWindowStatus();
      updateCurrentFrameMarker();
      loadFrame();
      const normalized = [currentIdx];
      if (currentIdx !== Math.round(Number(values[0]))) {
        state.sliderInternalUpdate = true;
        timeWindowSliderEl.noUiSlider.set(normalized);
        state.sliderInternalUpdate = false;
      }
    });
  }

  function updateSlider() {
    const n = state.times.length;
    ensureWindowSlider();
    if (!timeWindowSliderEl || !timeWindowSliderEl.noUiSlider) return;
    const minIdx = Math.max(0, Math.min(state.activeStartIdx, n - 1));
    const maxIdx = Math.max(minIdx, Math.min(state.activeEndIdx, n - 1));
    if (state.timeIndex < minIdx) state.timeIndex = minIdx;
    if (state.timeIndex > maxIdx) state.timeIndex = maxIdx;
    const slider = timeWindowSliderEl.noUiSlider;
    state.sliderInternalUpdate = true;
    slider.updateOptions({ range: { min: 0, max: Math.max(1, n - 1) } }, false);
    slider.set([state.timeIndex]);
    state.sliderInternalUpdate = false;
    lockOuterSliderHandles();
  }

  function updateTrackInfo() {
    const n = state.track.length;
    trackInfo.textContent = n === 0
      ? 'No track points. Click on map in "Add point" mode to add.'
      : `${n} track point(s). Save to combined CSV when done.`;
    btnSaveCombined.disabled = !state.sessionId || n === 0;
    if (btnGenerateGif) {
      btnGenerateGif.disabled = !state.sessionId || n === 0;
    }
    if (btnDownloadGif) {
      btnDownloadGif.disabled = !state.sessionId || n === 0;
    }
  }

  function updateDownloadLink() {
    if (!state.sessionId) {
      btnDownload.href = '#';
      btnDownload.style.visibility = 'hidden';
      return;
    }
    btnDownload.href = `${API}/api/csv/${state.sessionId}`;
    btnDownload.style.visibility = 'visible';
  }

  function clearGifPreview() {
    if (gifImg) {
      gifImg.src = '';
    }
    if (gifPreviewWrap) {
      gifPreviewWrap.hidden = true;
    }
    if (gifStatus) {
      gifStatus.textContent = '';
      gifStatus.hidden = true;
    }
    if (btnDownloadGif) {
      btnDownloadGif.disabled = true;
    }
  }

  function showGifStatus(msg) {
    if (!gifStatus) return;
    gifStatus.textContent = msg;
    gifStatus.hidden = !msg;
  }

  async function generateGif() {
    if (!state.sessionId || !state.track.length || !btnGenerateGif) return;
    try {
      btnGenerateGif.disabled = true;
      showGifStatus('Generating ERA5 GIF…');
      clearError();
      const url = `${API}/api/gif/era5/${state.sessionId}`;
      // Use a cache-busting query param so updated GIFs reload
      const gifUrl = `${url}?t=${Date.now()}`;
      if (gifImg && gifPreviewWrap) {
        gifImg.onload = function () {
          showGifStatus('');
        };
        gifImg.onerror = function () {
          showGifStatus('Failed to load ERA5 GIF.');
        };
        gifImg.src = gifUrl;
        gifPreviewWrap.hidden = false;
      } else {
        // Fallback: open in new tab
        window.open(gifUrl, '_blank');
        showGifStatus('');
      }
    } catch (e) {
      showGifStatus('Error generating ERA5 GIF: ' + e.message);
    } finally {
      if (btnGenerateGif) {
        btnGenerateGif.disabled = !state.sessionId || !state.track.length;
      }
    }
  }

  async function downloadGif() {
    if (!state.sessionId || !state.track.length) return;
    const url = `${API}/api/gif/era5/${state.sessionId}`;
    const filename = `era5_storm_${state.selectedStormId != null ? state.selectedStormId : state.sessionId}.gif`;
    try {
      showGifStatus('Preparing download…');
      const res = await fetch(url);
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
      showGifStatus('');
    } catch (e) {
      showGifStatus('Download failed: ' + e.message);
    }
  }

  function loadFrame() {
    if (!state.sessionId) return;
    const url = `${API}/api/frame/${state.sessionId}/${state.timeIndex}`;
    frameImg.src = url + '?t=' + Date.now();
  }

  async function fetchTrack() {
    if (!state.sessionId) return;
    const res = await fetch(`${API}/api/track/${state.sessionId}`);
    if (!res.ok) return;
    const data = await res.json();
    state.track = data.track || [];
    updateTrackInfo();
    updateDownloadLink();
  }

  async function addPoint(lon, lat) {
    const res = await fetch(`${API}/api/track/add`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.sessionId,
        time_index: state.timeIndex,
        lon,
        lat
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || 'Failed to add point');
      return;
    }
    clearError();
    const data = await res.json();
    state.track = data.track || [];
    updateTrackInfo();
    updateDownloadLink();
    loadFrame();
  }

  async function deletePoint(timeIndex) {
    const res = await fetch(`${API}/api/track/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.sessionId,
        time_index: timeIndex
      })
    });
    if (!res.ok) return;
    const data = await res.json();
    state.track = data.track || [];
    updateTrackInfo();
    updateDownloadLink();
    loadFrame();
  }

  async function deleteLastPoint() {
    const res = await fetch(`${API}/api/track/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.sessionId,
        time_index: -1
      })
    });
    if (!res.ok) return;
    const data = await res.json();
    state.track = data.track || [];
    updateTrackInfo();
    updateDownloadLink();
    loadFrame();
  }

  function formatIsoForDisplay(isoString) {
    if (!isoString) return '—';
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  }

  function formatIsoForCompactUtc(isoString) {
    if (!isoString) return '—';
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }

  function parseSessionUtc(value) {
    if (!value) return null;
    const s = String(value).trim().replace(' ', 'T');
    const iso = s.endsWith('Z') ? s : `${s}Z`;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function sessionIndexTimeMs(idx) {
    if (idx < 0 || idx >= state.times.length) return NaN;
    const t = parseSessionUtc(state.times[idx].time_utc);
    return t ? t.getTime() : NaN;
  }

  function computeBarrierBounds() {
    const windows = state.stormWindows || [];
    if (!windows.length) return null;
    let startIso = windows[0].start_utc;
    let endIso = windows[0].end_utc;
    windows.forEach((w) => {
      if (w.start_utc < startIso) startIso = w.start_utc;
      if (w.end_utc > endIso) endIso = w.end_utc;
    });
    return { startUtc: startIso, endUtc: endIso };
  }

  function updateFrameWindowStatus() {
    const n = state.times.length;
    if (!activeFirstFrameEl || !activeLastFrameEl || !activeFrameCountEl || n === 0) {
      return;
    }
    const startIdx = Math.max(0, Math.min(state.activeStartIdx, n - 1));
    const endIdx = Math.max(startIdx, Math.min(state.activeEndIdx, n - 1));
    const currentIdx = Math.max(startIdx, Math.min(state.timeIndex, endIdx));
    const firstIso = state.times[startIdx] ? parseSessionUtc(state.times[startIdx].time_utc) : null;
    const currentIso = state.times[currentIdx] ? parseSessionUtc(state.times[currentIdx].time_utc) : null;
    const lastIso = state.times[endIdx] ? parseSessionUtc(state.times[endIdx].time_utc) : null;
    activeFirstFrameEl.textContent = firstIso ? formatIsoForDisplay(firstIso.toISOString()) : '—';
    if (activeCurrentFrameEl) {
      activeCurrentFrameEl.textContent = currentIso ? formatIsoForDisplay(currentIso.toISOString()) : '—';
    }
    activeLastFrameEl.textContent = lastIso ? formatIsoForDisplay(lastIso.toISOString()) : '—';
    activeFrameCountEl.textContent = String(endIdx - startIdx + 1);
  }

  function nearestStartIndexForTimeMs(targetMs) {
    const n = state.times.length;
    for (let i = 0; i < n; i++) {
      const ms = sessionIndexTimeMs(i);
      if (Number.isFinite(ms) && ms >= targetMs) return i;
    }
    return n - 1;
  }

  function nearestEndIndexForTimeMs(targetMs) {
    for (let i = state.times.length - 1; i >= 0; i--) {
      const ms = sessionIndexTimeMs(i);
      if (Number.isFinite(ms) && ms <= targetMs) return i;
    }
    return 0;
  }

  function nearestIndexInRangeForTimeMs(targetMs, startIdx, endIdx) {
    if (!state.times.length) return 0;
    let bestIdx = Math.max(0, Math.min(startIdx, state.times.length - 1));
    let bestDiff = Infinity;
    for (let i = bestIdx; i <= Math.max(bestIdx, endIdx); i++) {
      const ms = sessionIndexTimeMs(i);
      if (!Number.isFinite(ms)) continue;
      const diff = Math.abs(ms - targetMs);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = i;
      }
    }
    return bestIdx;
  }

  function setEra5Window(startIso, endIso) {
    const nextStart = startIso || '';
    const nextEnd = endIso || '';
    startUtcInput.value = nextStart;
    endUtcInput.value = nextEnd;
    if (era5StartInput) era5StartInput.value = isoToLocalInputValue(nextStart);
    if (era5EndInput) era5EndInput.value = isoToLocalInputValue(nextEnd);
    updateEra5ApplyButtonDirtyState();
  }

  function updateEra5ApplyButtonDirtyState() {
    if (!btnEra5Apply) return;
    const pendingStartIso = localInputValueToIso(era5StartInput ? era5StartInput.value : '');
    const pendingEndIso = localInputValueToIso(era5EndInput ? era5EndInput.value : '');
    const committedStartIso = state.era5StartUtc || null;
    const committedEndIso = state.era5EndUtc || null;
    const isDirty = pendingStartIso !== committedStartIso || pendingEndIso !== committedEndIso;
    btnEra5Apply.classList.toggle('era5-apply-dirty', isDirty);
  }

  function commitEra5Window(startIso, endIso) {
    const nextStart = startIso && startIso.trim ? startIso.trim() : '';
    const nextEnd = endIso && endIso.trim ? endIso.trim() : '';
    state.era5StartUtc = nextStart || null;
    state.era5EndUtc = nextEnd || null;
    setEra5Window(nextStart, nextEnd);
  }

  function syncSessionToEra5Window() {
    const startIso = state.era5StartUtc;
    const endIso = state.era5EndUtc;
    if (!startIso || !endIso) return;
    showApiStatus('Updating ERA5 window: downloading data…');
    startStormSessionAutomatically(true);
  }

  function computeDefaultWindows(series) {
    const barrier = computeBarrierBounds();
    const seriesStartIso = series[0] && series[0].time_utc;
    const seriesEndIso = series[series.length - 1] && series[series.length - 1].time_utc;
    const msPerHour = 3600000;

    function toMs(iso) {
      const d = new Date(iso);
      return Number.isNaN(d.getTime()) ? NaN : d.getTime();
    }

    let mainStartMs;
    let mainEndMs;
    let era5StartMs;
    let era5EndMs;

    if (barrier) {
      const startMs = toMs(barrier.startUtc);
      const endMs = toMs(barrier.endUtc);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) {
        return null;
      }
      mainStartMs = startMs - 62 * msPerHour;
      mainEndMs = endMs + 62 * msPerHour;
      era5StartMs = startMs - 24 * msPerHour;
      era5EndMs = endMs + 24 * msPerHour;
    } else {
      // Fallback: center on max surge time, otherwise midpoint of series
      let centerMs = NaN;
      if (series && series.length) {
        let maxSurge = -Infinity;
        let maxSurgeMs = NaN;
        series.forEach((p) => {
          if (p.surge != null && !Number.isNaN(p.surge)) {
            const tMs = toMs(p.time_utc);
            if (Number.isFinite(tMs) && p.surge > maxSurge) {
              maxSurge = p.surge;
              maxSurgeMs = tMs;
            }
          }
        });
        if (Number.isFinite(maxSurgeMs)) {
          centerMs = maxSurgeMs;
        } else {
          const startMs = toMs(seriesStartIso);
          const endMs = toMs(seriesEndIso);
          if (Number.isFinite(startMs) && Number.isFinite(endMs)) {
            centerMs = startMs + (endMs - startMs) / 2;
          }
        }
      }
      if (!Number.isFinite(centerMs)) {
        return null;
      }
      mainStartMs = centerMs - 62 * msPerHour;
      mainEndMs = centerMs + 62 * msPerHour;
      era5StartMs = centerMs - 24 * msPerHour;
      era5EndMs = centerMs + 24 * msPerHour;
    }

    const clampToSeries = (ms) => {
      const startMs = toMs(seriesStartIso);
      const endMs = toMs(seriesEndIso);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return ms;
      return Math.min(Math.max(ms, startMs), endMs);
    };

    mainStartMs = clampToSeries(mainStartMs);
    mainEndMs = clampToSeries(mainEndMs);
    era5StartMs = clampToSeries(era5StartMs);
    era5EndMs = clampToSeries(era5EndMs);

    return {
      mainStartUtc: new Date(mainStartMs).toISOString(),
      mainEndUtc: new Date(mainEndMs).toISOString(),
      era5StartUtc: new Date(era5StartMs).toISOString(),
      era5EndUtc: new Date(era5EndMs).toISOString()
    };
  }

  async function startStormSessionAutomatically(force) {
    clearError();
    if (!state.selectedStormId) return;
    const startIso = state.era5StartUtc;
    const endIso = state.era5EndUtc;
    if (!startIso || !endIso) return;
    if (new Date(endIso) <= new Date(startIso)) {
      showError('End time must be after start time.');
      return;
    }
    const key = `${state.selectedStormId}|${startIso}|${endIso}`;
    if (!force && state.lastAutoStartKey === key) return;
    const requestId = ++state.autoStartRequestId;
    showApiStatus('Downloading ERA5 data from CDS API… This may take several minutes.');
    try {
      const res = await fetch(`${API}/api/storm/start-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          storm_id: state.selectedStormId,
          start_utc: startIso,
          end_utc: endIso
        })
      });
      const data = await res.json();
      if (requestId !== state.autoStartRequestId) return;
      if (!res.ok) {
        showApiStatus('');
        showError(data.detail || 'Failed to start storm session');
        return;
      }
      state.lastAutoStartKey = key;
      clearApiStatus();
      startTrackingSession(data);
    } catch (err) {
      if (requestId !== state.autoStartRequestId) return;
      clearApiStatus();
      showError('Network error: ' + err.message);
    }
  }

  function queueAutoStartSession(force) {
    if (state.autoStartTimer) {
      clearTimeout(state.autoStartTimer);
      state.autoStartTimer = null;
    }
    state.autoStartTimer = setTimeout(() => {
      startStormSessionAutomatically(Boolean(force));
    }, 350);
  }

  function updateChartViewport(minUtc, maxUtc) {
    if (!minUtc || !maxUtc) return;
    const charts = [state.waterTideChart, state.surgeChart];
    charts.forEach((chart) => {
      if (!chart) return;
      if (chart.options.scales && chart.options.scales.x) {
        chart.options.scales.x.min = minUtc;
        chart.options.scales.x.max = maxUtc;
        chart.update('none');
      }
    });
  }

  function updateCurrentFrameMarker(overrideIso) {
    let markerIso = overrideIso || null;
    if (!markerIso && state.times.length) {
      const current = state.times[state.timeIndex];
      markerIso = current && current.time_utc ? current.time_utc : null;
    }
    if (!markerIso) return;
    const charts = [state.waterTideChart, state.surgeChart];
    charts.forEach((chart) => {
      if (!chart || !chart.options || !chart.options.plugins || !chart.options.plugins.annotation) return;
      if (!chart.options.plugins.annotation.annotations) {
        chart.options.plugins.annotation.annotations = {};
      }
      chart.options.plugins.annotation.annotations['current-frame-line'] = {
        type: 'line',
        xMin: markerIso,
        xMax: markerIso,
        borderColor: 'rgba(220, 0, 0, 0.95)',
        borderWidth: 2,
        borderDash: [0, 0],
        label: {
          display: true,
          content: 'Current Frame',
          position: 'start',
          backgroundColor: 'rgba(255,255,255,0.8)',
          color: '#222',
          padding: 2
        }
      };
      chart.update('none');
    });
  }

  function destroyWaterCharts() {
    if (state.waterTideChart) {
      state.waterTideChart.destroy();
      state.waterTideChart = null;
    }
    if (state.surgeChart) {
      state.surgeChart.destroy();
      state.surgeChart = null;
    }
  }

  function destroyWindowSlider() {
    if (timeWindowSliderEl && timeWindowSliderEl.noUiSlider) {
      timeWindowSliderEl.noUiSlider.destroy();
    }
  }

  function renderWaterLevelChart() {
    destroyWaterCharts();
    const series = state.waterSeries;
    const stormWindows = state.stormWindows || [];
    if (!series.length || typeof Chart === 'undefined') return;
    const labels = series.map((p) => p.time_utc);
    const wlData = series.map((p) => p.water_level != null ? p.water_level : NaN);
    const tideData = series.map((p) => p.tide != null ? p.tide : NaN);
    const surgeData = series.map((p) => p.surge != null ? p.surge : NaN);

    const wlTideVals = wlData.concat(tideData).filter((v) => v != null && !Number.isNaN(v));
    const surgeVals = surgeData.filter((v) => v != null && !Number.isNaN(v));

    const wlTideMin = wlTideVals.length ? Math.min(...wlTideVals) - 0.3 : -0.5;
    const wlTideMax = wlTideVals.length ? Math.max(...wlTideVals) + 0.3 : 4;
    const surgeMin = surgeVals.length ? Math.min(...surgeVals) - 0.1 : -0.5;
    const surgeMax = surgeVals.length ? Math.max(...surgeVals) + 0.1 : 4;

    const waterAnnotations = {};
    const defaultWindows = computeDefaultWindows(series);
    const startIso = state.plotStartUtc || (defaultWindows && defaultWindows.mainStartUtc) || (series[0] && series[0].time_utc);
    const endIso = state.plotEndUtc || (defaultWindows && defaultWindows.mainEndUtc) || (series[series.length - 1] && series[series.length - 1].time_utc);
    const currentPoint = state.times[state.timeIndex];
    const markerIso = currentPoint && currentPoint.time_utc ? currentPoint.time_utc : (startIso || endIso);
    const addLine = function (target, key, xIso, color, labelText, withLabel) {
      if (!xIso) return;
      target[key] = {
        type: 'line',
        xMin: xIso,
        xMax: xIso,
        borderColor: color,
        borderWidth: 2,
        borderDash: [0, 0],
        label: withLabel ? {
          display: true,
          content: labelText,
          position: 'start',
          backgroundColor: 'rgba(255,255,255,0.8)',
          color: '#222',
          padding: 2,
          yAdjust: 0
        } : {
          display: false
        }
      };
    };
    const era5Start = state.era5StartUtc;
    const era5End = state.era5EndUtc;
    addLine(waterAnnotations, 'era5-start-line', era5Start, 'rgba(0, 120, 200, 0.9)', 'ERA5 Start', false);
    addLine(waterAnnotations, 'era5-end-line', era5End, 'rgba(0, 120, 200, 0.9)', 'ERA5 End', false);
    stormWindows.forEach(function (win, i) {
      const box = {
        type: 'box',
        xMin: win.start_utc,
        xMax: win.end_utc,
        yMin: Number.NEGATIVE_INFINITY,
        yMax: Number.POSITIVE_INFINITY,
        backgroundColor: 'rgba(200, 100, 180, 0.15)',
        borderColor: 'rgba(200, 100, 180, 0.5)',
        borderWidth: 1
      };
      waterAnnotations['storm-window-' + i] = box;
    });
    if (markerIso) {
      const markerAnn = {
        type: 'line',
        xMin: markerIso,
        xMax: markerIso,
        borderColor: 'rgba(220, 0, 0, 0.95)',
        borderWidth: 2,
        borderDash: [0, 0],
        label: {
          display: true,
          content: 'Current Frame',
          position: 'start',
          backgroundColor: 'rgba(255,255,255,0.8)',
          color: '#222',
          padding: 2
        }
      };
      waterAnnotations['current-frame-line'] = markerAnn;
    }
    const waterCtx = waterTideChartCanvas.getContext('2d');
    const yMin = Math.min(wlTideMin, surgeMin);
    const yMax = Math.max(wlTideMax, surgeMax);

    state.waterTideChart = new Chart(waterCtx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'Water level (m)', data: wlData, borderColor: '#1e88e5', backgroundColor: 'rgba(30, 136, 229, 0.1)', fill: false, tension: 0.1, pointRadius: 0 },
          { label: 'Predicted tide (m)', data: tideData, borderColor: '#e53935', backgroundColor: 'rgba(229, 57, 53, 0.08)', fill: false, tension: 0.1, pointRadius: 0, hidden: true },
          { label: 'Surge (m)', data: surgeData, borderColor: '#ff6728', backgroundColor: 'rgba(255, 103, 40, 0.1)', fill: false, tension: 0.1, pointRadius: 0 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 1.5,
        interaction: { intersect: false, mode: 'index' },
        scales: {
          x: {
            type: 'time',
            time: { unit: 'hour', displayFormats: { hour: 'HH:mm', day: 'MMM d', month: 'MMM yyyy' } },
            title: { display: true, text: 'Time (UTC)' },
            min: startIso,
            max: endIso,
            ticks: {
              display: true,
              source: 'data',
              callback: function (value) {
                const d = new Date(value);
                if (Number.isNaN(d.getTime())) return '';
                const pad = (n) => String(n).padStart(2, '0');
                const hours = d.getUTCHours();
                const minutes = d.getUTCMinutes();
                if (hours === 0 && minutes === 0) {
                  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
                  return `${months[d.getUTCMonth()]} ${d.getUTCDate()} 00:00`;
                }
                return `${pad(hours)}:${pad(minutes)}`;
              }
            }
          },
          y: {
            title: { display: true, text: 'm' },
            min: yMin,
            max: yMax
          }
        },
        plugins: {
          legend: { display: true },
          annotation: {
            annotations: waterAnnotations
          },
          tooltip: {
            callbacks: {
              label: function (item) {
                const raw = item.raw;
                const str = (raw != null && !Number.isNaN(raw)) ? raw.toFixed(3) : '—';
                return item.dataset.label + ': ' + str;
              }
            }
          }
        }
      }
    });
    updateCurrentFrameMarker(markerIso);
  }

  function startTrackingSession(data) {
    state.sessionId = data.session_id;
    state.times = data.times || [];
    state.bounds = data.bounds || {};
    state.activeStartIdx = 0;
    state.activeEndIdx = Math.max(0, state.times.length - 1);
    state.timeIndex = state.activeStartIdx;
    const currentAnchorIso = state.barrierEndUtc || state.defaultCurrentUtc;
    const currentMs = currentAnchorIso ? new Date(currentAnchorIso).getTime() : NaN;
    if (Number.isFinite(currentMs)) {
      let mappedCurrent = nearestIndexInRangeForTimeMs(
        currentMs,
        state.activeStartIdx,
        state.activeEndIdx
      );
      mappedCurrent = Math.max(state.activeStartIdx, Math.min(mappedCurrent, state.activeEndIdx));
      state.timeIndex = mappedCurrent;
    } else {
      state.timeIndex = state.activeStartIdx;
    }
    state.track = [];
    mapSection.hidden = false;
    if (gifSection) {
      gifSection.hidden = false;
    }
    clearGifPreview();
    updateSlider();
    updateTimeLabel();
    updateFrameWindowStatus();
    updateCurrentFrameMarker();
    updateTrackInfo();
    updateDownloadLink();
    loadFrame();
    fetchTrack();
  }

  async function loadStormCatalog() {
    try {
      const res = await fetch(`${API}/api/storms`);
      if (!res.ok) {
        throw new Error('Failed to load storms');
      }
      const data = await res.json();
      state.storms = data.storms || [];
      stormSelect.innerHTML = '';
      state.storms.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = String(s.storm_id);
        opt.textContent = `${s.storm_id}. ${s.label}`;
        stormSelect.appendChild(opt);
      });
      if (state.storms.length > 0) {
        state.selectedStormId = state.storms[0].storm_id;
        stormSelect.value = String(state.selectedStormId);
        updateStormDetails();
      }
    } catch (e) {
      showError('Could not load storm list: ' + e.message);
    }
  }

  function updateStormDetails() {
    const sid = Number(stormSelect.value);
    const s = state.storms.find((st) => Number(st.storm_id) === sid);
    state.selectedStormId = sid;
    if (!s) return;
    let hoursText = '';
    if (s.storm_start_local && s.storm_end_local) {
      const start = new Date(s.storm_start_local.replace(' ', 'T') + 'Z');
      const end = new Date(s.storm_end_local.replace(' ', 'T') + 'Z');
      if (!Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) && end > start) {
        const diffHours = (end.getTime() - start.getTime()) / 3600000;
        const rounded = Math.round(diffHours * 10) / 10;
        hoursText = ` (${rounded} h)`;
      }
    }
    stormMeta.innerHTML = `
      <div><strong>Barrier closed (local):</strong> ${s.storm_end_local}${hoursText}</div>
    `;
    waterLevelPlaceholder.hidden = false;
    if (waterLevelLoading) waterLevelLoading.hidden = true;
    if (waterLevelError) { waterLevelError.hidden = true; waterLevelError.textContent = ''; }
    waterLevelChartWrap.hidden = true;
    era5WindowSection.hidden = true;
    mapSection.hidden = true;
    if (gifSection) gifSection.hidden = true;
    destroyWaterCharts();
    destroyWindowSlider();
    state.sessionId = null;
    state.times = [];
    state.activeStartIdx = 0;
    state.activeEndIdx = 0;
    state.barrierStartUtc = null;
    state.barrierEndUtc = null;
    state.defaultCurrentUtc = null;
    state.plotStartUtc = null;
    state.plotEndUtc = null;
    state.timeIndex = 0;
    state.track = [];
    state.lastAutoStartKey = null;
    state.waterSeries = [];
    state.stormWindows = [];
    commitEra5Window('', '');
    if (chartRangeStartSelect) chartRangeStartSelect.innerHTML = '';
    if (chartRangeEndSelect) chartRangeEndSelect.innerHTML = '';
    if (activeFirstFrameEl) activeFirstFrameEl.textContent = '—';
    if (activeCurrentFrameEl) activeCurrentFrameEl.textContent = '—';
    if (activeLastFrameEl) activeLastFrameEl.textContent = '—';
    if (activeFrameCountEl) activeFrameCountEl.textContent = '0';

    const hasSeries = s.has_water_level_series !== false;
    if (!hasSeries) {
      waterLevelPlaceholder.textContent = 'No water level series for this storm.';
      return;
    }
    waterLevelPlaceholder.hidden = true;
    loadWaterLevelSeries(sid);
  }

  function loadWaterLevelSeries(sid) {
    if (!sid) return;
    destroyWaterCharts();
    destroyWindowSlider();
    state.waterSeries = [];
    if (waterLevelLoading) waterLevelLoading.hidden = false;
    if (waterLevelError) waterLevelError.hidden = true;
    const url = `${API}/api/storms/${sid}/water_level_series`;
    fetch(url)
      .then(function (res) {
        if (waterLevelLoading) waterLevelLoading.hidden = true;
        if (!res.ok) throw new Error('Could not load series');
        return res.json();
      })
      .then(function (data) {
        const series = data.series || [];
        state.waterSeries = series;
        state.stormWindows = data.storm_windows || [];
        if (series.length === 0) {
          if (waterLevelError) { waterLevelError.textContent = 'No water level data in window.'; waterLevelError.hidden = false; }
          return;
        }
        waterLevelChartWrap.hidden = false;
        era5WindowSection.hidden = false;
        const barrier = computeBarrierBounds();
        if (barrier) {
          state.barrierStartUtc = barrier.startUtc;
          state.barrierEndUtc = barrier.endUtc;
          state.defaultCurrentUtc = barrier.endUtc;
        } else {
          state.barrierStartUtc = null;
          state.barrierEndUtc = null;
          state.defaultCurrentUtc = null;
        }
        const defaults = computeDefaultWindows(series);
        if (defaults) {
          // ERA5 window defaults to 24h before/after barrier.
          commitEra5Window(defaults.era5StartUtc, defaults.era5EndUtc);
          populateChartRangeControls();
          applyChartRange(defaults.mainStartUtc, defaults.mainEndUtc);
        } else {
          commitEra5Window('', '');
          populateChartRangeControls();
          if (series.length) {
            applyChartRange(series[0].time_utc, series[series.length - 1].time_utc);
          }
        }
        renderWaterLevelChart();
        updateEra5LineAnnotations(state.era5StartUtc, state.era5EndUtc);
        queueAutoStartSession(true);
      })
      .catch(function (e) {
        if (waterLevelLoading) waterLevelLoading.hidden = true;
        if (waterLevelError) { waterLevelError.textContent = e.message || 'Error loading series'; waterLevelError.hidden = false; }
      });
  }

  if (btnGenerateGif) {
    btnGenerateGif.addEventListener('click', () => {
      generateGif();
    });
  }
  if (btnDownloadGif) {
    btnDownloadGif.addEventListener('click', () => {
      downloadGif();
    });
  }

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener('change', () => { state.mode = radio.value; });
  });

  clickLayer.addEventListener('click', (e) => {
    if (!state.sessionId || !state.bounds) return;
    const x = e.clientX;
    const y = e.clientY;
    const pt = pixelToLonLat(x, y);
    if (!pt) return;
    if (state.mode === 'add') {
      addPoint(pt.lon, pt.lat);
    } else {
      const idx = state.track.findIndex(p => p.time_index === state.timeIndex);
      if (idx >= 0) deletePoint(state.timeIndex);
      else deleteLastPoint();
    }
  });

  frameImg.addEventListener('load', () => {
    const w = frameImg.naturalWidth;
    const h = frameImg.naturalHeight;
    clickLayer.width = w;
    clickLayer.height = h;
    clickLayer.style.width = '100%';
    clickLayer.style.height = 'auto';
  });
  stormSelect.addEventListener('change', () => {
    updateStormDetails();
  });

  btnSaveCombined.addEventListener('click', async () => {
    if (!state.sessionId || state.track.length === 0) return;
    try {
      btnSaveCombined.disabled = true;
      const res = await fetch(`${API}/api/combined/save/${state.sessionId}`, {
        method: 'POST'
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showError(data.detail || 'Failed to save combined CSV');
        return;
      }
    } catch (e) {
      showError('Network error: ' + e.message);
    } finally {
      updateTrackInfo();
    }
  });

  if (chartRangeStartSelect) {
    chartRangeStartSelect.addEventListener('change', () => {
      if (!chartRangeStartSelect.value || !chartRangeEndSelect || !chartRangeEndSelect.value) return;
      applyChartRange(chartRangeStartSelect.value, chartRangeEndSelect.value);
      renderWaterLevelChart();
    });
  }

  if (chartRangeEndSelect) {
    chartRangeEndSelect.addEventListener('change', () => {
      if (!chartRangeEndSelect.value || !chartRangeStartSelect || !chartRangeStartSelect.value) return;
      applyChartRange(chartRangeStartSelect.value, chartRangeEndSelect.value);
      renderWaterLevelChart();
    });
  }

  if (btnEra5Apply) {
    btnEra5Apply.addEventListener('click', () => {
      const startIso = localInputValueToIso(era5StartInput ? era5StartInput.value : '');
      const endIso = localInputValueToIso(era5EndInput ? era5EndInput.value : '');
      if (!startIso || !endIso) {
        showError('Please set both ERA5 start and end times.');
        return;
      }
      if (new Date(endIso) <= new Date(startIso)) {
        showError('ERA5 end time must be after start time.');
        return;
      }
      clearError();
      commitEra5Window(startIso, endIso);
      updateEra5LineAnnotations(state.era5StartUtc, state.era5EndUtc);
      renderWaterLevelChart();
      syncSessionToEra5Window();
    });
  }

  [era5StartInput, era5EndInput].forEach((inputEl) => {
    if (!inputEl) return;
    inputEl.addEventListener('input', updateEra5ApplyButtonDirtyState);
    inputEl.addEventListener('change', updateEra5ApplyButtonDirtyState);
  });

  updateEra5ApplyButtonDirtyState();

  loadStormCatalog();
})();
