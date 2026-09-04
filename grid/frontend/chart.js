/* Compact forecast chart for the left panel: recent metered load on the left,
   the LightGBM forward curve on the right, split at "now". Inline SVG -- no
   charting dependency for two polylines. */

const SVGNS = "http://www.w3.org/2000/svg";
const W = 268, H = 92, PAD = 6;

function node(name, attrs) {
  const n = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

const ForecastChart = {
  render(svg, data) {
    svg.innerHTML = "";
    const past = data.past || [];
    const future = data.future || [];
    if (past.length + future.length < 2) return;

    const values = [...past.map((p) => p.actual_mw), ...future.map((f) => f.predicted_mw)];
    const lo = Math.min(...values) * 0.97;
    const hi = Math.max(...values) * 1.03;
    const span = hi - lo || 1;
    const n = values.length - 1;

    const x = (i) => PAD + (i / n) * (W - 2 * PAD);
    const y = (v) => H - PAD - ((v - lo) / span) * (H - 2 * PAD);

    // horizontal guides
    for (let g = 0; g <= 2; g++) {
      const gy = PAD + (g / 2) * (H - 2 * PAD);
      svg.appendChild(node("line", {
        x1: PAD, x2: W - PAD, y1: gy, y2: gy,
        stroke: "#1a2436", "stroke-width": 1,
      }));
    }

    const splitIndex = Math.max(0, past.length - 1);
    const splitX = x(splitIndex);

    // shaded forecast region so "predicted" is obvious at a glance
    svg.appendChild(node("rect", {
      x: splitX, y: PAD, width: W - PAD - splitX, height: H - 2 * PAD,
      fill: "rgba(0,229,255,0.06)",
    }));
    svg.appendChild(node("line", {
      x1: splitX, x2: splitX, y1: PAD, y2: H - PAD,
      stroke: "#26354d", "stroke-width": 1, "stroke-dasharray": "2 2",
    }));

    const actualPts = past.map((p, i) => `${x(i)},${y(p.actual_mw)}`).join(" ");
    svg.appendChild(node("polyline", {
      points: actualPts, fill: "none", stroke: "#7e90a8", "stroke-width": 1.6,
    }));

    // start the forecast line at the last actual so the curve is continuous
    const predPts = [
      `${x(splitIndex)},${y(past.length ? past[past.length - 1].actual_mw : future[0].predicted_mw)}`,
      ...future.map((f, i) => `${x(past.length + i)},${y(f.predicted_mw)}`),
    ].join(" ");
    svg.appendChild(node("polyline", {
      points: predPts, fill: "none", stroke: "#00e5ff", "stroke-width": 2,
      "stroke-linejoin": "round",
    }));

    if (future.length) {
      const last = future[future.length - 1];
      svg.appendChild(node("circle", {
        cx: x(values.length - 1), cy: y(last.predicted_mw), r: 2.6,
        fill: "#00e5ff",
      }));
    }

    const peak = Math.max(...future.map((f) => f.predicted_mw), 0);
    const el = document.getElementById("chartPeak");
    if (el) el.textContent = peak ? `peak ${Math.round(peak)} MW` : "";
  },
};

window.ForecastChart = ForecastChart;
