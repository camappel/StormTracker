(function () {
  const API = ''; // same origin
  let state = {
    sessionId: null,
    bounds: null,
    times: [],
    timeIndex: 0,
    track: [],
    mode: 'add',
    storms: [],
    selectedStormId: null
  };

  const stormSelect = document.getElementById('storm-select');
  const stormMeta = document.getElementById('storm-meta');
  const waterLevelImg = document.getElementById('water-level-img');
  const waterLevelPlaceholder = document.getElementById('water-level-placeholder');
  const era5Form = document.getElementById('era5-form');
  const startUtcInput = document.getElementById('start-utc');
  const endUtcInput = document.getElementById('end-utc');
  const btnStartStorm = document.getElementById('btn-start-storm');
  const btnUploadNc = document.getElementById('btn-upload-nc');
  const fileInput = document.getElementById('file-input');
  const uploadError = document.getElementById('upload-error');
  const apiStatus = document.getElementById('api-status');
  const mapSection = document.getElementById('map-section');
  const frameImg = document.getElementById('frame-img');
  const clickLayer = document.getElementById('click-layer');
  const timeSlider = document.getElementById('time-slider');
  const timeLabel = document.getElementById('time-label');
  const btnPrev = document.getElementById('btn-prev');
  const btnNext = document.getElementById('btn-next');
  const btnDownload = document.getElementById('btn-download');
  const btnSaveCombined = document.getElementById('btn-save-combined');
  const btnDownloadCombined = document.getElementById('btn-download-combined');
  const trackInfo = document.getElementById('track-info');

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
    const rect = clickLayer.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    const fracX = (x - rect.left) / w;
    const fracY = (y - rect.top) / h;
    const lon = b.lon_min + fracX * (b.lon_max - b.lon_min);
    const lat = b.lat_max - fracY * (b.lat_max - b.lat_min);
    return { lon, lat };
  }

  function updateTimeLabel() {
    const n = state.times.length;
    const i = state.timeIndex + 1;
    const t = state.times[state.timeIndex];
    timeLabel.textContent = `Step ${i} / ${n}${t ? ' — ' + t.time_utc : ''}`;
  }

  function updateSlider() {
    const n = state.times.length;
    timeSlider.min = 0;
    timeSlider.max = Math.max(0, n - 1);
    timeSlider.value = state.timeIndex;
    timeSlider.disabled = n <= 1;
    btnPrev.disabled = n <= 1 || state.timeIndex <= 0;
    btnNext.disabled = n <= 1 || state.timeIndex >= n - 1;
  }

  function updateTrackInfo() {
    const n = state.track.length;
    trackInfo.textContent = n === 0
      ? 'No track points. Click on map in "Add point" mode to add.'
      : `${n} track point(s). Save to combined CSV when done.`;
    btnSaveCombined.disabled = !state.sessionId || n === 0;
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

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) handleFile(file);
  });

  async function handleFile(file) {
    clearError();
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch(`${API}/api/upload`, {
        method: 'POST',
        body: form
      });
      const data = await res.json();
      if (!res.ok) {
        showError(data.detail || 'Upload failed');
        return;
      }
      startTrackingSession(data);
    } catch (e) {
      showError('Network error: ' + e.message);
    }
  }

  function isoToLocalDatetimeValue(isoString) {
    if (!isoString) return '';
    const d = new Date(isoString);
    const pad = (n) => String(n).padStart(2, '0');
    const yyyy = d.getUTCFullYear();
    const mm = pad(d.getUTCMonth() + 1);
    const dd = pad(d.getUTCDate());
    const hh = pad(d.getUTCHours());
    const min = pad(d.getUTCMinutes());
    return `${yyyy}-${mm}-${dd}T${hh}:${min}`;
  }

  function localDatetimeValueToIso(value) {
    if (!value) return null;
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
  }

  function startTrackingSession(data) {
    state.sessionId = data.session_id;
    state.times = data.times || [];
    state.bounds = data.bounds || {};
    state.timeIndex = 0;
    state.track = [];
    mapSection.hidden = false;
    updateSlider();
    updateTimeLabel();
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
    stormMeta.innerHTML = `
      <div><strong>Start (local):</strong> ${s.storm_start_local}</div>
      <div><strong>End (local):</strong> ${s.storm_end_local}</div>
    `;
    startUtcInput.value = isoToLocalDatetimeValue(s.default_start_utc);
    endUtcInput.value = isoToLocalDatetimeValue(s.default_end_utc);

    if (s.has_water_level_plot) {
      waterLevelPlaceholder.hidden = true;
      waterLevelImg.hidden = false;
      waterLevelImg.src = `${API}/api/storms/${sid}/water_level?t=${Date.now()}`;
    } else {
      waterLevelImg.hidden = true;
      waterLevelPlaceholder.hidden = false;
      waterLevelPlaceholder.textContent = 'No pre-generated water level plot for this storm.';
    }
  }

  btnPrev.addEventListener('click', () => {
    if (state.times.length === 0 || state.timeIndex <= 0) return;
    state.timeIndex--;
    updateSlider();
    updateTimeLabel();
    loadFrame();
  });
  btnNext.addEventListener('click', () => {
    const n = state.times.length;
    if (n === 0 || state.timeIndex >= n - 1) return;
    state.timeIndex++;
    updateSlider();
    updateTimeLabel();
    loadFrame();
  });

  timeSlider.addEventListener('input', () => {
    const n = state.times.length;
    if (n === 0) return;
    const v = parseInt(timeSlider.value, 10);
    state.timeIndex = Math.max(0, Math.min(v, n - 1));
    updateTimeLabel();
    loadFrame();
  });

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener('change', () => { state.mode = radio.value; });
  });

  clickLayer.addEventListener('click', (e) => {
    if (!state.sessionId || !state.bounds) return;
    const rect = clickLayer.getBoundingClientRect();
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

  era5Form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearError();
    clearApiStatus();
    if (!state.selectedStormId) {
      showError('Select a storm first.');
      return;
    }
    const startIso = localDatetimeValueToIso(startUtcInput.value);
    const endIso = localDatetimeValueToIso(endUtcInput.value);
    if (!startIso || !endIso) {
      showError('Provide valid start and end datetimes.');
      return;
    }
    try {
      btnStartStorm.disabled = true;
      showApiStatus('Downloading ERA5 data from CDS API… This may take several minutes.');
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
      if (!res.ok) {
        showApiStatus('');
        showError(data.detail || 'Failed to start storm session');
        return;
      }
      clearApiStatus();
      startTrackingSession(data);
    } catch (err) {
      clearApiStatus();
      showError('Network error: ' + err.message);
    } finally {
      btnStartStorm.disabled = false;
    }
  });

  btnUploadNc.addEventListener('click', () => fileInput.click());

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) handleFile(file);
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

  loadStormCatalog();
})();
