// Copy buttons (the command line of a job, paths): the clipboard needs a user gesture and a secure context,
// which Caddy's HTTPS provides; without it the button just shows the text selected.
(function () {
  "use strict";
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const text = button.getAttribute("data-copy");
    const done = () => { const label = button.textContent; button.textContent = "✓"; setTimeout(() => { button.textContent = label; }, 1200); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => window.prompt("Copy:", text));
    else window.prompt("Copy:", text);
  });
})();

// The phone's "More" sheet (the pages that do not fit into the bottom bar) and the export form's period presets.
(function () {
  "use strict";
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-more-toggle]");
    const sheet = document.getElementById("more-sheet");
    if (toggle && sheet) {
      const open = sheet.hasAttribute("hidden");
      if (open) sheet.removeAttribute("hidden"); else sheet.setAttribute("hidden", "");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (sheet && !sheet.hasAttribute("hidden") && !event.target.closest("#more-sheet")) {
      sheet.setAttribute("hidden", "");
      document.querySelectorAll("[data-more-toggle]").forEach((b) => b.setAttribute("aria-expanded", "false"));
    }
    const preset = event.target.closest(".chip-button[data-from]");
    if (preset) {
      const form = preset.closest("form");
      const from = form && form.querySelector('input[name="from"]');
      const to = form && form.querySelector('input[name="to"]');
      if (from && to) { from.value = preset.getAttribute("data-from"); to.value = preset.getAttribute("data-to"); }
    }
  });
})();

// Theme toggle: system → light → dark, remembered per browser; theme.js applies it before the first paint.
(function () {
  "use strict";
  const KEY = "wristpull-theme";
  const ORDER = ["system", "light", "dark"];
  const current = () => { try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; } };
  function apply(mode) {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("data-mode", mode);
      const label = button.querySelector("[data-theme-label]");
      if (label) label.textContent = button.getAttribute("data-label-" + mode) || mode;
    });
    document.dispatchEvent(new CustomEvent("wristpull:theme"));
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme-toggle]");
    if (!button) return;
    const next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
    try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
    apply(next);
  });
  apply(current());
})();

// Charts: every <script type="application/json" data-charts> block holds chart definitions built in Python
// (web/pages.py); each definition draws into <div id="chart-<id>">. Missing days are null: gaps, not zeros.
(function () {
  "use strict";
  if (!window.echarts) return;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const instances = new Map(); // element -> { chart, def }
  const two = (n) => String(n).padStart(2, "0");
  const fmtDate = (v) => (typeof v === "string" && v.length === 10 ? v.slice(8, 10) + "." + v.slice(5, 7) + "." : v);
  const fmtTime = (m) => two(Math.floor(m / 60)) + ":" + two(Math.round(m % 60));
  const fmtPace = (v) => { const m = Math.floor(v), sec = Math.round((v - m) * 60); return m + ":" + two(sec === 60 ? 0 : sec) + (sec === 60 ? "" : ""); };
  const fmtClock18 = (v) => fmtTime((1080 + v) % 1440);
  const fmtValue = (def) => (v) => (v == null ? "–" : (def.format === "pace" ? fmtPace(v) : (def.format === "clock18" ? fmtClock18(v) : v)));
  const withUnit = (def) => (v) => (v == null ? "–" : fmtValue(def)(v) + (def.unit ? " " + def.unit : ""));

  function chrome(def, extra) {
    const text = css("--muted");
    const grid = css("--border");
    return Object.assign({
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
      tooltip: { trigger: "axis", valueFormatter: withUnit(def), axisPointer: { type: "line", lineStyle: { color: grid } } },
      xAxis: { type: "category", data: def.labels, axisLabel: { color: text, hideOverlap: true, formatter: fmtDate }, axisLine: { lineStyle: { color: grid } }, axisTick: { show: false } },
      yAxis: { type: "value", scale: def.kind !== "bar", inverse: !!def.inverse, axisLabel: { color: text, formatter: def.format === "pace" ? fmtPace : (def.format === "clock18" ? fmtClock18 : undefined) }, splitLine: { lineStyle: { color: grid } }, axisLine: { show: false } },
    }, extra);
  }

  // Daily series: watch and strap as categorical colours, the 7-day mean as a thin muted line, the 28-day band as a
  // stacked area pair (low transparent, high - low filled) that stays out of the legend and the tooltip.
  function daily(def) {
    const colors = [css("--series-1"), css("--series-2"), css("--series-3")];
    const visible = [];
    const series = [];
    let slot = 0;
    const low = def.series.find((s) => s.role === "band_low");
    for (const s of def.series) {
      const role = s.role || (slot === 0 ? "watch" : "strap");
      if (role === "band_low") {
        series.push({ name: s.name, type: "line", stack: "band", data: s.data, silent: true, symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, tooltip: { show: false }, z: 1 });
        continue;
      }
      if (role === "band_high") {
        const diff = s.data.map((v, i) => (v == null || !low || low.data[i] == null ? null : Math.round((v - low.data[i]) * 1000) / 1000));
        series.push({ name: s.name, type: "line", stack: "band", data: diff, silent: true, symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: css("--band"), opacity: 1 }, tooltip: { show: false }, z: 1 });
        continue;
      }
      if (role === "mean") {
        visible.push(s.name);
        series.push({ name: s.name, type: "line", data: s.data, symbol: "none", connectNulls: true, lineStyle: { width: 1, color: css("--series-mean") }, itemStyle: { color: css("--series-mean") }, z: 2 });
        continue;
      }
      const color = colors[slot % colors.length];
      slot += 1;
      visible.push(s.name);
      const base = { name: s.name, data: s.data, itemStyle: { color }, z: 3 };
      if (def.kind === "bar") series.push({ ...base, type: "bar", barMaxWidth: def.deviation ? 28 : 14, barGap: "10%", itemStyle: { color: def.deviation ? undefined : color, borderRadius: def.deviation ? 3 : [3, 3, 0, 0] }, ...(def.deviation ? { data: s.data.map((v) => ({ value: v, itemStyle: { color: v == null || v >= 0 ? colors[0] : colors[1] } })) } : {}) });
      else if (def.kind === "stacked") series.push({ ...base, type: "bar", stack: "total", barMaxWidth: 22, itemStyle: { color: s.name === "Other" ? css("--series-mean") : color, borderColor: css("--surface"), borderWidth: 1 } });
      else if (def.kind === "points") series.push({ ...base, type: "line", connectNulls: true, showSymbol: true, symbolSize: 7, lineStyle: { width: 1, type: "dotted", color } });
      else series.push({ ...base, type: "line", connectNulls: false, showSymbol: def.labels.length <= 40, symbolSize: 5, lineStyle: { width: 2, color }, areaStyle: def.area ? { color, opacity: 0.14 } : undefined, sampling: def.labels.length > 400 ? "lttb" : undefined });
    }
    if (def.kind === "acwr" && def.band && series.length) {
      series[0].markArea = { silent: true, itemStyle: { color: css("--band") }, data: [[{ yAxis: def.band[0] }, { yAxis: def.band[1] }]] };
    }
    const zoom = def.zoom && def.labels.length > 14;
    const option = chrome(def, {
      grid: { left: 48, right: 12, top: visible.length > 1 ? 30 : 12, bottom: zoom ? 48 : 28 },
      legend: visible.length > 1 ? { top: 0, right: 0, data: visible, textStyle: { color: css("--muted") }, itemWidth: 14, itemHeight: 8 } : undefined,
      // the inside zoom only answers to pinch, so the wheel and a finger keep scrolling the page; the slider does the rest
      dataZoom: zoom ? [{ type: "inside", filterMode: "none", zoomOnMouseWheel: false, moveOnMouseMove: false, moveOnMouseWheel: false }, { type: "slider", height: 16, bottom: 4, brushSelect: false, filterMode: "none", borderColor: css("--border") }] : undefined,
      series,
    });
    if (def.kind === "acwr") { option.yAxis.min = 0; option.yAxis.max = 3; option.yAxis.scale = false; }  // a lone workout after a break is "4": off the chart, in the tooltip
    if (def.kind === "stacked") option.yAxis.scale = false;
    if (def.range) { option.yAxis.min = def.range[0]; option.yAxis.max = def.range[1]; option.yAxis.scale = false; }
    if (def.guides && series.length) {  // quiet horizontal guides (readiness: 45 and 70)
      series[0].markLine = { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: css("--series-mean"), type: "dashed", width: 1 }, data: def.guides.map((y) => ({ yAxis: y })) };
    }
    if (def.group === "activity") option.xAxis.axisLabel.formatter = (v) => v + " min";
    return option;
  }

  // Relationship of two measures: one dot per day (muted), the bin means on top (accent) — same scale for both.
  function scatter(def) {
    const days = def.series[0] || { points: [] };
    const bins = def.series[1] || { points: [] };
    const fx = def.x_format === "clock18" ? fmtClock18 : (v) => v;
    return {
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
      grid: { left: 48, right: 12, top: 28, bottom: 32 },
      legend: { top: 0, right: 0, textStyle: { color: css("--muted") }, itemWidth: 12, itemHeight: 8 },
      tooltip: { trigger: "item", formatter: (p) => (p.seriesIndex === 1 ? "Ø " : "") + fx(p.value[0]) + (def.x_unit && def.x_unit !== "clock" ? " " + def.x_unit : "") + " → " + p.value[1] + (def.unit ? " " + def.unit : "") },
      xAxis: { type: "value", name: def.x_title, nameLocation: "middle", nameGap: 22, nameTextStyle: { color: css("--muted") }, scale: true, axisLabel: { color: css("--muted"), formatter: fx }, axisLine: { lineStyle: { color: css("--border") } }, splitLine: { show: false } },
      yAxis: { type: "value", scale: true, axisLabel: { color: css("--muted") }, splitLine: { lineStyle: { color: css("--border") } }, axisLine: { show: false } },
      series: [
        { name: days.name, type: "scatter", data: days.points, symbolSize: 7, itemStyle: { color: css("--series-mean"), opacity: 0.55 } },
        { name: bins.name, type: "line", data: bins.points, symbol: "circle", symbolSize: 11, lineStyle: { width: 2, color: css("--series-1") }, itemStyle: { color: css("--series-1"), borderColor: css("--surface"), borderWidth: 2 }, z: 3 },
      ],
    };
  }

  // Part-to-whole in at most six slices (the heart-rate zones): a donut with the zone colours and minutes in the tooltip.
  function donut(def) {
    const s = def.series[0] || { data: [] };
    const zoneColors = [css("--border")].concat([1, 2, 3, 4, 5].map((i) => css("--zone-" + i)));  // entry 0: below zone 1
    const total = s.data.reduce((a, v) => a + (v || 0), 0) || 1;
    return {
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
      tooltip: { trigger: "item", formatter: (p) => p.name + ": " + Math.round(p.value) + " min (" + Math.round(100 * p.value / total) + " %)" },
      legend: { bottom: 0, left: "center", textStyle: { color: css("--muted") }, itemWidth: 12, itemHeight: 10 },
      series: [{ type: "pie", radius: ["52%", "78%"], center: ["50%", "44%"], avoidLabelOverlap: true, padAngle: 2, itemStyle: { borderRadius: 4, borderColor: css("--surface"), borderWidth: 2 },
        label: { show: false }, emphasis: { label: { show: true, formatter: (p) => p.name + "\n" + Math.round(100 * p.value / total) + " %", color: css("--text") } },
        data: def.labels.map((name, i) => ({ name, value: s.data[i] || 0, itemStyle: { color: def.zones ? zoneColors[Math.min(i, zoneColors.length - 1)] : [css("--series-1"), css("--series-2"), css("--series-3")][i % 3] } })) }],
    };
  }

  // The GPS track as a line in a plane with equal scale (no map tiles: nothing leaves the machine).
  function track(def, el) {
    const pts = (def.series[0] || { data: [] }).data;
    const lats = pts.map((p) => p[1]), lons = pts.map((p) => p[0]);
    const midLat = (Math.min(...lats) + Math.max(...lats)) / 2;
    const k = Math.cos(midLat * Math.PI / 180);
    const xs = lons.map((lon) => lon * k);
    const spanX = Math.max(...xs) - Math.min(...xs), spanY = Math.max(...lats) - Math.min(...lats);
    const ratio = (el.clientWidth || 600) / (el.clientHeight || 240);
    let w = Math.max(spanX, spanY * ratio) * 1.1, h = w / ratio;
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...lats) + Math.min(...lats)) / 2;
    return {
      animation: false,
      grid: { left: 4, right: 4, top: 4, bottom: 4 },
      tooltip: { show: false },
      xAxis: { type: "value", min: cx - w / 2, max: cx + w / 2, show: false },
      yAxis: { type: "value", min: cy - h / 2, max: cy + h / 2, show: false },
      series: [
        { type: "line", data: xs.map((x, i) => [x, lats[i]]), symbol: "none", lineStyle: { width: 2.5, color: css("--series-1") }, smooth: false },
        { type: "scatter", data: [[xs[0], lats[0]]], symbolSize: 10, itemStyle: { color: css("--good") } },
        { type: "scatter", data: [[xs[xs.length - 1], lats[lats.length - 1]]], symbolSize: 10, itemStyle: { color: css("--bad") } },
      ],
    };
  }

  // Night time axis: minutes since bedtime, labelled as clock time (x0 = bedtime as minute of the day).
  function nightAxis(def) {
    return { type: "value", min: 0, max: def.xmax, interval: def.xmax > 600 ? 120 : 60, axisLabel: { color: css("--muted"), formatter: (v) => fmtTime((def.x0 + v) % 1440) }, axisLine: { lineStyle: { color: css("--border") } }, splitLine: { show: false } };
  }

  function timeline(def) {
    const color = css("--series-1");
    const s = def.series[0] || { points: [] };
    const option = chrome(def, {
      grid: { left: 48, right: 12, top: 12, bottom: 28 },
      series: [{ name: s.name, type: "line", data: s.points, symbol: "circle", showSymbol: s.points.length <= 12, symbolSize: 6, connectNulls: false, lineStyle: { width: 1.5, color }, itemStyle: { color }, areaStyle: { color, opacity: 0.12 } }],
    });
    option.xAxis = nightAxis(def);
    option.tooltip = { trigger: "axis", valueFormatter: withUnit(def), axisPointer: { label: { formatter: (p) => fmtTime((def.x0 + p.value) % 1440) } } };
    return option;
  }

  // The hypnogram: one block per stage interval, deep at the bottom, awake at the top.
  function hypnogram(def) {
    const stageColors = def.stages.map((name) => css("--stage-" + name));
    const names = def.stage_names || def.stages;
    const blocks = (def.series[0] || { data: [] }).data;
    return {
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
      grid: { left: 64, right: 12, top: 8, bottom: 28 },
      tooltip: { trigger: "item", formatter: (p) => names[p.value[2]] + ": " + fmtTime((def.x0 + p.value[0]) % 1440) + " – " + fmtTime((def.x0 + p.value[1]) % 1440) + " (" + Math.round(p.value[1] - p.value[0]) + " min)" },
      xAxis: nightAxis(def),
      yAxis: { type: "category", data: names, axisLabel: { color: css("--muted") }, axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: true, lineStyle: { color: css("--border") } } },
      series: [{ type: "custom", data: blocks, encode: { x: [0, 1], y: 2 },
        renderItem: (params, api) => {
          const start = api.coord([api.value(0), api.value(2)]), end = api.coord([api.value(1), api.value(2)]);
          const height = api.size([0, 1])[1] * 0.7;
          const rect = window.echarts.graphic.clipRectByRect({ x: start[0], y: start[1] - height / 2, width: Math.max(1, end[0] - start[0]), height }, { x: params.coordSys.x, y: params.coordSys.y, width: params.coordSys.width, height: params.coordSys.height });
          return rect && { type: "rect", shape: rect, style: { fill: stageColors[api.value(2)] } };
        } }],
    };
  }

  function sparkline(def) {
    const s = def.series[0] || { data: [] };
    return {
      animation: false,
      grid: { left: 2, right: 2, top: 4, bottom: 2 },
      xAxis: { type: "category", data: def.labels, show: false },
      yAxis: { type: "value", scale: true, show: false },
      series: [{ type: "line", data: s.data, symbol: "none", connectNulls: true, lineStyle: { width: 1.5, color: css("--series-1") }, areaStyle: { color: css("--series-1"), opacity: 0.12 } }],
    };
  }

  // One day's curve: x = minutes of the local day, the nights shaded, stress painted by band.
  function intraday(def) {
    const colors = [css("--series-1"), css("--series-2")];
    const series = def.series.map((s, i) => ({
      name: s.name, type: "line", data: s.points, symbol: "none", step: def.step ? "end" : false, connectNulls: false,
      lineStyle: { width: 2, color: colors[i % colors.length] }, itemStyle: { color: colors[i % colors.length] },
      areaStyle: def.series.length === 1 ? { color: colors[i % colors.length], opacity: 0.14 } : undefined,
    }));
    if (series.length && def.nights && def.nights.length) {
      series[0].markArea = { silent: true, itemStyle: { color: css("--night") }, data: def.nights.map(([a, b]) => [{ xAxis: a }, { xAxis: b }]) };
    }
    const option = chrome(def, {
      grid: { left: 44, right: 12, top: series.length > 1 ? 30 : 12, bottom: 28 },
      legend: series.length > 1 ? { top: 0, right: 0, textStyle: { color: css("--muted") }, itemWidth: 14, itemHeight: 8 } : undefined,
      series,
    });
    option.xAxis = { type: "value", min: 0, max: 1440, interval: 180, axisLabel: { color: css("--muted"), formatter: fmtTime }, axisLine: { lineStyle: { color: css("--border") } }, splitLine: { show: false } };
    option.tooltip = { trigger: "axis", valueFormatter: withUnit(def), axisPointer: { label: { formatter: (p) => fmtTime(p.value) } } };
    if (def.range) { option.yAxis.min = def.range[0]; option.yAxis.max = def.range[1]; option.yAxis.scale = false; }
    if (def.stress && series.length) {  // Garmin's bands as quiet horizontal guides: rest / low / medium / high
      series[0].markLine = { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: css("--border"), type: "solid", width: 1 }, data: [{ yAxis: 25 }, { yAxis: 50 }, { yAxis: 75 }] };
    }
    return option;
  }

  function option(def, el) {
    if (def.kind === "sparkline") return sparkline(def);
    if (def.kind === "intraday") return intraday(def);
    if (def.kind === "donut") return donut(def);
    if (def.kind === "track") return track(def, el);
    if (def.kind === "timeline") return timeline(def);
    if (def.kind === "hypnogram") return hypnogram(def);
    if (def.kind === "scatter") return scatter(def);
    return daily(def);
  }

  function init(root) {
    for (const [el, inst] of instances) {
      if (!document.contains(el)) { inst.chart.dispose(); instances.delete(el); }
    }
    root.querySelectorAll('script[type="application/json"][data-charts]').forEach((block) => {
      let defs;
      try { defs = JSON.parse(block.textContent); } catch (e) { return; }
      for (const def of defs) {
        const el = document.getElementById("chart-" + def.id);
        if (!el || instances.has(el)) continue;
        const chart = window.echarts.init(el, null, { renderer: "canvas" });
        chart.setOption(option(def, el));
        if (def.group) { chart.group = def.group; window.echarts.connect(def.group); }  // linked tooltips across the curves of one activity or night
        instances.set(el, { chart, def });
        const dayUrl = el.getAttribute("data-day-url");
        if (dayUrl && !def.weekly && window.htmx) {
          chart.on("click", (params) => {
            const day = params.name;
            if (typeof day !== "string" || day.length !== 10) return;
            window.htmx.ajax("GET", dayUrl + "?day=" + day + "&weeks=" + (el.getAttribute("data-weeks") || "4"), { target: "#intraday", swap: "outerHTML" });
          });
        }
      }
    });
  }

  const redraw = () => instances.forEach(({ chart, def }, el) => chart.setOption(option(def, el), true));
  window.addEventListener("resize", () => { instances.forEach(({ chart }) => chart.resize()); instances.forEach(({ chart, def }, el) => { if (def.kind === "track") chart.setOption(option(def, el), true); }); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  document.addEventListener("wristpull:theme", redraw);
  document.addEventListener("htmx:afterSwap", (event) => init(event.target.ownerDocument ? document : document));
  init(document);
})();
