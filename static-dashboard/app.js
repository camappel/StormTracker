(function () {
  const DATA = window.STORM_DASHBOARD_DATA || { storms: [], generated_at: null };
  const BARRIER_LABELS = {
    thames: "Thames",
    eastern_scheldt: "ES",
    es: "ES",
    both: "Both"
  };
  const HYDRO_BARRIERS = ["thames", "eastern_scheldt"];
  const FRAME_PRODUCTS = [
    { key: "era5", label: "ERA5 MSLP" },
    { key: "wind", label: "ERA5 wind" },
    { key: "gtsm", label: "GTSM surge" }
  ];
  const CATALOG_END_YEAR = 2026;
  const COLORS = {
    ink: "#1f2933",
    muted: "#5e6b75",
    grid: "#d8e0e5",
    land: "#e4ebdf",
    sea: "#eef6f8",
    water: "#1f6fba",
    tide: "#4f7d32",
    surge: "#c64d4d",
    closure: "rgba(185, 121, 24, 0.16)",
    closureEdge: "rgba(185, 121, 24, 0.52)"
  };

  const state = {
    query: "",
    barrier: "all",
    type: "all",
    sort: "date-desc",
    selectedStormId: null,
    selectedHydroBarrier: null,
    frameIndex: 0,
    hydroChart: null,
    gtsmChart: null
  };

  const els = {
    summaryStorms: document.getElementById("summary-storms"),
    summaryClosures: document.getElementById("summary-closures"),
    summaryYears: document.getElementById("summary-years"),
    search: document.getElementById("storm-search"),
    barrierFilter: document.getElementById("barrier-filter"),
    typeFilter: document.getElementById("type-filter"),
    sortSelect: document.getElementById("sort-select"),
    listCount: document.getElementById("list-count"),
    stormList: document.getElementById("storm-list"),
    detailBadges: document.getElementById("detail-badges"),
    detailTitle: document.getElementById("detail-title"),
    detailPeriod: document.getElementById("detail-period"),
    hydroSwitch: document.getElementById("hydro-switch"),
    metricGrid: document.getElementById("metric-grid"),
    frameCaption: document.getElementById("frame-caption"),
    frameTimestamp: document.getElementById("frame-timestamp"),
    frameSlider: document.getElementById("frame-slider"),
    frameGrid: document.getElementById("frame-grid"),
    framePrev: document.getElementById("btn-frame-prev"),
    frameNext: document.getElementById("btn-frame-next"),
    hydroCaption: document.getElementById("hydro-caption"),
    hydroCanvas: document.getElementById("hydro-canvas"),
    gtsmCaption: document.getElementById("gtsm-caption"),
    gtsmCanvas: document.getElementById("gtsm-canvas"),
    closureCaption: document.getElementById("closure-caption"),
    closureTable: document.getElementById("closure-table")
  };

  function init() {
    configureCharting();
    const storms = DATA.storms || [];
    state.selectedStormId = storms[0] ? storms[0].storm_id : null;
    renderSummary();
    bindEvents();
    render();
    window.addEventListener("resize", debounce(resizeHydrograph, 80));
  }

  function configureCharting() {
    if (typeof Chart === "undefined") return;
    if (window["chartjs-plugin-annotation"]) {
      Chart.register(window["chartjs-plugin-annotation"]);
    }
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = COLORS.muted;
  }

  function resizeHydrograph() {
    let resized = false;
    if (state.hydroChart) {
      state.hydroChart.resize();
      resized = true;
    }
    if (state.gtsmChart) {
      state.gtsmChart.resize();
      resized = true;
    }
    if (!resized) renderCanvases();
  }

  function bindEvents() {
    els.search.addEventListener("input", function () {
      state.query = els.search.value.trim().toLowerCase();
      render();
    });

    els.barrierFilter.addEventListener("click", function (event) {
      const button = event.target.closest("button[data-filter]");
      if (!button) return;
      state.barrier = button.dataset.filter;
      els.barrierFilter.querySelectorAll(".segment").forEach(function (node) {
        node.classList.toggle("is-active", node === button);
      });
      render();
    });

    els.typeFilter.addEventListener("change", function () {
      state.type = els.typeFilter.value;
      render();
    });

    els.sortSelect.addEventListener("change", function () {
      state.sort = els.sortSelect.value;
      render();
    });

    els.frameSlider.addEventListener("input", function () {
      state.frameIndex = Number(els.frameSlider.value) || 0;
      renderFrameBrowser(selectedStorm());
    });

    els.framePrev.addEventListener("click", function () {
      state.frameIndex = Math.max(0, state.frameIndex - 1);
      renderFrameBrowser(selectedStorm());
    });

    els.frameNext.addEventListener("click", function () {
      const storm = selectedStorm();
      const count = frameCount(storm);
      state.frameIndex = Math.min(Math.max(0, count - 1), state.frameIndex + 1);
      renderFrameBrowser(storm);
    });
  }

  function render() {
    const storms = filteredStorms();
    const previousStormId = state.selectedStormId;
    if (!storms.some(function (storm) { return storm.storm_id === state.selectedStormId; })) {
      state.selectedStormId = storms[0] ? storms[0].storm_id : null;
    }
    if (previousStormId !== state.selectedStormId) {
      state.frameIndex = 0;
    }
    renderList(storms);
    renderDetail(selectedStorm());
  }

  function renderSummary() {
    const storms = DATA.storms || [];
    const closures = storms.reduce(function (sum, storm) {
      return sum + ((storm.closures || []).length || 0);
    }, 0);
    const years = storms
      .map(function (storm) { return storm.year; })
      .filter(function (year) { return Number.isFinite(Number(year)); });
    const maxYear = years.length ? Math.max(Math.max.apply(null, years), CATALOG_END_YEAR) : CATALOG_END_YEAR;
    const yearSpan = years.length ? `${Math.min.apply(null, years)}-${maxYear}` : "0";
    els.summaryStorms.textContent = String(storms.length);
    els.summaryClosures.textContent = String(closures);
    els.summaryYears.textContent = yearSpan;
  }

  function filteredStorms() {
    const query = state.query;
    const storms = (DATA.storms || []).filter(function (storm) {
      if (state.barrier !== "all" && storm.barrier_class !== state.barrier) return false;
      if (state.type === "unset") {
        if (storm.storm_type) return false;
      } else if (state.type !== "all" && storm.storm_type !== state.type) {
        return false;
      }
      if (!query) return true;
      return searchText(storm).includes(query);
    });
    storms.sort(compareStorms);
    return storms;
  }

  function compareStorms(a, b) {
    if (state.sort === "date-asc") return dateValue(a) - dateValue(b);
    if (state.sort === "pressure-asc") return pressureValue(a) - pressureValue(b);
    if (state.sort === "duration-desc") return durationValue(b) - durationValue(a);
    return dateValue(b) - dateValue(a);
  }

  function searchText(storm) {
    const barriers = (storm.barriers || []).map(function (b) { return barrierLabel(b); }).join(" ");
    const closureText = (storm.closures || []).map(function (closure) {
      return [closure.closure_id, closure.start, closure.end, closure.high_water_time, barrierLabel(closure.barrier)].join(" ");
    }).join(" ");
    return [
      storm.storm,
      storm.label,
      storm.storm_type,
      storm.barrier_class_label,
      storm.year,
      barriers,
      closureText
    ].join(" ").toLowerCase();
  }

  function renderList(storms) {
    els.listCount.textContent = `${storms.length} ${storms.length === 1 ? "storm" : "storms"}`;
    els.stormList.innerHTML = "";
    if (!storms.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No storms match the current filters.";
      els.stormList.appendChild(empty);
      return;
    }

    const frag = document.createDocumentFragment();
    storms.forEach(function (storm) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "storm-row";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", storm.storm_id === state.selectedStormId ? "true" : "false");
      button.classList.toggle("is-selected", storm.storm_id === state.selectedStormId);
      button.dataset.stormId = String(storm.storm_id);

      const title = document.createElement("div");
      title.innerHTML = `<div class="storm-name">${escapeHtml(storm.label)}</div><div class="storm-date">${escapeHtml(shortDate(storm.display_start))}</div>`;
      button.appendChild(title);

      const pressure = document.createElement("div");
      pressure.className = "storm-pressure";
      pressure.textContent = formatPressure(storm.track_stats && storm.track_stats.min_pressure_hpa);
      button.appendChild(pressure);

      const meta = document.createElement("div");
      meta.className = "storm-row-meta";
      meta.appendChild(badge(storm.barrier_class, storm.barrier_class_label));
      meta.appendChild(typePill(storm.storm_type));
      const closures = document.createElement("span");
      closures.className = "badge";
      closures.textContent = `${(storm.closures || []).length} closure${(storm.closures || []).length === 1 ? "" : "s"}`;
      meta.appendChild(closures);
      button.appendChild(meta);

      button.addEventListener("click", function () {
        state.selectedStormId = storm.storm_id;
        state.frameIndex = 0;
        render();
      });
      frag.appendChild(button);
    });
    els.stormList.appendChild(frag);
  }

  function renderDetail(storm) {
    if (!storm) {
      els.detailBadges.innerHTML = "";
      els.detailTitle.textContent = "No storm selected";
      els.detailPeriod.textContent = "";
      els.hydroSwitch.innerHTML = "";
      els.metricGrid.innerHTML = "";
      els.closureTable.innerHTML = "";
      renderFrameBrowser(null);
      destroyHydroChart();
      destroyGtsmChart();
      clearCanvas(els.hydroCanvas, "No storm selected");
      clearCanvas(els.gtsmCanvas, "No storm selected");
      return;
    }

    els.detailBadges.innerHTML = "";
    els.detailBadges.appendChild(badge(storm.barrier_class, storm.barrier_class_label));
    els.detailBadges.appendChild(typePill(storm.storm_type));
    els.detailTitle.textContent = storm.label;
    els.detailPeriod.textContent = `${formatDateTime(storm.display_start)} to ${formatDateTime(storm.display_end)}`;
    renderHydroSwitch(storm);
    renderMetrics(storm);
    renderFrameBrowser(storm);
    renderClosures(storm);
    renderCanvases();
  }

  function renderHydroSwitch(storm) {
    const barriers = hydroBarriers(storm);
    const selected = normalizeBarrier(state.selectedHydroBarrier);
    if (!selected || !barriers.includes(selected)) {
      state.selectedHydroBarrier = preferredHydroBarrier(storm, barriers);
    }
    els.hydroSwitch.innerHTML = "";
    barriers.forEach(function (barrier) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = barrier === normalizeBarrier(state.selectedHydroBarrier) ? "is-active" : "";
      button.textContent = barrierLabel(barrier);
      button.title = hydroSwitchTitle(storm, barrier);
      button.addEventListener("click", function () {
        state.selectedHydroBarrier = barrier;
        renderMetrics(storm);
        drawHydrograph(storm);
        drawGtsmChart(storm);
      });
      els.hydroSwitch.appendChild(button);
    });
  }

  function hydroBarriers(storm) {
    return HYDRO_BARRIERS.slice();
  }

  function preferredHydroBarrier(storm, barriers) {
    const closureBarrier = barriers.find(function (barrier) {
      return (storm.closures || []).some(function (closure) {
        return normalizeBarrier(closure.barrier) === barrier;
      });
    });
    return barriers.find(function (barrier) {
      return hasObservedSeries(storm, barrier);
    }) || closureBarrier || barriers.find(function (barrier) {
      return hasGtsmSeries(storm, barrier);
    }) || barriers[0] || "thames";
  }

  function hasObservedSeries(storm, barrier) {
    const series = storm.series && storm.series[normalizeBarrier(barrier)];
    return !!(series && Array.isArray(series.points) && series.points.length);
  }

  function hasGtsmSeries(storm, barrier) {
    const gauges = storm && storm.gtsm_series && storm.gtsm_series.gauges;
    const gauge = gaugeKeyForBarrier(barrier);
    const points = gauges && gauges[gauge] && gauges[gauge].points;
    return Array.isArray(points) && points.length > 0;
  }

  function hydroSwitchTitle(storm, barrier) {
    const label = barrierLabel(barrier);
    if (hasObservedSeries(storm, barrier)) return `${label} observed hydrograph available`;
    if (hasGtsmSeries(storm, barrier)) return `${label} GTSM series available`;
    return `${label} data unavailable for this storm window`;
  }

  function renderMetrics(storm) {
    const selectedSeries = seriesForSelectedBarrier(storm);
    const stats = selectedSeries ? selectedSeries.stats || {} : {};
    const metrics = [
      {
        label: "Lowest MSLP",
        value: formatPressure(storm.track_stats && storm.track_stats.min_pressure_hpa),
        note: shortDate(storm.track_stats && storm.track_stats.min_pressure_time)
      },
      {
        label: "Storm type",
        value: storm.storm_type || "Unset",
        note: "Classification"
      },
      {
        label: "Peak level",
        value: formatMetres(stats.max_water_level),
        note: peakLevelContext(state.selectedHydroBarrier)
      },
      {
        label: "Peak surge",
        value: formatMetres(stats.max_surge),
        note: shortDate(stats.max_surge_time)
      },
      {
        label: "Closures",
        value: String((storm.closures || []).length),
        note: storm.barrier_class_label || "Catalog"
      }
    ];

    els.metricGrid.innerHTML = "";
    metrics.forEach(function (item) {
      const card = document.createElement("div");
      card.className = "metric-card";
      card.innerHTML = `
        <span class="metric-label">${escapeHtml(item.label)}</span>
        <span class="metric-value">${escapeHtml(item.value)}</span>
        <span class="metric-note">${escapeHtml(item.note || "")}</span>
      `;
      els.metricGrid.appendChild(card);
    });
  }

  function renderClosures(storm) {
    const closures = storm.closures || [];
    els.closureCaption.textContent = `${closures.length} record${closures.length === 1 ? "" : "s"}`;
    els.closureTable.innerHTML = "";
    if (!closures.length) {
      els.closureTable.innerHTML = '<tr><td colspan="5">No closure records for this storm.</td></tr>';
      return;
    }
    const frag = document.createDocumentFragment();
    closures.forEach(function (closure) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${escapeHtml(barrierLabel(closure.barrier))}</td>
        <td>${escapeHtml(formatDateTime(closure.start))}</td>
        <td>${escapeHtml(formatDateTime(closure.end))}</td>
        <td>${escapeHtml(formatDateTime(closure.high_water_time))}</td>
        <td>${escapeHtml(closure.closure_id == null ? "-" : String(closure.closure_id))}</td>
      `;
      frag.appendChild(row);
    });
    els.closureTable.appendChild(frag);
  }

  function renderFrameBrowser(storm) {
    if (!storm) {
      els.frameCaption.textContent = "";
      els.frameTimestamp.textContent = "No cached frames";
      els.frameSlider.disabled = true;
      els.frameSlider.max = "0";
      els.frameSlider.value = "0";
      els.framePrev.disabled = true;
      els.frameNext.disabled = true;
      els.frameGrid.innerHTML = "";
      return;
    }

    const count = frameCount(storm);
    state.frameIndex = Math.max(0, Math.min(state.frameIndex, Math.max(0, count - 1)));
    els.frameSlider.disabled = count <= 1;
    els.frameSlider.min = "0";
    els.frameSlider.max = String(Math.max(0, count - 1));
    els.frameSlider.value = String(state.frameIndex);
    els.framePrev.disabled = count <= 1 || state.frameIndex <= 0;
    els.frameNext.disabled = count <= 1 || state.frameIndex >= count - 1;
    els.frameCaption.textContent = count ? `${state.frameIndex + 1} / ${count}` : "No frame files";

    const activeTime = frameTimes(storm)[state.frameIndex] || firstFrameTime(storm) || null;
    els.frameTimestamp.textContent = activeTime ? formatDateTime(activeTime) : "No cached frames";

    els.frameGrid.innerHTML = "";
    FRAME_PRODUCTS.forEach(function (product) {
      els.frameGrid.appendChild(frameCard(storm, product, activeTime));
    });
    preloadFrameNeighbors(storm);
    updateFrameMarkers(activeTime);
  }

  function frameCard(storm, product, activeTime) {
    const productFrames = storm.frames && storm.frames.products ? storm.frames.products[product.key] : null;
    const frame = frameForProduct(productFrames, activeTime, state.frameIndex);
    const card = document.createElement("article");
    card.className = `frame-card frame-card-${product.key}`;
    const count = productFrames && productFrames.frames ? productFrames.frames.length : 0;
    const note = count ? `${count} files` : "Not exported";
    card.innerHTML = `
      <div class="frame-card-header">
        <span class="frame-card-title">${escapeHtml(product.label)}</span>
        <span class="frame-card-note">${escapeHtml(note)}</span>
      </div>
      <div class="frame-image-box"></div>
    `;
    const box = card.querySelector(".frame-image-box");
    if (!frame || !frame.src) {
      const empty = document.createElement("div");
      empty.className = "frame-empty";
      empty.textContent = `${product.label} frames are not exported for this storm.`;
      box.appendChild(empty);
      return card;
    }
    const img = document.createElement("img");
    img.src = frame.src;
    img.alt = `${product.label} frame for ${storm.label} at ${formatDateTime(frame.time_utc || activeTime)}`;
    img.loading = "eager";
    img.decoding = "async";
    img.onerror = function () {
      box.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "frame-empty";
      empty.textContent = "The listed frame file could not be loaded.";
      box.appendChild(empty);
    };
    box.appendChild(img);
    return card;
  }

  function frameCount(storm) {
    if (!storm || !storm.frames) return 0;
    const times = frameTimes(storm);
    if (times.length) return times.length;
    const products = storm.frames.products || {};
    return Math.max.apply(null, [0].concat(Object.keys(products).map(function (key) {
      const frames = products[key] && products[key].frames;
      return Array.isArray(frames) ? frames.length : 0;
    })));
  }

  function frameTimes(storm) {
    if (!storm || !storm.frames || !Array.isArray(storm.frames.times)) return [];
    return storm.frames.times;
  }

  function firstFrameTime(storm) {
    const products = storm && storm.frames ? storm.frames.products || {} : {};
    for (const product of FRAME_PRODUCTS) {
      const frames = products[product.key] && products[product.key].frames;
      if (Array.isArray(frames) && frames[0] && frames[0].time_utc) return frames[0].time_utc;
    }
    return null;
  }

  function frameForProduct(productFrames, activeTime, index) {
    if (!productFrames || !Array.isArray(productFrames.frames) || !productFrames.frames.length) return null;
    const frames = productFrames.frames;
    if (frames[index] && (!activeTime || frames[index].time_utc === activeTime)) return frames[index];
    if (!activeTime) return frames[Math.min(index, frames.length - 1)];
    const activeMs = Date.parse(activeTime);
    if (!Number.isFinite(activeMs)) return frames[Math.min(index, frames.length - 1)];
    let best = frames[0];
    let bestDelta = Infinity;
    frames.forEach(function (frame) {
      const ms = Date.parse(frame.time_utc);
      if (!Number.isFinite(ms)) return;
      const delta = Math.abs(ms - activeMs);
      if (delta < bestDelta) {
        best = frame;
        bestDelta = delta;
      }
    });
    return best;
  }

  function preloadFrameNeighbors(storm) {
    if (!storm || !storm.frames || !storm.frames.products) return;
    const times = frameTimes(storm);
    [-2, -1, 1, 2].forEach(function (offset) {
      const idx = state.frameIndex + offset;
      if (idx < 0 || idx >= frameCount(storm)) return;
      const time = times[idx] || null;
      FRAME_PRODUCTS.forEach(function (product) {
        const productFrames = storm.frames.products[product.key];
        const frame = frameForProduct(productFrames, time, idx);
        if (!frame || !frame.src) return;
        const img = new Image();
        img.src = frame.src;
      });
    });
  }

  function renderCanvases() {
    const storm = selectedStorm();
    if (!storm) return;
    drawHydrograph(storm);
    drawGtsmChart(storm);
  }

  function drawHydrograph(storm) {
    if (typeof Chart !== "undefined") {
      renderHydroChart(storm);
      return;
    }
    drawHydrographFallback(storm);
  }

  function renderHydroChart(storm) {
    const canvas = els.hydroCanvas;
    const series = seriesForSelectedBarrier(storm);

    if (!series || !series.points || !series.points.length) {
      destroyHydroChart();
      clearCanvas(canvas, "No water-level data in this window");
      els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | no observed data`;
      return;
    }

    const points = hydroPoints(series);
    if (!points.length || !hydroValues(points).length) {
      destroyHydroChart();
      clearCanvas(canvas, "No finite values in this window");
      els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | no finite observed values`;
      return;
    }

    const yRange = hydroYRange(points);
    const firstIso = points[0].iso;
    const lastIso = points[points.length - 1].iso;
    const markerIso = frameTimes(storm)[state.frameIndex] || firstFrameTime(storm) || null;
    const highWaters = highWaterPoints(storm, points);
    const datasets = [
      hydroLineDataset("Water level", points, "water", COLORS.water, 2.6),
      hydroLineDataset("Tide", points, "tide", COLORS.tide, 2),
      hydroLineDataset("Surge", points, "surge", COLORS.surge, 2)
    ];

    if (highWaters.length) {
      datasets.push({
        label: "High water",
        type: "scatter",
        data: highWaters,
        showLine: false,
        pointStyle: "star",
        pointRadius: 8,
        pointHoverRadius: 10,
        borderColor: "#7a4f0c",
        backgroundColor: "#f2b84b",
        borderWidth: 1.4
      });
    }

    destroyHydroChart();
    state.hydroChart = new Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        normalized: true,
        interaction: { intersect: false, mode: "nearest", axis: "x" },
        elements: {
          point: { hitRadius: 10 }
        },
        scales: {
          x: {
            type: "time",
            min: firstIso,
            max: lastIso,
            time: {
              unit: "hour",
              displayFormats: { hour: "HH:mm", day: "MMM d", month: "MMM yyyy" }
            },
            title: { display: true, text: "Time (UTC)" },
            grid: { color: COLORS.grid },
            ticks: {
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 8,
              callback: function (value) {
                return compactDate(value);
              }
            }
          },
          y: {
            min: yRange.min,
            max: yRange.max,
            title: { display: true, text: hydroYAxisTitle() },
            grid: { color: COLORS.grid },
            ticks: {
              callback: function (value) {
                const num = Number(value);
                return Number.isFinite(num) ? `${num.toFixed(1)} m` : value;
              }
            }
          }
        },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "start",
            labels: {
              boxWidth: 18,
              boxHeight: 3,
              usePointStyle: true
            }
          },
          tooltip: {
            callbacks: {
              title: function (items) {
                const item = items && items[0];
                return item ? formatDateTime(item.parsed.x) : "";
              },
              label: function (item) {
                const value = item.parsed && item.parsed.y;
                const shown = value != null && !Number.isNaN(value) ? `${value.toFixed(3)} m` : "-";
                return `${item.dataset.label}: ${shown}`;
              }
            }
          },
          annotation: {
            annotations: hydroAnnotations(storm, points, firstIso, lastIso, yRange.min, yRange.max, markerIso)
          }
        }
      }
    });

    els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | ${points.length} hours`;
  }

  function destroyHydroChart() {
    if (!state.hydroChart) return;
    state.hydroChart.destroy();
    state.hydroChart = null;
  }

  function drawGtsmChart(storm) {
    if (typeof Chart === "undefined") {
      clearCanvas(els.gtsmCanvas, "Chart library unavailable");
      return;
    }
    renderGtsmChart(storm);
  }

  function renderGtsmChart(storm) {
    const canvas = els.gtsmCanvas;
    const observedSeries = seriesForSelectedBarrier(storm);
    const observedPoints = hydroPoints(observedSeries || { points: [] });
    const modelPoints = gtsmPointsForSelectedBarrier(storm);
    const gaugeLabel = gaugeLabelForSelectedBarrier();

    if (!modelPoints.length) {
      destroyGtsmChart();
      clearCanvas(canvas, "No cached GTSM gauge series for this storm");
      els.gtsmCaption.textContent = `${gaugeLabel} | no GTSM series`;
      return;
    }

    const observedSurge = observedPoints
      .filter(function (point) { return point.surge != null; })
      .map(function (point) { return { x: point.iso, y: point.surge }; });
    const modelSurge = modelPoints
      .filter(function (point) { return point.surge != null; })
      .map(function (point) { return { x: point.iso, y: point.surge }; });
    if (!modelSurge.length) {
      destroyGtsmChart();
      clearCanvas(canvas, "No finite GTSM gauge values for this storm");
      els.gtsmCaption.textContent = `${gaugeLabel} | no finite GTSM values`;
      return;
    }
    const yRange = valueRange(
      observedSurge.concat(modelSurge).map(function (point) { return point.y; }),
      0.15,
      0.2
    );
    const xRange = chartTimeRange(observedPoints, modelPoints);
    const markerIso = frameTimes(storm)[state.frameIndex] || firstFrameTime(storm) || null;

    destroyGtsmChart();
    state.gtsmChart = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            label: `${gaugeLabel} observed residual`,
            data: observedSurge,
            borderColor: COLORS.surge,
            backgroundColor: COLORS.surge,
            borderWidth: 2,
            fill: false,
            tension: 0.12,
            pointRadius: 0,
            pointHoverRadius: 4,
            spanGaps: false
          },
          {
            label: `${gaugeLabel} GTSM`,
            data: modelSurge,
            borderColor: COLORS.water,
            backgroundColor: COLORS.water,
            borderDash: [6, 4],
            borderWidth: 2.2,
            fill: false,
            tension: 0.12,
            pointRadius: 0,
            pointHoverRadius: 4,
            spanGaps: false
          }
        ]
      },
      options: comparisonChartOptions({
        xMin: xRange.min,
        xMax: xRange.max,
        yMin: yRange.min,
        yMax: yRange.max,
        yTitle: "Surge / residual (m)",
        annotations: hydroAnnotations(storm, observedPoints, xRange.min, xRange.max, yRange.min, yRange.max, markerIso)
      })
    });

    els.gtsmCaption.textContent = `${gaugeLabel} | ${modelSurge.length} GTSM hours`;
    if (!observedSurge.length) {
      els.gtsmCaption.textContent += ", no observed residual";
    }
  }

  function destroyGtsmChart() {
    if (!state.gtsmChart) return;
    state.gtsmChart.destroy();
    state.gtsmChart = null;
  }

  function hydroPoints(series) {
    return (series.points || []).map(function (point) {
      const time = Date.parse(point[0]);
      if (!Number.isFinite(time)) return null;
      return {
        iso: new Date(time).toISOString().replace(".000Z", "Z"),
        time: time,
        water: numberOrNull(point[1]),
        tide: numberOrNull(point[2]),
        surge: numberOrNull(point[3])
      };
    }).filter(Boolean).sort(function (a, b) {
      return a.time - b.time;
    });
  }

  function hydroLineDataset(label, points, key, color, width) {
    return {
      label: label,
      data: points.map(function (point) {
        return { x: point.iso, y: point[key] };
      }),
      borderColor: color,
      backgroundColor: color,
      borderWidth: width,
      fill: false,
      tension: 0.12,
      pointRadius: 0,
      pointHoverRadius: 4,
      spanGaps: false
    };
  }

  function highWaterPoints(storm, points) {
    const selected = normalizeBarrier(state.selectedHydroBarrier);
    const seen = new Set();
    const out = [];
    (storm.closures || []).forEach(function (closure) {
      if (normalizeBarrier(closure.barrier) !== selected) return;
      const time = Date.parse(closure.high_water_time);
      if (!Number.isFinite(time)) return;
      const iso = new Date(time).toISOString().replace(".000Z", "Z");
      if (seen.has(iso)) return;
      const nearest = nearestHydroPoint(points, time, "water");
      if (!nearest || Math.abs(nearest.time - time) > 3 * 60 * 60 * 1000) return;
      seen.add(iso);
      out.push({ x: iso, y: nearest.water });
    });
    return out;
  }

  function nearestHydroPoint(points, time, key) {
    let best = null;
    let bestDelta = Infinity;
    points.forEach(function (point) {
      if (point[key] == null) return;
      const delta = Math.abs(point.time - time);
      if (delta < bestDelta) {
        best = point;
        bestDelta = delta;
      }
    });
    return best;
  }

  function hydroValues(points) {
    const values = [];
    points.forEach(function (point) {
      ["water", "tide", "surge"].forEach(function (key) {
        if (point[key] != null) values.push(point[key]);
      });
    });
    return values;
  }

  function hydroYRange(points) {
    return valueRange(hydroValues(points), 0.15, 0.2);
  }

  function valueRange(values, padFraction, minPad) {
    const nums = values.filter(function (value) {
      return value != null && Number.isFinite(Number(value));
    });
    if (!nums.length) return { min: -0.5, max: 1 };
    let yMin = Math.min.apply(null, nums);
    let yMax = Math.max.apply(null, nums);
    const pad = Math.max(minPad, (yMax - yMin) * padFraction);
    yMin = Math.floor((yMin - pad) * 10) / 10;
    yMax = Math.ceil((yMax + pad) * 10) / 10;
    return { min: yMin, max: yMax };
  }

  function chartTimeRange() {
    const times = [];
    Array.prototype.forEach.call(arguments, function (points) {
      (points || []).forEach(function (point) {
        const time = point && Number.isFinite(point.time) ? point.time : Date.parse(point && point.iso);
        if (Number.isFinite(time)) times.push(time);
      });
    });
    if (!times.length) {
      const now = new Date().toISOString().replace(".000Z", "Z");
      return { min: now, max: now };
    }
    return {
      min: new Date(Math.min.apply(null, times)).toISOString().replace(".000Z", "Z"),
      max: new Date(Math.max.apply(null, times)).toISOString().replace(".000Z", "Z")
    };
  }

  function comparisonChartOptions(config) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      normalized: true,
      interaction: { intersect: false, mode: "nearest", axis: "x" },
      elements: {
        point: { hitRadius: 10 }
      },
      scales: {
        x: {
          type: "time",
          min: config.xMin,
          max: config.xMax,
          time: {
            unit: "hour",
            displayFormats: { hour: "HH:mm", day: "MMM d", month: "MMM yyyy" }
          },
          title: { display: true, text: "Time (UTC)" },
          grid: { color: COLORS.grid },
          ticks: {
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 8,
            callback: function (value) {
              return compactDate(value);
            }
          }
        },
        y: {
          min: config.yMin,
          max: config.yMax,
          title: { display: true, text: config.yTitle },
          grid: { color: COLORS.grid },
          ticks: {
            callback: function (value) {
              const num = Number(value);
              return Number.isFinite(num) ? `${num.toFixed(1)} m` : value;
            }
          }
        }
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          align: "start",
          labels: {
            boxWidth: 18,
            boxHeight: 3,
            usePointStyle: true
          }
        },
        tooltip: {
          callbacks: {
            title: function (items) {
              const item = items && items[0];
              return item ? formatDateTime(item.parsed.x) : "";
            },
            label: function (item) {
              const value = item.parsed && item.parsed.y;
              const shown = value != null && !Number.isNaN(value) ? `${value.toFixed(3)} m` : "-";
              return `${item.dataset.label}: ${shown}`;
            }
          }
        },
        annotation: {
          annotations: config.annotations || {}
        }
      }
    };
  }

  function hydroAnnotations(storm, points, firstIso, lastIso, yMin, yMax, markerIso) {
    const annotations = {};
    const first = Date.parse(firstIso);
    const last = Date.parse(lastIso);
    const selected = normalizeBarrier(state.selectedHydroBarrier);
    (storm.closures || []).forEach(function (closure, index) {
      if (normalizeBarrier(closure.barrier) !== selected) return;
      const start = Date.parse(closure.start);
      const end = Date.parse(closure.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      if (end < first || start > last) return;
      const xMin = new Date(Math.max(first, start)).toISOString().replace(".000Z", "Z");
      const xMax = new Date(Math.min(last, end)).toISOString().replace(".000Z", "Z");
      annotations[`closure-window-${index}`] = {
        type: "box",
        xMin: xMin,
        xMax: xMax,
        yMin: yMin,
        yMax: yMax,
        backgroundColor: COLORS.closure,
        borderColor: COLORS.closureEdge,
        borderWidth: 1,
        drawTime: "beforeDatasetsDraw"
      };
    });
    highWaterPoints(storm, points).forEach(function (point, index) {
      annotations[`high-water-line-${index}`] = {
        type: "line",
        xMin: point.x,
        xMax: point.x,
        borderColor: "rgba(122, 79, 12, 0.7)",
        borderWidth: 1.2,
        borderDash: [4, 4],
        drawTime: "afterDatasetsDraw"
      };
    });
    if (markerIso) {
      annotations["current-frame-line"] = currentFrameAnnotation(markerIso, true);
    }
    return annotations;
  }

  function currentFrameAnnotation(iso, withLabel) {
    return {
      type: "line",
      xMin: iso,
      xMax: iso,
      borderColor: "rgba(198, 77, 77, 0.95)",
      borderWidth: 1.8,
      label: {
        display: !!withLabel,
        content: "Frame",
        position: "start",
        backgroundColor: "rgba(255, 255, 255, 0.92)",
        color: COLORS.ink,
        borderColor: "rgba(198, 77, 77, 0.45)",
        borderWidth: 1,
        borderRadius: 4,
        padding: 5
      }
    };
  }

  function updateHydroFrameMarker(markerIso) {
    updateFrameMarkers(markerIso);
  }

  function updateFrameMarkers(markerIso) {
    updateChartFrameMarker(state.hydroChart, markerIso);
    updateChartFrameMarker(state.gtsmChart, markerIso);
  }

  function updateChartFrameMarker(chart, markerIso) {
    if (!chart || !markerIso) return;
    const plugins = chart.options && chart.options.plugins;
    if (!plugins || !plugins.annotation) return;
    if (!plugins.annotation.annotations) plugins.annotation.annotations = {};
    plugins.annotation.annotations["current-frame-line"] = currentFrameAnnotation(markerIso, true);
    chart.update("none");
  }

  function hydroYAxisTitle() {
    const selected = normalizeBarrier(state.selectedHydroBarrier);
    if (selected === "eastern_scheldt") return "Roompot Buiten (m NAP)";
    if (selected === "thames") return "Southend (m CD)";
    return "Water level (m)";
  }

  function gtsmPointsForSelectedBarrier(storm) {
    const gauges = storm && storm.gtsm_series && storm.gtsm_series.gauges;
    const gauge = gaugeKeyForSelectedBarrier();
    const source = gauges && gauges[gauge] && Array.isArray(gauges[gauge].points) ? gauges[gauge].points : [];
    return source.map(function (point) {
      const time = Date.parse(point[0]);
      if (!Number.isFinite(time)) return null;
      return {
        iso: new Date(time).toISOString().replace(".000Z", "Z"),
        time: time,
        surge: numberOrNull(point[1])
      };
    }).filter(Boolean).sort(function (a, b) {
      return a.time - b.time;
    });
  }

  function gaugeKeyForSelectedBarrier() {
    return gaugeKeyForBarrier(state.selectedHydroBarrier);
  }

  function gaugeKeyForBarrier(barrier) {
    return normalizeBarrier(barrier) === "thames" ? "southend" : "rpbu";
  }

  function gaugeLabelForSelectedBarrier() {
    return gaugeKeyForSelectedBarrier() === "southend" ? "Southend" : "Roompot Buiten";
  }

  function drawHydrographFallback(storm) {
    const canvas = els.hydroCanvas;
    const ctx = setupCanvas(canvas);
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const series = seriesForSelectedBarrier(storm);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#fbfcfd";
    ctx.fillRect(0, 0, width, height);

    if (!series || !series.points || !series.points.length) {
      drawCenteredText(ctx, width, height, "No water-level data in this window");
      els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | no observed data`;
      return;
    }

    const points = series.points.map(function (point) {
      return {
        time: Date.parse(point[0]),
        water: numberOrNull(point[1]),
        tide: numberOrNull(point[2]),
        surge: numberOrNull(point[3])
      };
    }).filter(function (point) {
      return Number.isFinite(point.time);
    });
    const plot = { left: 54, right: 18, top: 18, bottom: 44 };
    const xMin = Math.min.apply(null, points.map(function (p) { return p.time; }));
    const xMax = Math.max.apply(null, points.map(function (p) { return p.time; }));
    const values = [];
    points.forEach(function (point) {
      ["water", "tide", "surge"].forEach(function (key) {
        if (point[key] != null) values.push(point[key]);
      });
    });
    if (!values.length) {
      drawCenteredText(ctx, width, height, "No finite values in this window");
      els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | no finite observed values`;
      return;
    }
    let yMin = Math.min.apply(null, values);
    let yMax = Math.max.apply(null, values);
    const pad = Math.max(0.2, (yMax - yMin) * 0.15);
    yMin -= pad;
    yMax += pad;

    const x = function (time) {
      if (xMax === xMin) return plot.left;
      return plot.left + ((time - xMin) / (xMax - xMin)) * (width - plot.left - plot.right);
    };
    const y = function (value) {
      if (yMax === yMin) return height - plot.bottom;
      return height - plot.bottom - ((value - yMin) / (yMax - yMin)) * (height - plot.top - plot.bottom);
    };

    drawClosureWindows(ctx, storm, x, plot, height, xMin, xMax);
    drawAxes(ctx, plot, width, height, xMin, xMax, yMin, yMax, x, y);
    drawLine(ctx, points, "water", x, y, COLORS.water, 2.4);
    drawLine(ctx, points, "tide", x, y, COLORS.tide, 1.8);
    drawLine(ctx, points, "surge", x, y, COLORS.surge, 1.8);
    els.hydroCaption.textContent = `${barrierLabel(state.selectedHydroBarrier)} | ${points.length} hours`;
  }

  function drawClosureWindows(ctx, storm, x, plot, height, xMin, xMax) {
    const selected = state.selectedHydroBarrier;
    const closures = (storm.closures || []).filter(function (closure) {
      return normalizeBarrier(closure.barrier) === normalizeBarrier(selected);
    });
    ctx.save();
    closures.forEach(function (closure) {
      const start = Date.parse(closure.start);
      const end = Date.parse(closure.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      if (end < xMin || start > xMax) return;
      const left = Math.max(xMin, start);
      const right = Math.min(xMax, end);
      const px = x(left);
      const w = Math.max(2, x(right) - px);
      ctx.fillStyle = COLORS.closure;
      ctx.fillRect(px, plot.top, w, height - plot.top - plot.bottom);
      ctx.strokeStyle = COLORS.closureEdge;
      ctx.lineWidth = 1;
      ctx.strokeRect(px, plot.top, w, height - plot.top - plot.bottom);
    });
    ctx.restore();
  }

  function drawAxes(ctx, plot, width, height, xMin, xMax, yMin, yMax, x, y) {
    ctx.save();
    ctx.strokeStyle = COLORS.grid;
    ctx.fillStyle = COLORS.muted;
    ctx.lineWidth = 1;
    ctx.font = "12px system-ui, sans-serif";

    const yTicks = 5;
    for (let i = 0; i <= yTicks; i += 1) {
      const value = yMin + ((yMax - yMin) * i) / yTicks;
      const py = y(value);
      ctx.beginPath();
      ctx.moveTo(plot.left, py);
      ctx.lineTo(width - plot.right, py);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(value.toFixed(1), plot.left - 8, py);
    }

    const xTicks = 4;
    for (let i = 0; i <= xTicks; i += 1) {
      const time = xMin + ((xMax - xMin) * i) / xTicks;
      const px = x(time);
      ctx.beginPath();
      ctx.moveTo(px, plot.top);
      ctx.lineTo(px, height - plot.bottom);
      ctx.stroke();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(compactDate(new Date(time).toISOString()), px, height - plot.bottom + 10);
    }

    ctx.strokeStyle = COLORS.ink;
    ctx.beginPath();
    ctx.moveTo(plot.left, plot.top);
    ctx.lineTo(plot.left, height - plot.bottom);
    ctx.lineTo(width - plot.right, height - plot.bottom);
    ctx.stroke();
    ctx.restore();
  }

  function drawLine(ctx, points, key, x, y, color, width) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    let drawing = false;
    points.forEach(function (point) {
      const value = point[key];
      if (value == null) {
        if (drawing) ctx.stroke();
        drawing = false;
        return;
      }
      if (!drawing) {
        ctx.beginPath();
        ctx.moveTo(x(point.time), y(value));
        drawing = true;
      } else {
        ctx.lineTo(x(point.time), y(value));
      }
    });
    if (drawing) ctx.stroke();
    ctx.restore();
  }

  function setupCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const targetWidth = Math.max(320, Math.round(rect.width * ratio));
    const targetHeight = Math.max(200, Math.round(rect.height * ratio));
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      canvas.width = targetWidth;
      canvas.height = targetHeight;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return ctx;
  }

  function drawCenteredText(ctx, width, height, text) {
    ctx.save();
    ctx.fillStyle = COLORS.muted;
    ctx.font = "700 14px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, width / 2, height / 2);
    ctx.restore();
  }

  function clearCanvas(canvas, text) {
    const ctx = setupCanvas(canvas);
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#fbfcfd";
    ctx.fillRect(0, 0, width, height);
    drawCenteredText(ctx, width, height, text);
  }

  function selectedStorm() {
    return (DATA.storms || []).find(function (storm) {
      return storm.storm_id === state.selectedStormId;
    }) || null;
  }

  function seriesForSelectedBarrier(storm) {
    const key = normalizeBarrier(state.selectedHydroBarrier);
    return storm.series && storm.series[key] ? storm.series[key] : null;
  }

  function badge(kind, label) {
    const span = document.createElement("span");
    span.className = `badge ${kind || ""}`;
    span.textContent = label || "Unknown";
    return span;
  }

  function typePill(type) {
    const span = document.createElement("span");
    span.className = `type-pill ${type ? "" : "unset"}`;
    span.textContent = type || "Unset";
    return span;
  }

  function normalizeBarrier(value) {
    if (value === "es") return "eastern_scheldt";
    return value || "";
  }

  function barrierLabel(value) {
    return BARRIER_LABELS[value] || BARRIER_LABELS[normalizeBarrier(value)] || "Unknown";
  }

  function peakLevelContext(barrier) {
    const normalized = normalizeBarrier(barrier);
    const stats = DATA.gauge_stats && DATA.gauge_stats[normalized];
    if (!stats) return "Absolute max in chart";
    const context = [];
    if (stats.mean_high_water != null) context.push(`MHW est. ${formatMetres(stats.mean_high_water)}`);
    if (stats.max_water_level != null) context.push(`Gauge max ${formatMetres(stats.max_water_level)}`);
    return context.length ? context.join(" | ") : "Absolute max in chart";
  }

  function dateValue(storm) {
    const value = Date.parse(storm.display_start || storm.storm_window && storm.storm_window.start || storm.era5_window && storm.era5_window.start);
    return Number.isFinite(value) ? value : 0;
  }

  function pressureValue(storm) {
    const value = Number(storm.track_stats && storm.track_stats.min_pressure_hpa);
    return Number.isFinite(value) ? value : 9999;
  }

  function durationValue(storm) {
    const value = Number(storm.duration_hours);
    return Number.isFinite(value) ? value : 0;
  }

  function formatPressure(value) {
    const num = Number(value);
    return Number.isFinite(num) ? `${num.toFixed(1)} hPa` : "-";
  }

  function formatMetres(value) {
    const num = Number(value);
    return Number.isFinite(num) ? `${num.toFixed(2)} m` : "-";
  }

  function formatHours(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "-";
    if (num < 48) return `${Math.round(num)} h`;
    return `${(num / 24).toFixed(1)} d`;
  }

  function formatDateTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "-";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC",
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(date).replace(",", "");
  }

  function shortDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "-";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC",
      year: "numeric",
      month: "short",
      day: "2-digit"
    }).format(date).replace(",", "");
  }

  function compactDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "-";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      hour12: false
    }).format(date).replace(",", "");
  }

  function numberOrNull(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function debounce(fn, wait) {
    let timer = null;
    return function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(fn, wait);
    };
  }

  init();
})();
