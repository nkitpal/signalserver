// SVG path-profile chart for point-to-point analysis.
import type { PathReport } from "./api";

const SERIES: { key: string; label: string; color: string; fill?: boolean; dash?: string }[] = [
  { key: "profile", label: "Terrain", color: "#8a6a3b", fill: true },
  { key: "reference", label: "Line of sight", color: "#e8edf3", dash: "5 4" },
  { key: "fresnel", label: "1st Fresnel zone", color: "#9bd84d" },
  { key: "fresnel60", label: "60% Fresnel", color: "#ff8a3d" },
  { key: "curvature", label: "Earth curvature", color: "#7b8794" },
];

export function profileChart(report: PathReport, width = 340, height = 190): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Path profile between transmitter and receiver");
  const series = report.series ?? {};
  const all = SERIES.flatMap((s) => series[s.key] ?? []);
  if (!all.length) return svg;
  const pad = { l: 40, r: 22, t: 8, b: 22 };
  const xs = all.map((p) => p[0]);
  const ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(0, Math.min(...ys)), y1 = Math.max(...ys) * 1.05;
  const sx = (x: number) => pad.l + ((x - x0) / (x1 - x0 || 1)) * (width - pad.l - pad.r);
  const sy = (y: number) => height - pad.b - ((y - y0) / (y1 - y0 || 1)) * (height - pad.t - pad.b);

  const add = (tag: string, attrs: Record<string, string | number>) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.appendChild(el);
    return el;
  };
  // grid + axes
  for (let i = 0; i <= 4; i++) {
    const y = y0 + ((y1 - y0) * i) / 4;
    add("line", { x1: pad.l, x2: width - pad.r, y1: sy(y), y2: sy(y), class: "grid" });
    add("text", { x: pad.l - 4, y: sy(y) + 3, "text-anchor": "end", class: "tick" }).textContent =
      Math.round(y).toString();
  }
  for (let i = 0; i <= 4; i++) {
    const x = x0 + ((x1 - x0) * i) / 4;
    add("text", { x: sx(x), y: height - 6, "text-anchor": "middle", class: "tick" }).textContent =
      `${x.toFixed(1)}${report.units?.distance ?? ""}`;
  }
  for (const s of SERIES) {
    const pts = series[s.key];
    if (!pts?.length) continue;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    if (s.fill) {
      add("path", {
        d: `${d}L${sx(pts[pts.length - 1][0])},${sy(y0)}L${sx(pts[0][0])},${sy(y0)}Z`,
        fill: s.color,
        "fill-opacity": 0.55,
        stroke: s.color,
      });
    } else {
      add("path", { d, fill: "none", stroke: s.color, "stroke-width": 1.5, "stroke-dasharray": s.dash ?? "" });
    }
  }
  return svg;
}

export function chartLegend(): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "chart-legend";
  for (const s of SERIES) {
    const item = document.createElement("span");
    item.innerHTML = `<i style="background:${s.color}"></i>${s.label}`;
    wrap.appendChild(item);
  }
  return wrap;
}
