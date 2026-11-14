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
  const withUnit = (def) => (v) => (v == null ? "–" : v + (def.unit ? " " + def.unit : ""));

  function chrome(def, extra) {
    const text = css("--muted");
    const grid = css("--border");
    return Object.assign({
      animation: false,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
      tooltip: { trigger: "axis", valueFormatter: withUnit(def), axisPointer: { type: "line", lineStyle: { color: grid } } },
      xAxis: { type: "category", data: def.labels, axisLabel: { color: text, hideOverlap: true, formatter: fmtDate }, axisLine: { lineStyle: { color: grid } }, axisTick: { show: false } },
      yAxis: { type: "value", scale: def.kind !== "bar", axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } }, axisLine: { show: false } },
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
      else if (def.kind === "points") series.push({ ...base, type: "line", connectNulls: true, showSymbol: true, symbolSize: 7, lineStyle: { width: 1, type: "dotted", color } });
      else series.push({ ...base, type: "line", connectNulls: false, showSymbol: def.labels.length <= 40, symbolSize: 5, lineStyle: { width: 2, color } });
    }
    if (def.kind === "acwr" && def.band && series.length) {
      series[0].markArea = { silent: true, itemStyle: { color: css("--band") }, data: [[{ yAxis: def.band[0] }, { yAxis: def.band[1] }]] };
    }
    const zoom = def.zoom && def.labels.length > 14;
    const option = chrome(def, {
      grid: { left: 44, right: 12, top: visible.length > 1 ? 30 : 12, bottom: zoom ? 48 : 28 },
      legend: visible.length > 1 ? { top: 0, right: 0, data: visible, textStyle: { color: css("--muted") }, itemWidth: 14, itemHeight: 8 } : undefined,
      // the inside zoom only answers to pinch, so the wheel and a finger keep scrolling the page; the slider does the rest
      dataZoom: zoom ? [{ type: "inside", filterMode: "none", zoomOnMouseWheel: false, moveOnMouseMove: false, moveOnMouseWheel: false }, { type: "slider", height: 16, bottom: 4, brushSelect: false, filterMode: "none", borderColor: css("--border") }] : undefined,
      series,
    });
    if (def.kind === "acwr") { option.yAxis.min = 0; option.yAxis.max = 3; option.yAxis.scale = false; }  // a lone workout after a break is "4": off the chart, in the tooltip
    return option;
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

  function option(def) {
    if (def.kind === "sparkline") return sparkline(def);
    if (def.kind === "intraday") return intraday(def);
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
        chart.setOption(option(def));
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

  const redraw = () => instances.forEach(({ chart, def }) => chart.setOption(option(def), true));
  window.addEventListener("resize", () => instances.forEach(({ chart }) => chart.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  document.addEventListener("wristpull:theme", redraw);
  document.addEventListener("htmx:afterSwap", (event) => init(event.target.ownerDocument ? document : document));
  init(document);
})();
