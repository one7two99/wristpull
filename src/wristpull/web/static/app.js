// Draws the trend charts from the JSON block the page carries. Missing days are null: gaps, not zeros.
(function () {
  "use strict";
  const block = document.getElementById("chart-data");
  if (!block || !window.echarts) return;
  const charts = JSON.parse(block.textContent);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const instances = [];

  function option(def) {
    const colors = [css("--series-1"), css("--series-2")];
    const text = css("--muted");
    const grid = css("--border");
    const series = def.series.map((s, i) => {
      const base = { name: s.name, data: s.data, itemStyle: { color: colors[i % colors.length] } };
      if (def.kind === "bar") return { ...base, type: "bar", barMaxWidth: 14 };
      return { ...base, type: "line", connectNulls: false, showSymbol: false, smooth: false, lineStyle: { width: 2, type: i === 0 ? "solid" : "dashed" } };
    });
    if (def.kind === "acwr" && def.band && series.length) {
      series[0].markArea = { silent: true, itemStyle: { color: css("--band") }, data: [[{ yAxis: def.band[0] }, { yAxis: def.band[1] }]] };
    }
    return {
      animation: false,
      grid: { left: 40, right: 12, top: series.length > 1 ? 28 : 12, bottom: 28 },
      legend: series.length > 1 ? { top: 0, right: 0, textStyle: { color: text }, itemWidth: 14, itemHeight: 8 } : undefined,
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: def.labels, axisLabel: { color: text, hideOverlap: true, formatter: (v) => v.length === 10 ? v.slice(8, 10) + "." + v.slice(5, 7) + "." : v }, axisLine: { lineStyle: { color: grid } } },
      yAxis: { type: "value", scale: def.kind !== "bar", axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } } },
      series,
    };
  }

  for (const def of charts) {
    const el = document.getElementById("chart-" + def.id);
    if (!el) continue;
    const chart = window.echarts.init(el, null, { renderer: "canvas" });
    chart.setOption(option(def));
    instances.push([chart, def]);
  }
  window.addEventListener("resize", () => instances.forEach(([chart]) => chart.resize()));
  const scheme = window.matchMedia("(prefers-color-scheme: dark)");
  scheme.addEventListener("change", () => instances.forEach(([chart, def]) => chart.setOption(option(def), true)));
})();
