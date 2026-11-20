// Measuring tools: distance (geodesic), area (geodesic polygon) and terrain profile.
import {
  CallbackProperty,
  Cartesian3,
  Color,
  EllipsoidGeodesic,
  Cartographic,
  Entity,
  HeightReference,
  LabelStyle,
  PolygonHierarchy,
  VerticalOrigin,
  Cartesian2,
} from "cesium";
import type { Globe } from "./globe";

export type MeasureMode = "distance" | "area" | "profile";

export interface MeasureResult {
  mode: MeasureMode;
  points: [number, number][]; // [lon, lat]
  distance_m: number;
  area_m2: number;
  segments_m: number[];
  bearing_deg: number | null;
  done: boolean;
}

const R = 6371008.8;

export function geodesicDistance(a: [number, number], b: [number, number]): number {
  const g = new EllipsoidGeodesic(
    Cartographic.fromDegrees(a[0], a[1]),
    Cartographic.fromDegrees(b[0], b[1]),
  );
  return g.surfaceDistance;
}

export function initialBearing(a: [number, number], b: [number, number]): number {
  const [l1, p1, l2, p2] = [a[0], a[1], b[0], b[1]].map((d) => (d * Math.PI) / 180);
  const y = Math.sin(l2 - l1) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(l2 - l1);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

/** Spherical polygon area (m²) from [lon, lat] vertices. */
export function polygonArea(points: [number, number][]): number {
  if (points.length < 3) return 0;
  let total = 0;
  for (let i = 0; i < points.length; i++) {
    const [l1, p1] = points[i].map((d) => (d * Math.PI) / 180);
    const [l2, p2] = points[(i + 1) % points.length].map((d) => (d * Math.PI) / 180);
    total += (l2 - l1) * (2 + Math.sin(p1) + Math.sin(p2));
  }
  return Math.abs((total * R * R) / 2);
}

export function formatDistance(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(m >= 100000 ? 0 : 2)} km` : `${m.toFixed(0)} m`;
}

export function formatArea(m2: number): string {
  return m2 >= 1e6 ? `${(m2 / 1e6).toFixed(2)} km²` : `${m2.toFixed(0)} m²`;
}

export class MeasureTool {
  private points: [number, number][] = [];
  private entities: Entity[] = [];
  private cursor: [number, number] | null = null;
  private done = false;

  constructor(
    private globe: Globe,
    public mode: MeasureMode,
    private onChange: (r: MeasureResult) => void,
  ) {
    globe.clickHook = (lat, lon) => {
      if (this.done) this.reset();
      this.points.push([lon, lat]);
      if (this.mode === "profile" && this.points.length === 2) this.finish();
      this.redraw();
      return true;
    };
    globe.finishHook = () => this.finish();
    const prev = globe.onCursor;
    this.restoreCursor = prev;
    globe.onCursor = (lat, lon) => {
      prev?.(lat, lon);
      if (!this.done && this.points.length) {
        this.cursor = [lon, lat];
        this.emit();
      }
    };
    document.body.classList.add("measuring");
    this.draw();
  }

  private restoreCursor: Globe["onCursor"];

  private finish() {
    if (this.points.length < 2) return;
    this.done = true;
    this.cursor = null;
    this.redraw();
  }

  reset() {
    this.points = [];
    this.done = false;
    this.cursor = null;
    this.redraw();
  }

  destroy() {
    this.globe.clickHook = null;
    this.globe.finishHook = null;
    this.globe.onCursor = this.restoreCursor;
    for (const e of this.entities) this.globe.viewer.entities.remove(e);
    this.entities = [];
    document.body.classList.remove("measuring");
  }

  private live(): [number, number][] {
    return this.cursor && !this.done ? [...this.points, this.cursor] : this.points;
  }

  result(): MeasureResult {
    const pts = this.live();
    const segs = pts.slice(1).map((p, i) => geodesicDistance(pts[i], p));
    const closed = this.mode === "area" && pts.length > 2 ? geodesicDistance(pts[pts.length - 1], pts[0]) : 0;
    return {
      mode: this.mode,
      points: this.points,
      distance_m: segs.reduce((a, b) => a + b, 0) + closed,
      area_m2: this.mode === "area" ? polygonArea(pts) : 0,
      segments_m: segs,
      bearing_deg: pts.length >= 2 ? initialBearing(pts[0], pts[1]) : null,
      done: this.done,
    };
  }

  private emit() {
    this.onChange(this.result());
  }

  private redraw() {
    this.emit();
  }

  private draw() {
    const v = this.globe.viewer;
    const positions = new CallbackProperty(() => {
      const pts = this.live();
      if (pts.length < 2) return [];
      const ring = this.mode === "area" && pts.length > 2 ? [...pts, pts[0]] : pts;
      return Cartesian3.fromDegreesArray(ring.flat());
    }, false);
    this.entities.push(
      v.entities.add({
        polyline: { positions, width: 3, clampToGround: true, material: Color.fromCssColorString("#ffcc33") },
      }),
    );
    if (this.mode === "area") {
      this.entities.push(
        v.entities.add({
          polygon: {
            hierarchy: new CallbackProperty(() => {
              const pts = this.live();
              return new PolygonHierarchy(pts.length > 2 ? Cartesian3.fromDegreesArray(pts.flat()) : []);
            }, false),
            material: Color.fromCssColorString("#ffcc33").withAlpha(0.2),
          },
        }),
      );
    }
    // Vertex markers and a running total label at the last point.
    this.entities.push(
      v.entities.add({
        position: new CallbackProperty(() => {
          const pts = this.live();
          const last = pts[pts.length - 1];
          return last ? Cartesian3.fromDegrees(last[0], last[1]) : Cartesian3.fromDegrees(0, -90);
        }, false) as unknown as Cartesian3,
        label: {
          text: new CallbackProperty(() => {
            const r = this.result();
            if (this.live().length < 2) return "";
            return r.mode === "area" ? formatArea(r.area_m2) : formatDistance(r.distance_m);
          }, false),
          font: "600 14px system-ui, sans-serif",
          style: LabelStyle.FILL_AND_OUTLINE,
          outlineWidth: 3,
          outlineColor: Color.BLACK,
          fillColor: Color.WHITE,
          verticalOrigin: VerticalOrigin.BOTTOM,
          pixelOffset: new Cartesian2(0, -14),
          heightReference: HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        point: {
          pixelSize: 7,
          color: Color.fromCssColorString("#ffcc33"),
          heightReference: HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      }),
    );
  }
}
