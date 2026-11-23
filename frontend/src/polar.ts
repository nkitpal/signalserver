// Polar plots of antenna patterns (attenuation in dB, 0 = strongest).

const RANGE_DB = 40; // centre of the plot

/**
 * Horizontal cut: 0° (boresight) at the top, clockwise, like a compass.
 * Vertical cut: 0° (front horizon) to the right, 90° (straight down) at the bottom.
 */
export function polarChart(att: number[], kind: "horizontal" | "vertical", size = 220, title?: string): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const pad = 30;
  const r = size / 2 - pad;
  const c = size / 2;
  svg.setAttribute("viewBox", `0 0 ${size} ${size + (title ? 14 : 0)}`);
  svg.setAttribute("class", "polar");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${kind} antenna pattern`);
  const add = (tag: string, attrs: Record<string, string | number>) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.appendChild(el);
    return el;
  };
  for (let db = 0; db <= RANGE_DB; db += 10) {
    const rr = r * (1 - db / RANGE_DB);
    if (rr <= 0) continue;
    add("circle", { cx: c, cy: c, r: rr, class: "ring" });
    add("text", { x: c + 2, y: c - rr + 9, class: "tick" }).textContent = db ? `-${db}` : "0 dB";
  }
  for (let a = 0; a < 360; a += 30) {
    const [x, y] = point(a, r, kind, c);
    add("line", { x1: c, y1: c, x2: x, y2: y, class: "spoke" });
  }
  const labels: [number, string][] = kind === "horizontal"
    ? [[0, "0°"], [90, "90°"], [180, "180°"], [270, "270°"]]
    : [[0, "front"], [90, "down"], [180, "back"], [270, "up"]];
  for (const [a, text] of labels) {
    const [x, y] = point(a, r + 14, kind, c);
    add("text", { x, y: y + 3, "text-anchor": "middle", class: "tick axis" }).textContent = text;
  }
  const d = att
    .map((v, a) => {
      const rr = r * Math.max(0, 1 - Math.min(v, RANGE_DB) / RANGE_DB);
      const [x, y] = point(a, rr, kind, c);
      return `${a ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join("") + "Z";
  add("path", { d, class: "lobe" });
  if (title) add("text", { x: c, y: size + 10, "text-anchor": "middle", class: "title" }).textContent = title;
  return svg;
}

function point(angle: number, radius: number, kind: "horizontal" | "vertical", c: number): [number, number] {
  const rad = (angle * Math.PI) / 180;
  if (kind === "horizontal") return [c + radius * Math.sin(rad), c - radius * Math.cos(rad)];
  return [c + radius * Math.cos(rad), c + radius * Math.sin(rad)];
}
