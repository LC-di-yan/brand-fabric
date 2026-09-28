/** ECharts 封装：主题与设计令牌联动，暗色切换自动重绘。
 *
 * - makeChart(id)：懒初始化并注册到实例表；窗口尺寸变化自动 resize；
 *   视图切走时容器 display:none 会导致尺寸为 0，切回时由 resizeOnShow 兜底。
 * - chartTheme()：每次取当前 computed style，主题切换后由 refreshAll 重绘。
 */

const instances = new Map();

export function chartTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
  return {
    text: v("--chart-text", "#8595aa"),
    grid: v("--chart-grid", "#e9edf3"),
    series: [
      v("--chart-1", "#2e6cb4"), v("--chart-2", "#8fb0d4"), v("--chart-3", "#d98e2b"),
      v("--chart-4", "#4fa08a"), v("--chart-5", "#8d7fc4"),
    ],
    surface: v("--surface", "#ffffff"),
    accent: v("--chart-1", "#2e6cb4"),
    orange: v("--chart-3", "#d98e2b"),
  };
}

export function makeChart(containerId) {
  let chart = instances.get(containerId);
  if (!chart) {
    const dom = document.getElementById(containerId);
    if (!dom) throw new Error(`图表容器不存在：${containerId}`);
    chart = echarts.init(dom);
    instances.set(containerId, chart);
  }
  // 容器从 display:none 切回可见时尺寸可能为 0，下一帧强制 resize
  requestAnimationFrame(() => chart.resize());
  return chart;
}

const baseText = () => ({ fontSize: 10.5, color: chartTheme().text });

export function axisStyle() {
  const t = chartTheme();
  return {
    axisLine: { lineStyle: { color: t.grid } },
    axisLabel: { ...baseText(), hideOverlap: true },
    splitLine: { lineStyle: { color: t.grid } },
  };
}

export function legendStyle() {
  return { top: 0, textStyle: baseText(), itemWidth: 14, itemHeight: 8, itemGap: 14 };
}

/** 主题切换后重绘全部图表（各视图在 app.js 的 onThemeChange 里调用各自 render）。 */
export function disposeAll() {
  for (const chart of instances.values()) chart.dispose();
  instances.clear();
}

window.addEventListener("resize", () => {
  for (const chart of instances.values()) chart.resize();
});
