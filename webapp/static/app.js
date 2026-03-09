(function () {
  const API = ''; // same origin
  let state = {
    sessionId: null,
    bounds: null,
    times: [],
    timeIndex: 0,
    track: [],
    mode: 'add'
  };

  const uploadZone = document.getElementById('upload-zone');
  const fileInput = document.getElementById('file-input');
  const uploadError = document.getElementById('upload-error');
  const uploadSection = document.getElementById('upload-section');
  const mapSection = document.getElementById('map-section');
  const frameImg = document.getElementById('frame-img');
  const clickLayer = document.getElementById('click-layer');
  const timeSlider = document.getElementById('time-slider');
  const timeLabel = document.getElementById('time-label');
  const btnPrev = document.getElementById('btn-prev');
  const btnNext = document.getElementById('btn-next');
  const btnDownload = document.getElementById('btn-download');
  const trackInfo = document.getElementById('track-info');

  function showError(msg) {
    uploadError.textContent = msg;
    uploadError.hidden = false;
  }

  function clearError() {
    uploadError.textContent = '';
    uploadError.hidden = true;
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
      : `${n} track point(s). Download CSV when done.`;
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

  uploadZone.addEventListener('click', () => fileInput.click());
  uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('dragover');
  });
  uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
  uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file && file.name.toLowerCase().endsWith('.nc')) handleFile(file);
    else showError('Please drop a .nc file.');
  });
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
      state.sessionId = data.session_id;
      state.times = data.times || [];
      state.bounds = data.bounds || {};
      state.timeIndex = 0;
      state.track = [];
      uploadSection.hidden = true;
      mapSection.hidden = false;
      updateSlider();
      updateTimeLabel();
      updateTrackInfo();
      updateDownloadLink();
      loadFrame();
      await fetchTrack();
    } catch (e) {
      showError('Network error: ' + e.message);
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
})();
