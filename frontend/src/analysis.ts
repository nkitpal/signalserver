// Per-pixel coverage values in the browser: hover read-outs and multi-plot composites.
import type { RunSummary } from "./api";

export interface ValueGrid {
  run: RunSummary;
  width: number;
  height: number;
  index: Uint8Array; // band index per pixel, 255 = no coverage
}

const cache = new Map<number, Promise<ValueGrid | null>>();

/** Load a run's values.png (8-bit band indices) into memory. */
export function loadValues(run: RunSummary): Promise<ValueGrid | null> {
  if (!run.values_url || run.value_top == null || run.value_step == null) return Promise.resolve(null);
  let p = cache.get(run.id);
  if (!p) {
    p = new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = img.width;
        canvas.height = img.height;
        const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
        ctx.drawImage(img, 0, 0);
        const rgba = ctx.getImageData(0, 0, img.width, img.height).data;
        const index = new Uint8Array(img.width * img.height);
        for (let i = 0; i < index.length; i++) index[i] = rgba[i * 4];
        resolve({ run, width: img.width, height: img.height, index });
      };
      img.onerror = () => resolve(null);
      img.src = run.values_url!;
    });
    cache.set(run.id, p);
  }
  return p;
}

export function valueAt(g: ValueGrid, lat: number, lon: number): number | null {
  const r = g.run;
  if (r.north == null || r.south == null || r.east == null || r.west == null) return null;
  if (lat > r.north || lat < r.south || lon < r.west || lon > r.east) return null;
  const col = Math.min(g.width - 1, Math.floor(((lon - r.west) / (r.east - r.west)) * g.width));
  const row = Math.min(g.height - 1, Math.floor(((r.north - lat) / (r.north - r.south)) * g.height));
  const idx = g.index[row * g.width + col];
  if (idx === 255) return null;
  return r.value_top! + idx * r.value_step!;
}

/** True when bigger values are better (signal); false for path loss. */
export const higherIsBetter = (unit: string) => unit !== "dB";

export type CompositeMode = "best" | "overlap" | "strongest";

export const PLOT_COLORS = [
  "#4da3ff", "#ff7a45", "#3ddc84", "#c77dff", "#ffd23f", "#ff5c8a", "#2ec4b6", "#a0c4ff",
  "#f15bb5", "#9ef01a", "#fb8500", "#00bbf9",
];

export interface Composite {
  canvas: HTMLCanvasElement;
  west: number;
  south: number;
  east: number;
  north: number;
  counts: number[]; // pixels won (best) or per overlap level
}

/**
 * Render several runs onto one grid covering their union.
 * - best: each pixel takes the colour of the plot with the strongest signal (best server)
 * - overlap: colour by how many plots cover the pixel above the threshold
 * - strongest: the strongest value of any plot, coloured with ``palette``
 */
export function composite(
  grids: ValueGrid[],
  mode: CompositeMode,
  threshold: number | null,
  colors: string[],
  palette: { value: number; color: string }[],
  maxPixels = 4_000_000,
): Composite | null {
  const valid = grids.filter((g) => g.run.north != null);
  if (!valid.length) return null;
  const unit = valid[0].run.units;
  const better = higherIsBetter(unit);
  const west = Math.min(...valid.map((g) => g.run.west!));
  const east = Math.max(...valid.map((g) => g.run.east!));
  const south = Math.min(...valid.map((g) => g.run.south!));
  const north = Math.max(...valid.map((g) => g.run.north!));
  // Finest source resolution, capped to maxPixels.
  let dpp = Math.min(...valid.map((g) => (g.run.east! - g.run.west!) / g.width));
  let width = Math.ceil((east - west) / dpp);
  let height = Math.ceil((north - south) / dpp);
  const scale = Math.sqrt((width * height) / maxPixels);
  if (scale > 1) {
    dpp *= scale;
    width = Math.ceil((east - west) / dpp);
    height = Math.ceil((north - south) / dpp);
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(width, height);
  const out = img.data;
  const rgb = colors.map(hexToRgb);
  const pal = [...palette].sort((a, b) => (better ? b.value - a.value : a.value - b.value)).map((p) => ({ v: p.value, c: hexToRgb(p.color) }));
  const overlapRamp = ["#4da3ff", "#3ddc84", "#ffd23f", "#ff7a45", "#ff3d3d", "#c77dff"].map(hexToRgb);
  const counts = new Array(Math.max(valid.length + 1, 2)).fill(0);
  const pass = (v: number) => threshold == null || (better ? v >= threshold : v <= threshold);

  // Precompute column/row lookups per grid.
  const lookups = valid.map((g) => {
    const r = g.run;
    const cols = new Int32Array(width);
    for (let x = 0; x < width; x++) {
      const lon = west + (x + 0.5) * dpp;
      const c = Math.floor(((lon - r.west!) / (r.east! - r.west!)) * g.width);
      cols[x] = c >= 0 && c < g.width ? c : -1;
    }
    const rows = new Int32Array(height);
    for (let y = 0; y < height; y++) {
      const lat = north - (y + 0.5) * dpp;
      const rr = Math.floor(((r.north! - lat) / (r.north! - r.south!)) * g.height);
      rows[y] = rr >= 0 && rr < g.height ? rr : -1;
    }
    return { g, cols, rows, top: r.value_top!, step: r.value_step! };
  });

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let best = -1;
      let bestV = better ? -Infinity : Infinity;
      let n = 0;
      for (let k = 0; k < lookups.length; k++) {
        const L = lookups[k];
        const c = L.cols[x];
        const r = L.rows[y];
        if (c < 0 || r < 0) continue;
        const idx = L.g.index[r * L.g.width + c];
        if (idx === 255) continue;
        const v = L.top + idx * L.step;
        if (!pass(v)) continue;
        n++;
        if (better ? v > bestV : v < bestV) {
          bestV = v;
          best = k;
        }
      }
      if (best < 0) continue;
      const o = (y * width + x) * 4;
      let col: [number, number, number];
      if (mode === "best") {
        col = rgb[best % rgb.length];
        counts[best]++;
      } else if (mode === "overlap") {
        col = overlapRamp[Math.min(n, overlapRamp.length) - 1];
        counts[n]++;
      } else {
        const band = pal.find((p) => (better ? bestV >= p.v : bestV <= p.v)) ?? pal[pal.length - 1];
        if (!band) continue;
        col = band.c;
      }
      out[o] = col[0];
      out[o + 1] = col[1];
      out[o + 2] = col[2];
      out[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return { canvas, west, south: north - height * dpp, east: west + width * dpp, north, counts };
}

export function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export const OVERLAP_COLORS = ["#4da3ff", "#3ddc84", "#ffd23f", "#ff7a45", "#ff3d3d", "#c77dff"];
