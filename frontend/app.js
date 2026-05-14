(() => {
  const $ = (sel) => document.querySelector(sel);
  const statusEl = $("#status");
  const setStatus = (msg, isErr = false) => {
    statusEl.textContent = msg;
    statusEl.style.color = isErr ? "#ef5350" : "#26a69a";
    if (!isErr) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 3000);
  };

  // ---- Inline field-error tooltips
  const fieldErrors = new WeakMap();
  const showFieldError = (input, msg, ttl = 5000) => {
    if (!input) { setStatus(msg, true); return; }
    clearFieldError(input);
    const parent = input.parentElement;
    const tip = document.createElement("div");
    tip.className = "field-error";
    tip.textContent = msg;
    parent.appendChild(tip);
    input.classList.add("bad");
    fieldErrors.set(input, tip);
    input.addEventListener("input", () => clearFieldError(input), { once: true });
    if (ttl) setTimeout(() => clearFieldError(input), ttl);
  };
  const clearFieldError = (input) => {
    const tip = fieldErrors.get(input);
    if (tip) { tip.remove(); fieldErrors.delete(input); }
    input.classList.remove("bad");
  };

  // ---- Constants
  const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
  const TF_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 };
  const COLORS = ["#ffb74d", "#4fc3f7", "#aed581", "#f06292", "#ba68c8"];
  const INITIAL_VISIBLE_BARS = 80;

  // ---- Chart setup
  const chartEl = $("#chart");
  const rsiEl = $("#rsi-chart");
  const cvdEl = $("#cvd-chart");

  const chart = LightweightCharts.createChart(chartEl, {
    autoSize: true,
    layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
    grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a2e39" },
    rightPriceScale: { borderColor: "#2a2e39" },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    // disable plain-wheel zoom — we route Ctrl+Wheel through our own handler
    handleScale: { mouseWheel: false, pinch: false, axisPressedMouseMove: true },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  });
  const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });
  const candleMarkers = LightweightCharts.createSeriesMarkers(candleSeries, []);

  // volume — bottom overlay on its own price scale; auto-scales independently
  const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
    visible: false,
  });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  const rsiChart = LightweightCharts.createChart(rsiEl, {
    autoSize: true,
    layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
    grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a2e39", visible: false },
    rightPriceScale: { borderColor: "#2a2e39" },
    handleScale: { mouseWheel: false, pinch: false, axisPressedMouseMove: true },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  });
  const rsiSeries = rsiChart.addSeries(LightweightCharts.LineSeries, { color: "#bb86fc", lineWidth: 1 });

  const cvdChart = LightweightCharts.createChart(cvdEl, {
    autoSize: true,
    layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
    grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2a2e39", visible: false },
    rightPriceScale: { borderColor: "#2a2e39" },
    handleScale: { mouseWheel: false, pinch: false, axisPressedMouseMove: true },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  });
  const cvdSeries = cvdChart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    priceLineVisible: false,
  });

  let _syncing = false;
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (!r || _syncing) return;
    _syncing = true;
    try { rsiChart.timeScale().setVisibleLogicalRange(r); } catch (_) {}
    try { cvdChart.timeScale().setVisibleLogicalRange(r); } catch (_) {}
    _syncing = false;
  });
  const showRsi = (visible) => rsiEl.classList.toggle("visible", visible);
  const showCvd = (visible) => cvdEl.classList.toggle("visible", visible);
  showRsi(false);
  showCvd(false);

  // ---- Ctrl+Wheel zoom with locked price/bar ratio (TradingView-style)
  // Plain wheel is a no-op; Ctrl/Cmd+Wheel zooms time and price together,
  // anchored on the mouse, so candle shape stays constant across zoom levels.
  // Press R to reset back to autoScale on price.
  let lockedRatio = null;   // dollars per bar — captured on first zoom
  const priceScale = chart.priceScale("right");

  const resetZoomLock = () => {
    lockedRatio = null;
    priceScale.applyOptions({ autoScale: true });
  };

  const wheelZoom = (e) => {
    e.preventDefault();
    const ts = chart.timeScale();
    const tr = ts.getVisibleLogicalRange();
    if (!tr) return;
    const factor = e.deltaY < 0 ? 0.85 : 1.18;
    const rect = chartEl.getBoundingClientRect();
    const relX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const relY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    const tSpan = tr.to - tr.from;
    const tAnchor = tr.from + tSpan * relX;
    const newTSpan = Math.max(2, tSpan * factor);
    const newTimeRange = {
      from: tAnchor - newTSpan * relX,
      to: tAnchor + newTSpan * (1 - relX),
    };

    if (e.ctrlKey || e.metaKey) {
      // Ctrl/Cmd + wheel — ratio-locked zoom (price scales with time)
      if (lockedRatio === null) {
        const pr = priceScale.getVisibleRange();
        if (!pr) return;
        const bars = tr.to - tr.from;
        lockedRatio = bars > 0 ? (pr.to - pr.from) / bars : null;
        if (!lockedRatio) return;
        priceScale.applyOptions({ autoScale: false });
      }
      const pr = priceScale.getVisibleRange();
      if (!pr) return;
      ts.setVisibleLogicalRange(newTimeRange);
      const pSpan = pr.to - pr.from;
      const pAnchor = pr.to - relY * pSpan;     // chart y is inverted
      const newPSpan = newTSpan * lockedRatio;
      priceScale.setVisibleRange({
        from: pAnchor - newPSpan * (1 - relY),
        to: pAnchor + newPSpan * relY,
      });
    } else {
      // plain wheel — time-only zoom; release any prior ratio lock
      if (lockedRatio !== null) resetZoomLock();
      ts.setVisibleLogicalRange(newTimeRange);
    }
  };
  chartEl.addEventListener("wheel", wheelZoom, { passive: false });
  rsiEl.addEventListener("wheel", wheelZoom, { passive: false });

  const emaSeries = {};
  const tradePriceLines = new Map();
  const colorMap = {};
  let colorIdx = 0;
  const colorFor = (key) => (colorMap[key] ||= COLORS[colorIdx++ % COLORS.length]);

  // ---- Indicator math
  const computeEma = (candles, period) => {
    if (candles.length < period) return [];
    const k = 2 / (period + 1);
    const out = [];
    let sum = 0;
    for (let i = 0; i < period; i++) sum += candles[i].close;
    let ema = sum / period;
    out.push({ time: candles[period - 1].time, value: ema });
    for (let i = period; i < candles.length; i++) {
      ema = candles[i].close * k + ema * (1 - k);
      out.push({ time: candles[i].time, value: ema });
    }
    return out;
  };
  const computeRsi = (candles, period) => {
    if (candles.length <= period) return [];
    const out = [];
    let avgGain = 0, avgLoss = 0;
    for (let i = 1; i <= period; i++) {
      const d = candles[i].close - candles[i - 1].close;
      if (d >= 0) avgGain += d; else avgLoss -= d;
    }
    avgGain /= period; avgLoss /= period;
    let rs = avgLoss === 0 ? Infinity : avgGain / avgLoss;
    out.push({ time: candles[period].time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + rs) });
    for (let i = period + 1; i < candles.length; i++) {
      const d = candles[i].close - candles[i - 1].close;
      const gain = d > 0 ? d : 0;
      const loss = d < 0 ? -d : 0;
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      rs = avgLoss === 0 ? Infinity : avgGain / avgLoss;
      out.push({ time: candles[i].time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + rs) });
    }
    return out;
  };
  const selectedIndicators = () =>
    [...document.querySelectorAll(".ind:checked")].map((el) => ({
      kind: el.dataset.kind, period: parseInt(el.dataset.period, 10),
    }));
  const renderIndicators = () => {
    if (!session) return;
    const wanted = selectedIndicators();
    const wantEmaPeriods = new Set(wanted.filter(i => i.kind === "ema").map(i => i.period));
    const rsiSpec = wanted.find(i => i.kind === "rsi");
    for (const p of Object.keys(emaSeries).map(Number)) {
      if (!wantEmaPeriods.has(p)) { chart.removeSeries(emaSeries[p]); delete emaSeries[p]; }
    }
    for (const p of wantEmaPeriods) {
      if (!emaSeries[p]) {
        emaSeries[p] = chart.addSeries(LightweightCharts.LineSeries, {
          color: colorFor(`ema_${p}`), lineWidth: 1,
          priceLineVisible: false, lastValueVisible: false,
        });
      }
      emaSeries[p].setData(computeEma(session.candles, p));
    }
    if (rsiSpec) {
      rsiSeries.setData(computeRsi(session.candles, rsiSpec.period));
      showRsi(true);
    } else {
      rsiSeries.setData([]);
      showRsi(false);
    }
    const wantCvd = !!document.querySelector('.ind[data-kind="cvd"]:checked');
    if (wantCvd) { showCvd(true); fetchCvd(); }
    else { showCvd(false); cvdSeries.setData([]); cvdLastCursor = null; }

    const wantVolume = !!document.querySelector('.ind[data-kind="volume"]:checked');
    volumeSeries.applyOptions({ visible: wantVolume });
    if (wantVolume) {
      volumeSeries.setData(session.candles.map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? "rgba(38, 166, 154, 0.55)" : "rgba(239, 83, 80, 0.55)",
      })));
    } else {
      volumeSeries.setData([]);
    }
    const wantVolProfile = !!document.querySelector('.ind[data-kind="volprofile"]:checked');
    setVolumeProfileVisible(wantVolProfile);
  };

  // ---- Date helpers
  const parseDmy = (s) => {
    const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(s.trim());
    if (!m) return null;
    const [, dd, mm, yyyy] = m;
    const d = new Date(`${yyyy}-${mm}-${dd}T00:00:00Z`);
    if (isNaN(d.getTime())) return null;
    return d;
  };
  const fmtDmy = (date) => {
    const dd = String(date.getUTCDate()).padStart(2, "0");
    const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
    const yyyy = date.getUTCFullYear();
    return `${dd}/${mm}/${yyyy}`;
  };
  const toIso = (date) => date.toISOString().slice(0, 10);

  // ---- Form defaults
  const tfSelect = $("#tf-select");
  TIMEFRAMES.forEach((tf) => {
    const o = document.createElement("option");
    o.value = tf; o.textContent = tf;
    if (tf === "4h") o.selected = true;
    tfSelect.appendChild(o);
  });
  const setupForm = $("#setup-form");
  const today = new Date();
  const defaultReplay = new Date(today.getTime() - 60 * 86400 * 1000);
  flatpickr("#replay-date", {
    dateFormat: "d/m/Y",
    maxDate: "today",
    defaultDate: defaultReplay,
    allowInput: true,
  });

  // ---- State
  let session = null;
  let playTimer = null;
  let loadAbort = null;          // AbortController for in-flight session load
  let armedFor = null;          // "limit" | "sl" | "tp" | "hline" | "measure" | null

  // drawings
  const hlines = [];                   // [{ id, line, price, color }]
  let hlineCounter = 0;
  let measureFirst = null;             // { time, price }
  let measureSeries = null;            // line series for measure

  const api = async (path, opts = {}) => {
    const { signal, ...rest } = opts;
    const r = await fetch(path, {
      headers: { "content-type": "application/json" },
      signal,
      ...rest,
    });
    if (!r.ok) {
      let text = await r.text();
      try { text = JSON.parse(text).detail || text; } catch (_) {}
      const err = new Error(typeof text === "string" ? text : JSON.stringify(text));
      err.status = r.status;
      throw err;
    }
    return r.json();
  };

  // ---- Volume profile (horizontal histogram of volume-by-price for the
  // visible time window; POC highlighted in orange). Implemented as a
  // primitive so it re-renders automatically when you scroll/zoom.
  const VOLPROF_BUCKETS = 40;
  const VOLPROF_WIDTH_FRACTION = 0.18;
  const VOLPROF_FILL = "rgba(120, 144, 156, 0.38)";
  const VOLPROF_POC  = "rgba(255, 183, 77, 0.55)";

  const makeVolumeProfile = () => {
    let attached = null;
    const renderer = {
      draw(target) {
        if (!attached || !session || !session.candles.length) return;
        const ts = attached.chart.timeScale();
        const range = ts.getVisibleLogicalRange();
        if (!range) return;
        const cs = session.candles;
        const fromIdx = Math.max(0, Math.floor(range.from));
        const toIdx = Math.min(cs.length - 1, Math.ceil(range.to));
        if (toIdx < fromIdx) return;
        let pMin = Infinity, pMax = -Infinity;
        for (let i = fromIdx; i <= toIdx; i++) {
          if (cs[i].low < pMin) pMin = cs[i].low;
          if (cs[i].high > pMax) pMax = cs[i].high;
        }
        if (!isFinite(pMin) || !isFinite(pMax) || pMax <= pMin) return;
        const bucketSize = (pMax - pMin) / VOLPROF_BUCKETS;
        const buckets = new Array(VOLPROF_BUCKETS).fill(0);
        for (let i = fromIdx; i <= toIdx; i++) {
          const c = cs[i];
          const span = c.high - c.low;
          if (span <= 0) {
            const b = Math.min(VOLPROF_BUCKETS - 1,
              Math.max(0, Math.floor((c.close - pMin) / bucketSize)));
            buckets[b] += c.volume;
            continue;
          }
          // distribute the candle's volume evenly across the buckets it spans
          const bStart = Math.max(0, Math.floor((c.low - pMin) / bucketSize));
          const bEnd = Math.min(VOLPROF_BUCKETS - 1, Math.floor((c.high - pMin) / bucketSize));
          const per = c.volume / (bEnd - bStart + 1);
          for (let b = bStart; b <= bEnd; b++) buckets[b] += per;
        }
        let maxVol = 0, pocIdx = 0;
        for (let i = 0; i < buckets.length; i++) {
          if (buckets[i] > maxVol) { maxVol = buckets[i]; pocIdx = i; }
        }
        if (maxVol <= 0) return;

        target.useBitmapCoordinateSpace((scope) => {
          const ctx = scope.context;
          const series = attached.series;
          const hpr = scope.horizontalPixelRatio;
          const vpr = scope.verticalPixelRatio;
          const profileMax = scope.mediaSize.width * VOLPROF_WIDTH_FRACTION;
          for (let i = 0; i < VOLPROF_BUCKETS; i++) {
            if (buckets[i] <= 0) continue;
            const yTop = series.priceToCoordinate(pMin + (i + 1) * bucketSize);
            const yBot = series.priceToCoordinate(pMin + i * bucketSize);
            if (yTop == null || yBot == null) continue;
            const w = (buckets[i] / maxVol) * profileMax;
            const xLeft = scope.mediaSize.width - w;
            ctx.fillStyle = i === pocIdx ? VOLPROF_POC : VOLPROF_FILL;
            ctx.fillRect(
              xLeft * hpr,
              Math.min(yTop, yBot) * vpr,
              w * hpr,
              Math.max(1, Math.abs(yTop - yBot)) * vpr,
            );
          }
        });
      },
    };
    return {
      attached(params) { attached = params; },
      detached() { attached = null; },
      updateAllViews() {},
      paneViews() {
        return [{ zOrder: () => "normal", renderer: () => renderer }];
      },
    };
  };

  let volumeProfile = null;
  const setVolumeProfileVisible = (visible) => {
    if (visible && !volumeProfile) {
      volumeProfile = makeVolumeProfile();
      candleSeries.attachPrimitive(volumeProfile);
    } else if (!visible && volumeProfile) {
      candleSeries.detachPrimitive(volumeProfile);
      volumeProfile = null;
    }
  };

  // ---- Trade zone primitives (transparent green/red rectangles
  // entry→TP and entry→SL, persisting after the trade closes)
  const tradeZones = new Map();   // tradeId -> primitive
  const ZONE_GREEN = "rgba(38, 166, 154, 0.14)";
  const ZONE_RED   = "rgba(239,  83,  80, 0.14)";

  const makeTradeZone = (tradeId) => {
    let attached = null;
    const renderer = {
      draw(target) {
        if (!attached || !session) return;
        const t = session.trades.find((x) => x.id === tradeId);
        if (!t || t.entry_time == null || t.entry_price == null) return;
        target.useBitmapCoordinateSpace((scope) => {
          const ctx = scope.context;
          const ts = attached.chart.timeScale();
          const series = attached.series;
          const x1 = ts.timeToCoordinate(t.entry_time);
          const rightTime = t.exit_time != null ? t.exit_time : session.current_time;
          const x2 = ts.timeToCoordinate(rightTime);
          if (x1 == null || x2 == null) return;
          const yEntry = series.priceToCoordinate(t.entry_price);
          if (yEntry == null) return;
          const hpr = scope.horizontalPixelRatio;
          const vpr = scope.verticalPixelRatio;
          const left = Math.min(x1, x2) * hpr;
          const width = Math.max(1, Math.abs(x2 - x1)) * hpr;
          const drawBox = (yOther, color) => {
            if (yOther == null) return;
            ctx.fillStyle = color;
            ctx.fillRect(
              left,
              Math.min(yEntry, yOther) * vpr,
              width,
              Math.abs(yOther - yEntry) * vpr,
            );
          };
          if (t.tp != null) drawBox(series.priceToCoordinate(t.tp), ZONE_GREEN);
          if (t.sl != null) drawBox(series.priceToCoordinate(t.sl), ZONE_RED);
        });
      },
    };
    return {
      attached(params) { attached = params; },
      detached() { attached = null; },
      updateAllViews() {},
      paneViews() {
        return [{ zOrder: () => "bottom", renderer: () => renderer }];
      },
    };
  };

  const syncTradeZones = () => {
    const wantIds = new Set();
    if (session) {
      for (const t of session.trades) {
        if (t.entry_time != null) wantIds.add(t.id);
      }
    }
    // remove zones whose trade is gone or hasn't filled yet
    for (const [id, p] of tradeZones) {
      if (!wantIds.has(id)) {
        candleSeries.detachPrimitive(p);
        tradeZones.delete(id);
      }
    }
    // add zones for newly-filled trades
    for (const id of wantIds) {
      if (!tradeZones.has(id)) {
        const p = makeTradeZone(id);
        candleSeries.attachPrimitive(p);
        tradeZones.set(id, p);
      }
    }
  };

  // ---- CVD (cumulative volume delta) sub-pane
  let cvdAbort = null;
  let cvdLastCursor = null;
  const fetchCvd = async () => {
    if (!session) return;
    if (cvdLastCursor === session.cursor) return;
    cvdLastCursor = session.cursor;
    if (cvdAbort) cvdAbort.abort();
    cvdAbort = new AbortController();
    try {
      const data = await api(`/api/session/${session.id}/cvd`, { signal: cvdAbort.signal });
      cvdSeries.setData((data.points || []).map((p) => ({
        time: p.time, open: p.open, high: p.high, low: p.low, close: p.close,
      })));
    } catch (err) {
      if (err.name !== "AbortError") setStatus(`cvd: ${err.message}`, true);
    }
  };

  // ---- Time & Sales
  // Two modes:
  //   - candle mode: each session update re-pulls last N prints from the cursor candle
  //   - tick mode: tape accumulates trades streamed in via tick_step
  let tapeAbort = null;
  let tapeLastCursorTime = null;
  let tapeBuffer = [];        // accumulated prints (newest at end internally)
  const TAPE_CAP = 400;

  const tapeRowHtml = (t, isLarge) => {
    const time = new Date(t.time_ms).toISOString().slice(11, 19);
    return `<span class="t">${time}</span>` +
      `<span>${t.price.toFixed(getPrecision(t.price))}</span>` +
      `<span class="q">${t.qty.toFixed(t.qty >= 100 ? 0 : 2)}</span>`;
  };

  const renderTape = () => {
    const list = $("#tape-list");
    list.innerHTML = "";
    if (!tapeBuffer.length) {
      $("#tape-status").textContent = session ? "no trades yet" : "";
      return;
    }
    const qtys = tapeBuffer.map((t) => t.qty).sort((a, b) => a - b);
    const p95 = qtys[Math.floor(qtys.length * 0.95)] || Infinity;
    // newest at top
    for (let i = tapeBuffer.length - 1; i >= 0; i--) {
      const t = tapeBuffer[i];
      const row = document.createElement("div");
      row.className = `tape-row ${t.side}${t.qty >= p95 ? " large" : ""}`;
      row.innerHTML = tapeRowHtml(t);
      list.appendChild(row);
    }
    const speed = $("#speed").value;
    if (speed.startsWith("tick:")) {
      $("#tape-status").textContent = `${tapeBuffer.length} prints · tick replay · large = top 5%`;
    } else {
      const tfSec = TF_SECONDS[session.tf];
      const open = new Date(session.current_time * 1000).toISOString().slice(11, 16);
      const close = new Date((session.current_time + tfSec) * 1000).toISOString().slice(11, 16);
      $("#tape-status").textContent =
        `${tapeBuffer.length} prints in ${open}→${close} UTC  ·  large = top 5%`;
    }
  };

  const appendTicks = (ticks) => {
    if (!ticks || !ticks.length || !session) return;
    tapeBuffer.push(...ticks);
    if (tapeBuffer.length > TAPE_CAP) {
      tapeBuffer = tapeBuffer.slice(-TAPE_CAP);
    }
    // grow the chart's partial candle from these newly-revealed prints; on a
    // candle boundary, freeze the previous candle to its real kline OHLC.
    const tfSec = TF_SECONDS[session.tf];
    const tfMs = tfSec * 1000;
    for (const t of ticks) {
      const co = Math.floor(t.time_ms / tfMs) * tfSec;
      if (revealedForming.candleOpenSec !== co) {
        if (revealedForming.candleOpenSec != null) {
          const real = session.candles.find((c) => c.time === revealedForming.candleOpenSec);
          if (real) candleSeries.update(real);
        }
        revealedForming = { candleOpenSec: co, ticks: [] };
      }
      revealedForming.ticks.push(t);
    }
    if (revealedForming.ticks.length) {
      const prices = revealedForming.ticks.map((t) => t.price);
      candleSeries.update({
        time: revealedForming.candleOpenSec,
        open: prices[0],
        high: Math.max(...prices),
        low: Math.min(...prices),
        close: prices[prices.length - 1],
      });
    }
    renderTape();
  };

  const fetchTape = async () => {
    if (!session) return;
    if ($("#speed").value.startsWith("tick:")) return;  // tick mode owns its tape
    if (tapeLastCursorTime === session.current_time) return;
    tapeLastCursorTime = session.current_time;
    if (tapeAbort) tapeAbort.abort();
    tapeAbort = new AbortController();
    $("#tape-status").textContent = "loading…";
    try {
      const data = await api(`/api/session/${session.id}/recent_trades?n=80`,
        { signal: tapeAbort.signal });
      tapeBuffer = data.trades || [];
      renderTape();
    } catch (err) {
      if (err.name === "AbortError") return;
      $("#tape-status").textContent = `err: ${err.message}`;
    }
  };

  // ---- Trade rendering
  const renderTrades = () => {
    if (!session) return;
    const markers = [];
    for (const t of session.trades) {
      if (t.entry_time != null) {
        markers.push({
          time: t.entry_time,
          position: t.side === "long" ? "belowBar" : "aboveBar",
          color: t.side === "long" ? "#26a69a" : "#ef5350",
          shape: t.side === "long" ? "arrowUp" : "arrowDown",
          text: `${t.side.toUpperCase()} ${t.qty}`,
        });
      }
      if (t.status === "closed" && t.exit_time != null) {
        markers.push({
          time: t.exit_time,
          position: "inBar",
          color: (t.pnl ?? 0) >= 0 ? "#26a69a" : "#ef5350",
          shape: "circle",
          text: `EXIT (${t.exit_reason || ""})`,
        });
      }
    }
    markers.sort((a, b) => a.time - b.time);
    candleMarkers.setMarkers(markers);
    syncTradeZones();

    for (const lines of tradePriceLines.values()) {
      for (const line of lines) candleSeries.removePriceLine(line);
    }
    tradePriceLines.clear();
    for (const t of session.trades) {
      if (t.status === "closed") continue;
      const lines = [];
      if (t.status === "pending" && t.limit_price != null) {
        lines.push(candleSeries.createPriceLine({
          price: t.limit_price,
          color: t.side === "long" ? "#80cbc4" : "#ef9a9a",
          lineStyle: 1, lineWidth: 1, axisLabelVisible: true,
          title: `LIMIT ${t.side[0].toUpperCase()} ${t.qty}`,
        }));
      } else if (t.entry_price != null) {
        lines.push(candleSeries.createPriceLine({
          price: t.entry_price,
          color: t.side === "long" ? "#26a69a" : "#ef5350",
          lineStyle: 0, lineWidth: 1, axisLabelVisible: true,
          title: `${t.side[0].toUpperCase()} ${t.qty}`,
        }));
      }
      if (t.sl != null) lines.push(candleSeries.createPriceLine({
        price: t.sl, color: "#ef5350", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "SL",
      }));
      if (t.tp != null) lines.push(candleSeries.createPriceLine({
        price: t.tp, color: "#26a69a", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "TP",
      }));
      tradePriceLines.set(t.id, lines);
    }

    const tbody = $("#trades-table tbody");
    tbody.innerHTML = "";
    let openPnl = 0, closedPnl = 0, wins = 0, losses = 0, closed = 0;
    for (const t of [...session.trades].reverse()) {
      const tr = document.createElement("tr");
      if (t.status === "closed") tr.classList.add("closed");
      const pnl = t.pnl ?? 0;
      if (t.status === "open") openPnl += pnl;
      if (t.status === "closed") {
        closedPnl += pnl; closed += 1;
        if (pnl >= 0) wins += 1; else losses += 1;
      }
      const pnlClass = pnl >= 0 ? "pnl-pos" : "pnl-neg";
      const sideLabel = t.status === "pending" ? `${t.side} (limit)` : t.side;
      const entry = t.entry_price != null ? t.entry_price.toFixed(4)
        : (t.limit_price != null ? `@${t.limit_price.toFixed(4)}` : "—");
      const exitCol = t.status === "closed" ? t.exit_price.toFixed(4)
        : (t.status === "pending" ? "pending" : "—");
      const actionCol = (t.status === "open" || t.status === "pending")
        ? `<button data-close="${t.id}">${t.status === "pending" ? "cancel" : "close"}</button>`
        : (t.exit_reason || "");
      tr.innerHTML = `
        <td>${sideLabel}</td>
        <td>${t.qty}</td>
        <td>${entry}</td>
        <td>${exitCol}</td>
        <td class="${pnlClass}">${t.status === "pending" ? "—" : pnl.toFixed(4)}</td>
        <td>${actionCol}</td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll("button[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => closeTrade(btn.dataset.close));
    });
    const winRate = closed > 0 ? ((wins / closed) * 100).toFixed(1) : "—";
    $("#totals").innerHTML =
      `<span>Open P&amp;L: <b class="${openPnl >= 0 ? "pnl-pos" : "pnl-neg"}">${openPnl.toFixed(4)}</b></span>` +
      `<span>Closed P&amp;L: <b class="${closedPnl >= 0 ? "pnl-pos" : "pnl-neg"}">${closedPnl.toFixed(4)}</b></span>` +
      `<span>${wins}W / ${losses}L (${winRate}%)</span>`;
  };

  const applySession = (data, isNew = false) => {
    session = data;
    if (!isNew && session.in_tick) renderTickChart();
    else candleSeries.setData(session.candles);
    renderIndicators();
    renderTrades();
    fetchTape();
    const atEnd = session.cursor >= session.total - 1;
    $("#cursor-info").textContent = `${session.symbol} ${session.tf}  ${session.cursor + 1} / ${session.total}  @ ${new Date(session.current_time * 1000).toISOString().slice(0, 16).replace("T", " ")}  price ${session.current_price.toFixed(4)}`;
    $("#mark-info").textContent = `Mark: ${session.current_price.toFixed(4)}`;
    $("#back-1").disabled = session.cursor <= 0;
    $("#next-1").disabled = atEnd;
    $("#play").disabled = atEnd;
    $("#long-btn").disabled = atEnd;
    $("#short-btn").disabled = atEnd;
    if (atEnd && playTimer) stopPlay();

    if (isNew) {
      // re-enable auto-scale on the price axis so it follows the new data's price range
      resetZoomLock();
      rsiChart.priceScale("right").applyOptions({ autoScale: true });
      // defer one frame so the chart has rendered the new data before we set the range
      requestAnimationFrame(() => {
        const n = session.candles.length;   // warmup candles + cursor candle
        // show all warmup candles plus a few empty slots on the right for what's coming
        chart.timeScale().setVisibleLogicalRange({ from: 0, to: n + 5 });
      });
    }
  };

  const resetForNewSession = () => {
    stopPlay();
    disarm();
    clearAllDrawings();
    candleSeries.setData([]);
    candleMarkers.setMarkers([]);
    for (const p of tradeZones.values()) candleSeries.detachPrimitive(p);
    tradeZones.clear();
    setVolumeProfileVisible(false);
    cvdSeries.setData([]); showCvd(false); cvdLastCursor = null;
    tapeLastCursorTime = null;
    tapeBuffer = [];
    resetTickReplay();
    $("#tape-list").innerHTML = "";
    $("#tape-status").textContent = "";
    for (const lines of tradePriceLines.values()) {
      for (const line of lines) candleSeries.removePriceLine(line);
    }
    tradePriceLines.clear();
    for (const p of Object.keys(emaSeries).map(Number)) {
      chart.removeSeries(emaSeries[p]);
      delete emaSeries[p];
    }
    rsiSeries.setData([]);
    showRsi(false);
    // reset trade form back to market order
    $("#t-type").value = "market";
    $("#t-type").dispatchEvent(new Event("change"));
    $("#t-limit").value = "";
    session = null;
  };

  // ---- Actions
  const loadSession = async () => {
    const fd = new FormData(setupForm);
    const replayDate = parseDmy(fd.get("replay_date"));
    if (!replayDate) { showFieldError($("#replay-date"), "use dd/mm/yyyy"); return; }
    if (replayDate.getTime() >= Date.now()) {
      showFieldError($("#replay-date"), "must be in the past"); return;
    }
    const tf = fd.get("tf");
    const warmup = parseInt(fd.get("warmup"), 10) || 100;
    const tfSec = TF_SECONDS[tf];
    if (!tfSec) { setStatus("unsupported tf", true); return; }

    // fetch a little extra before the replay date so the chart has warmup candles
    // visible behind the cursor (replay_ts tells the backend where to put the cursor)
    const startMs = replayDate.getTime() - (warmup + 5) * tfSec * 1000;
    const start = toIso(new Date(startMs));
    const end = toIso(new Date(Math.min(Date.now(), replayDate.getTime() + 365 * 86400 * 1000)));
    const replayTs = Math.floor(replayDate.getTime() / 1000);

    const body = {
      symbol: fd.get("symbol").trim().toUpperCase(),
      market: fd.get("market"),
      tf, start, end, warmup,
      replay_ts: replayTs,
    };

    // cancel any in-flight load so a quick re-submit isn't dropped
    if (loadAbort) loadAbort.abort();
    loadAbort = new AbortController();
    const signal = loadAbort.signal;
    setStatus("loading…");
    try {
      resetForNewSession();
      const data = await api("/api/session", {
        method: "POST", body: JSON.stringify(body), signal,
      });
      applySession(data, true);
      setStatus(`loaded ${data.total} candles`);
    } catch (err) {
      if (err.name === "AbortError") return;  // superseded by a newer load
      const msg = err.message;
      if (/symbol|invalid symbol/i.test(msg)) showFieldError(setupForm.symbol, "unknown symbol");
      else if (/no candles/i.test(msg)) showFieldError($("#replay-date"), "no data for this range");
      else setStatus(msg, true);
    }
  };

  const stepN = async (n) => {
    if (!session) return;
    try {
      const data = await api(`/api/session/${session.id}/step`, {
        method: "POST", body: JSON.stringify({ n }),
      });
      applySession(data);
    } catch (err) { setStatus(err.message, true); stopPlay(); }
  };

  // ---- Tick-by-tick replay engine
  // Pre-fetches aggTrades into a queue, then a requestAnimationFrame loop
  // advances a simulated clock at (real Δt × tickSpeed) and reveals only ticks
  // whose timestamps are due. So 1x ≈ real time; 2x is twice as fast; etc.
  let tickQueue = [];          // pending {time_ms, price, qty, side} from backend
  let simClockMs = null;       // simulated wall clock (ms since epoch)
  let tickSpeed = 1;
  let tickRAF = null;
  let tickLastFrame = null;
  let tickFetching = false;
  let tickAtEnd = false;
  // chart's view of the forming candle is built from REVEALED ticks only
  // (not the pre-fetched batch), so the candle moves in lockstep with the tape
  let revealedForming = { candleOpenSec: null, ticks: [] };
  const resetRevealedForming = () => { revealedForming = { candleOpenSec: null, ticks: [] }; };

  const TICK_PREFETCH = 80;
  const TICK_REFILL_AT = 40;

  const tickPrefetch = async () => {
    if (tickFetching || tickAtEnd || !session) return;
    tickFetching = true;
    try {
      const data = await api(`/api/session/${session.id}/tick_step`, {
        method: "POST", body: JSON.stringify({ n: TICK_PREFETCH }),
      });
      if (data.new_ticks && data.new_ticks.length) {
        tickQueue.push(...data.new_ticks);
      }
      // applySession will update the chart's partial candle to the latest
      // state — slightly ahead of the tape reveal, but only by ~80 ticks
      applySession(data, false);
      if (data.at_end) tickAtEnd = true;
    } catch (err) {
      setStatus(err.message, true);
      stopPlay();
    } finally {
      tickFetching = false;
    }
  };

  const tickFrame = (now) => {
    if (tickLastFrame === null) tickLastFrame = now;
    const dtReal = now - tickLastFrame;
    tickLastFrame = now;

    if (simClockMs === null && tickQueue.length > 0) {
      // align the clock to just before the first pending tick so it reveals
      // promptly without an artificial wait
      simClockMs = tickQueue[0].time_ms - 1;
    }
    if (simClockMs !== null) simClockMs += dtReal * tickSpeed;

    // reveal eligible ticks
    let revealed = null;
    while (tickQueue.length > 0 && tickQueue[0].time_ms <= simClockMs) {
      (revealed ||= []).push(tickQueue.shift());
    }
    if (revealed) appendTicks(revealed);

    if (tickQueue.length < TICK_REFILL_AT && !tickFetching && !tickAtEnd) {
      tickPrefetch();
    }

    if (tickAtEnd && tickQueue.length === 0 && !tickFetching) {
      stopPlay();
      return;
    }
    tickRAF = requestAnimationFrame(tickFrame);
  };

  const tickStartPlay = (speed) => {
    tickSpeed = speed;
    tickLastFrame = null;
    tickAtEnd = false;
    if (tickQueue.length < TICK_PREFETCH) tickPrefetch();
    tickRAF = requestAnimationFrame(tickFrame);
  };
  const tickStopPlay = () => {
    if (tickRAF) cancelAnimationFrame(tickRAF);
    tickRAF = null;
    tickLastFrame = null;
  };
  const resetTickReplay = () => {
    tickQueue = [];
    simClockMs = null;
    tickAtEnd = false;
    resetRevealedForming();
  };

  // build chart data from completed klines up to (but not including) the candle
  // we're currently revealing tick-by-tick, then append our own partial OHLC
  // built from revealed ticks. Called on prefetch so the chart "stays behind"
  // the backend's lookahead.
  const renderTickChart = () => {
    if (!session) return;
    const formingOpen = revealedForming.candleOpenSec;
    let chartData;
    if (formingOpen != null) {
      chartData = session.candles.filter((c) => c.time < formingOpen);
      if (revealedForming.ticks.length) {
        const prices = revealedForming.ticks.map((t) => t.price);
        chartData.push({
          time: formingOpen,
          open: prices[0],
          high: Math.max(...prices),
          low: Math.min(...prices),
          close: prices[prices.length - 1],
        });
      }
    } else {
      chartData = session.in_tick ? session.candles.slice(0, -1) : session.candles;
    }
    candleSeries.setData(chartData);
  };

  // manual one-step in tick mode (Next button) — reveal n ticks immediately,
  // bypassing the simulated clock
  const tickNext = async (n) => {
    if (!session) return;
    try {
      const data = await api(`/api/session/${session.id}/tick_step`, {
        method: "POST", body: JSON.stringify({ n }),
      });
      applySession(data, false);
      if (data.new_ticks) appendTicks(data.new_ticks);
      if (data.at_end) tickAtEnd = true;
    } catch (err) { setStatus(err.message, true); }
  };
  const backN = async (n) => {
    if (!session) return;
    try {
      const data = await api(`/api/session/${session.id}/back`, {
        method: "POST", body: JSON.stringify({ n }),
      });
      // backend resets tick state on rewind — drop our local queue so we don't
      // try to reveal ticks from candles that no longer exist on the cursor.
      resetTickReplay();
      applySession(data);
    } catch (err) { setStatus(err.message, true); }
  };

  const placeTrade = async (side) => {
    if (!session) return;
    // pause autoplay so the cursor can't advance between the user's click
    // and the request reaching the backend (would record a one-bar-late entry)
    if (playTimer) stopPlay();
    const qtyInput = $("#t-qty");
    const qty = parseFloat(qtyInput.value);
    if (!(qty > 0)) { showFieldError(qtyInput, "qty must be > 0"); return; }

    const orderType = $("#t-type").value;
    let limitPrice = null;
    let refPrice = session.current_price;
    if (orderType === "limit") {
      const limitInput = $("#t-limit");
      limitPrice = parseFloat(limitInput.value);
      if (!(limitPrice > 0)) { showFieldError(limitInput, "limit price required"); return; }
      if (side === "long" && limitPrice >= session.current_price) {
        showFieldError(limitInput, `long limit must be < market (${session.current_price.toFixed(4)})`); return;
      }
      if (side === "short" && limitPrice <= session.current_price) {
        showFieldError(limitInput, `short limit must be > market (${session.current_price.toFixed(4)})`); return;
      }
      refPrice = limitPrice;
    }

    const useSl = $("#use-sl").checked;
    const useTp = $("#use-tp").checked;
    const slInput = $("#t-sl-pct");
    const tpInput = $("#t-tp-pct");
    const slPct = useSl && slInput.value ? parseFloat(slInput.value) : null;
    const tpPct = useTp && tpInput.value ? parseFloat(tpInput.value) : null;
    if (useSl && !(slPct > 0)) { showFieldError(slInput, "must be > 0"); return; }
    if (useTp && !(tpPct > 0)) { showFieldError(tpInput, "must be > 0"); return; }

    let sl = null, tp = null;
    if (slPct != null) sl = side === "long" ? refPrice * (1 - slPct / 100) : refPrice * (1 + slPct / 100);
    if (tpPct != null) tp = side === "long" ? refPrice * (1 + tpPct / 100) : refPrice * (1 - tpPct / 100);

    try {
      const data = await api(`/api/session/${session.id}/trade`, {
        method: "POST",
        body: JSON.stringify({ side, qty, order_type: orderType, limit_price: limitPrice, sl, tp }),
      });
      applySession(data);
      setStatus(orderType === "limit"
        ? `${side} LIMIT @ ${limitPrice.toFixed(4)} pending`
        : `opened ${side} @ ${data.current_price.toFixed(4)}`);
    } catch (err) {
      const m = err.message;
      if (/limit/i.test(m) && /must be/i.test(m))      showFieldError($("#t-limit"), m);
      else if (/SL/i.test(m))                          showFieldError(slInput, m);
      else if (/TP/i.test(m))                          showFieldError(tpInput, m);
      else setStatus(m, true);
    }
  };

  const closeTrade = async (tid) => {
    if (!session) return;
    if (playTimer) stopPlay();    // same race-avoidance as placeTrade
    try {
      const data = await api(`/api/session/${session.id}/trade/${tid}/close`, { method: "POST" });
      applySession(data);
    } catch (err) { setStatus(err.message, true); }
  };

  const isTickSpeed = () => $("#speed").value.startsWith("tick:");
  const tickSpeedFor = () => parseFloat($("#speed").value.slice(5));
  const isPlaying = () => !!(playTimer || tickRAF);

  const startPlay = () => {
    if (playTimer || tickRAF || !session) return;
    if (session.cursor >= session.total - 1 && !session.in_tick) return;
    $("#play").textContent = "Pause";
    if (isTickSpeed()) {
      tickStartPlay(tickSpeedFor());
    } else {
      const speed = parseInt($("#speed").value, 10);
      playTimer = setInterval(() => stepN(parseInt($("#step-n").value, 10) || 1), speed);
    }
  };
  const stopPlay = () => {
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
    tickStopPlay();
    $("#play").textContent = "Play";
  };

  // ---- Arming / chart click handling
  const ARM_LABEL = {
    limit: "set LIMIT price", sl: "set STOP-LOSS", tp: "set TAKE-PROFIT",
    hline: "place horizontal line", measure: "click start of measurement",
  };
  const armFor = (kind) => {
    armedFor = kind;
    chartEl.classList.add("armed");
    document.querySelectorAll(".pick, .tool").forEach((b) => {
      const k = b.dataset.pick || b.dataset.tool;
      b.classList.toggle("armed", k === kind);
    });
    const hint = $("#arm-hint");
    hint.textContent = `${ARM_LABEL[kind] || kind}  ·  esc to cancel`;
    hint.style.display = "";
  };
  const disarm = () => {
    armedFor = null;
    chartEl.classList.remove("armed");
    document.querySelectorAll(".pick, .tool").forEach((b) => b.classList.remove("armed"));
    $("#arm-hint").style.display = "none";
    // if measure had only first point, clear it
    if (measureFirst && (!measureSeries || measureSeries === null)) measureFirst = null;
  };

  const refPriceForPicks = () => {
    if (!session) return null;
    const orderType = $("#t-type").value;
    if (orderType === "limit") {
      const lp = parseFloat($("#t-limit").value);
      if (lp > 0) return lp;
    }
    return session.current_price;
  };
  const getPrecision = (n) => {
    if (n >= 1000) return 2;
    if (n >= 10) return 3;
    if (n >= 1) return 4;
    return 6;
  };

  const addHline = (price) => {
    const color = $("#hline-color").value || "#90caf9";
    const id = `hl_${++hlineCounter}`;
    const line = candleSeries.createPriceLine({
      price, color, lineStyle: 0, lineWidth: 1,
      axisLabelVisible: true, title: `H ${price.toFixed(getPrecision(price))}`,
    });
    hlines.push({ id, line, price, color });
    renderHlines();
  };
  const removeHline = (id) => {
    const idx = hlines.findIndex((h) => h.id === id);
    if (idx < 0) return;
    candleSeries.removePriceLine(hlines[idx].line);
    hlines.splice(idx, 1);
    renderHlines();
  };
  const updateHlineColor = (id, color) => {
    const h = hlines.find((x) => x.id === id);
    if (!h) return;
    h.color = color;
    h.line.applyOptions({ color });
  };
  const renderHlines = () => {
    const strip = $("#hlines-strip");
    strip.innerHTML = "";
    for (const h of hlines) {
      const chip = document.createElement("span");
      chip.className = "hline-chip";
      const colorInput = document.createElement("input");
      colorInput.type = "color";
      colorInput.value = h.color;
      colorInput.title = "line color";
      colorInput.addEventListener("input", (e) => updateHlineColor(h.id, e.target.value));
      const label = document.createElement("span");
      label.className = "px";
      label.textContent = h.price.toFixed(getPrecision(h.price));
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "×";
      del.title = "remove";
      del.addEventListener("click", () => removeHline(h.id));
      chip.append(colorInput, label, del);
      strip.appendChild(chip);
    }
  };
  const clearHlines = () => {
    for (const h of hlines) candleSeries.removePriceLine(h.line);
    hlines.length = 0;
    renderHlines();
  };
  const clearMeasure = () => {
    if (measureSeries) { chart.removeSeries(measureSeries); measureSeries = null; }
    measureFirst = null;
    $("#measure-summary").textContent = "";
  };
  const clearAllDrawings = () => { clearHlines(); clearMeasure(); };

  const finishMeasure = (second) => {
    const a = measureFirst;
    const b = second;
    if (!a || !b) return;
    const [s, e] = a.time <= b.time ? [a, b] : [b, a];
    measureSeries = chart.addSeries(LightweightCharts.LineSeries, {
      color: "#ffb74d", lineWidth: 2, lineStyle: 1,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    measureSeries.setData([{ time: s.time, value: s.price }, { time: e.time, value: e.price }]);
    const dp = e.price - s.price;
    const pct = (dp / s.price) * 100;
    const dtSec = e.time - s.time;
    const tf = session ? TF_SECONDS[session.tf] : null;
    // bar count between endpoints (excludes the starting endpoint)
    const bars = tf ? Math.round(dtSec / tf) : null;
    const human = dtSec >= 86400
      ? `${(dtSec / 86400).toFixed(1)}d`
      : dtSec >= 3600 ? `${(dtSec / 3600).toFixed(1)}h` : `${Math.round(dtSec / 60)}m`;
    $("#measure-summary").innerHTML =
      `Δ <b>${dp >= 0 ? "+" : ""}${dp.toFixed(getPrecision(Math.abs(dp) || 1))}</b>` +
      `  (<b>${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</b>)` +
      `  ·  ${human}` +
      (bars != null ? `  ·  <b>${bars}</b> bars` : "");
    measureFirst = null;
  };

  chart.subscribeClick((param) => {
    if (!armedFor || !param || !param.point) return;
    const price = candleSeries.coordinateToPrice(param.point.y);
    if (price == null || !isFinite(price)) return;
    const time = param.time;

    if (armedFor === "limit") {
      $("#t-type").value = "limit";
      $("#t-type").dispatchEvent(new Event("change"));
      $("#t-limit").value = price.toFixed(getPrecision(price));
      setStatus(`limit set to ${$("#t-limit").value}`);
      disarm();
    } else if (armedFor === "sl" || armedFor === "tp") {
      const side = $("#pick-side").value;
      const ref = refPriceForPicks();
      if (ref == null) { disarm(); return; }
      const targetInput = armedFor === "sl" ? $("#t-sl-pct") : $("#t-tp-pct");
      const useBox = armedFor === "sl" ? $("#use-sl") : $("#use-tp");
      if (armedFor === "sl") {
        const ok = side === "long" ? price < ref : price > ref;
        if (!ok) {
          showFieldError(targetInput, side === "long" ? "SL must be below entry" : "SL must be above entry");
          disarm(); return;
        }
      } else {
        const ok = side === "long" ? price > ref : price < ref;
        if (!ok) {
          showFieldError(targetInput, side === "long" ? "TP must be above entry" : "TP must be below entry");
          disarm(); return;
        }
      }
      const pct = Math.abs((price - ref) / ref) * 100;
      useBox.checked = true;
      targetInput.disabled = false;
      targetInput.value = pct.toFixed(2);
      setStatus(`${armedFor.toUpperCase()} set to ${pct.toFixed(2)}% (price ${price.toFixed(getPrecision(price))})`);
      disarm();
    } else if (armedFor === "hline") {
      addHline(price);
      setStatus(`h-line @ ${price.toFixed(getPrecision(price))}`);
      // stay armed so multiple lines can be placed quickly; esc to stop
    } else if (armedFor === "measure") {
      if (!time) return;
      if (!measureFirst) {
        measureFirst = { time, price };
        $("#arm-hint").textContent = "click end of measurement  ·  esc to cancel";
      } else {
        // remove any prior measure series first
        if (measureSeries) { chart.removeSeries(measureSeries); measureSeries = null; }
        finishMeasure({ time, price });
        disarm();
      }
    }
  });

  // ---- Wire up
  setupForm.addEventListener("submit", (e) => { e.preventDefault(); loadSession(); });
  $("#next-1").addEventListener("click", () => {
    const n = parseInt($("#step-n").value, 10) || 1;
    if (isTickSpeed()) tickNext(n);
    else stepN(n);
  });
  $("#back-1").addEventListener("click", () => backN(parseInt($("#step-n").value, 10) || 1));
  $("#play").addEventListener("click", () => (isPlaying() ? stopPlay() : startPlay()));
  $("#long-btn").addEventListener("click", () => placeTrade("long"));
  $("#short-btn").addEventListener("click", () => placeTrade("short"));
  document.querySelectorAll(".ind").forEach((el) => el.addEventListener("change", renderIndicators));
  // Track previous mode so we can drop the pending tick queue when leaving
  // tick mode (those ticks would no longer match where the cursor will be).
  let _prevSpeedTick = isTickSpeed();
  $("#speed").addEventListener("change", () => {
    const wasPlaying = !!(playTimer || tickRAF);
    if (wasPlaying) stopPlay();
    const nowTick = isTickSpeed();
    if (_prevSpeedTick && !nowTick) resetTickReplay();
    _prevSpeedTick = nowTick;
    if (wasPlaying) startPlay();
    else if (nowTick) tickSpeed = tickSpeedFor();   // record speed for next Play
  });
  $("#t-type").addEventListener("change", (e) => {
    const isLimit = e.target.value === "limit";
    $("#limit-row").style.display = isLimit ? "" : "none";
    $("#pick-limit").style.display = isLimit ? "" : "none";
    if (!isLimit) $("#t-limit").value = "";
    if (!isLimit && armedFor === "limit") disarm();
  });
  document.querySelectorAll(".pick").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const kind = btn.dataset.pick;
      if (armedFor === kind) disarm();
      else armFor(kind);
    });
  });
  document.querySelectorAll(".tool").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const kind = btn.dataset.tool;
      if (armedFor === kind) { disarm(); return; }
      if (kind === "measure") clearMeasure();
      armFor(kind);
    });
  });
  $("#clear-drawings").addEventListener("click", clearAllDrawings);

  // collapsible side-pane panels — click a header to toggle
  document.querySelectorAll(".side-pane > .trade-panel > h3, " +
                           ".side-pane > .tape-panel > h3, " +
                           ".side-pane > .trades-panel > h3").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("collapsed"));
  });

  // SL/TP toggle behavior
  const wireToggle = (box, input) => {
    box.addEventListener("change", () => {
      input.disabled = !box.checked;
      if (!box.checked) { input.value = ""; clearFieldError(input); }
      else input.focus();
    });
  };
  wireToggle($("#use-sl"), $("#t-sl-pct"));
  wireToggle($("#use-tp"), $("#t-tp-pct"));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && armedFor) { disarm(); return; }
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowRight") { e.preventDefault(); stepN(parseInt($("#step-n").value, 10) || 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); backN(parseInt($("#step-n").value, 10) || 1); }
    else if (e.key === " ") { e.preventDefault(); isPlaying() ? stopPlay() : startPlay(); }
    else if (e.key.toLowerCase() === "l") placeTrade("long");
    else if (e.key.toLowerCase() === "s") placeTrade("short");
    else if (e.key.toLowerCase() === "h") armFor("hline");
    else if (e.key.toLowerCase() === "m") { clearMeasure(); armFor("measure"); }
    else if (e.key.toLowerCase() === "r") { resetZoomLock(); setStatus("zoom reset"); }
  });

  loadSession();
})();
