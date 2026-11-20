// Offline street map: renders PMTiles vector tiles (Protomaps / OpenStreetMap) into
// canvas tiles for Cesium, entirely in the browser. Packs are served by our API.
import Point from "@mapbox/point-geometry";
import { namedFlavor } from "@protomaps/basemaps";
import { Credit, Rectangle, UrlTemplateImageryProvider, WebMercatorTilingScheme } from "cesium";
import {
  Labelers,
  labelRules,
  paint,
  paintRules,
  sourcesToViews,
  type PaintRule,
  type PreparedTile,
  type View,
} from "protomaps-leaflet";
import type { BasemapPack } from "./api";

const TILE = 256;

/** Cesium imagery provider whose tiles are drawn from a PMTiles vector archive. */
export class VectorTileImageryProvider extends UrlTemplateImageryProvider {
  private views: Map<string, View>;
  private rules: PaintRule[];
  private labelers: Labelers;
  private background: string;

  constructor(pack: BasemapPack, opts: { dark?: boolean; minimumLevel?: number; maximumLevel?: number; world?: boolean } = {}) {
    const rect = opts.world ? undefined : Rectangle.fromDegrees(pack.west, pack.south, pack.east, pack.north);
    super({
      url: "data:,", // unused: requestImage is overridden
      tilingScheme: new WebMercatorTilingScheme(),
      tileWidth: TILE,
      tileHeight: TILE,
      minimumLevel: opts.minimumLevel ?? 0,
      maximumLevel: opts.maximumLevel ?? 18,
      rectangle: rect,
      credit: new Credit("© OpenStreetMap contributors, Protomaps (offline)"),
    });
    const flavor = namedFlavor(opts.dark ? "dark" : "light");
    this.views = sourcesToViews({ url: pack.url, maxDataZoom: pack.max_zoom });
    this.rules = paintRules(flavor);
    const scratch = document.createElement("canvas").getContext("2d")!;
    this.labelers = new Labelers(scratch, labelRules(flavor, "en"), 16, () => {});
    this.background = flavor.background;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  override requestImage(x: number, y: number, level: number): any {
    return this.render(x, y, level);
  }

  private async render(x: number, y: number, z: number): Promise<HTMLCanvasElement> {
    const prepared = new Map<string, PreparedTile[]>();
    for (const [key, view] of this.views) {
      try {
        prepared.set(key, [await view.getDisplayTile({ x, y, z })]);
      } catch {
        /* tile outside the pack: leave empty */
      }
    }
    const canvas = document.createElement("canvas");
    canvas.width = TILE;
    canvas.height = TILE;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = this.background;
    ctx.fillRect(0, 0, TILE, TILE);
    if (!prepared.size) return canvas;
    this.labelers.add(z, prepared);
    const labels = this.labelers.getIndex(z) ?? null;
    const buf = 16;
    const bbox = { minX: TILE * x - buf, minY: TILE * y - buf, maxX: TILE * (x + 1) + buf, maxY: TILE * (y + 1) + buf };
    const origin = new Point(TILE * x, TILE * y);
    paint(ctx, z, prepared, null, this.rules, bbox, origin, false);
    if (labels) paint(ctx, z, prepared, labels, [], bbox, origin, true);
    return canvas;
  }
}
