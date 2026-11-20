// CesiumJS globe: terrain, base imagery, stations, coverage overlays and path lines.
import {
  Cartesian2,
  Cartesian3,
  Cartographic,
  Color,
  CustomHeightmapTerrainProvider,
  Entity,
  GeographicTilingScheme,
  HeightReference,
  ImageryLayer,
  Ion,
  LabelStyle,
  Math as CMath,
  Rectangle,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  SingleTileImageryProvider,
  CesiumTerrainProvider,
  UrlTemplateImageryProvider,
  VerticalOrigin,
  Viewer,
  defined,
  PolylineDashMaterialProperty,
  NearFarScalar,
  DistanceDisplayCondition,
  KmlDataSource,
  type DataSource,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import type { BasemapPack, GeoJSON, Layer, PathReport, RunSummary, Station, ViewerConfig } from "./api";
import type { Composite } from "./analysis";
import { VectorTileImageryProvider } from "./offline";

const TERRAIN_SIZE = 32;

export class Globe {
  viewer: Viewer;
  private baseLayers: ImageryLayer[] = [];
  private baseId = "";
  private packs: BasemapPack[] = [];
  private compositeLayer: ImageryLayer | null = null;
  private kml = new Map<number, DataSource>();
  /** Map click consumer (e.g. measuring tools); return true to stop default handling. */
  clickHook: ((lat: number, lon: number) => boolean) | null = null;
  finishHook: (() => void) | null = null;
  private coverage = new Map<number, ImageryLayer>();
  private stationEntities = new Map<number, Entity>();
  private pathEntities: Entity[] = [];
  private tileEntities: Entity[] = [];
  private pickHandler: ((lat: number, lon: number) => void) | null = null;
  onStationClick: ((id: number) => void) | null = null;
  onCursor: ((lat: number, lon: number) => void) | null = null;

  constructor(container: HTMLElement, private config: ViewerConfig) {
    if (config.cesium_ion_token) Ion.defaultAccessToken = config.cesium_ion_token;
    this.viewer = new Viewer(container, {
      baseLayer: false,
      baseLayerPicker: false,
      geocoder: false,
      timeline: false,
      animation: false,
      sceneModePicker: true,
      navigationHelpButton: true,
      homeButton: true,
      fullscreenButton: false,
      selectionIndicator: true,
      infoBox: false,
      terrainProvider: this.localTerrain(),
    });
    const scene = this.viewer.scene;
    scene.globe.depthTestAgainstTerrain = false;
    scene.globe.enableLighting = false;
    scene.globe.baseColor = Color.fromCssColorString("#1b2330");
    this.setImagery(config.imagery[0]?.id ?? "osm");
    if (config.cesium_ion_token) void this.useWorldTerrain();

    const handler = new ScreenSpaceEventHandler(scene.canvas);
    handler.setInputAction((ev: { position: Cartesian2 }) => {
      const picked = scene.pick(ev.position);
      const ll = this.pickLatLon(ev.position);
      if (ll && this.clickHook && this.clickHook(ll[0], ll[1])) return;
      if (this.pickHandler && ll) {
        const cb = this.pickHandler;
        this.pickHandler = null;
        document.body.classList.remove("picking");
        cb(ll[0], ll[1]);
        return;
      }
      if (defined(picked) && picked.id instanceof Entity) {
        const sid = picked.id.properties?.stationId?.getValue();
        if (typeof sid === "number" && this.onStationClick) this.onStationClick(sid);
      }
    }, ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction(() => this.finishHook?.(), ScreenSpaceEventType.RIGHT_CLICK);
    handler.setInputAction(() => this.finishHook?.(), ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
    handler.setInputAction((ev: { endPosition: Cartesian2 }) => {
      const ll = this.pickLatLon(ev.endPosition);
      if (ll && this.onCursor) this.onCursor(ll[0], ll[1]);
    }, ScreenSpaceEventType.MOUSE_MOVE);

    this.viewer.camera.setView({ destination: Cartesian3.fromDegrees(10, 30, 18_000_000) });
  }

  /** Terrain built from the same DEM tiles that drive the propagation model. */
  private localTerrain() {
    return new CustomHeightmapTerrainProvider({
      width: TERRAIN_SIZE,
      height: TERRAIN_SIZE,
      tilingScheme: new GeographicTilingScheme(),
      callback: async (x: number, y: number, level: number) => {
        if (level < 6) return new Float32Array(TERRAIN_SIZE * TERRAIN_SIZE);
        const res = await fetch(`/api/elevation/terrain/${level}/${x}/${y}.f32`);
        if (!res.ok) return new Float32Array(TERRAIN_SIZE * TERRAIN_SIZE);
        return new Float32Array(await res.arrayBuffer());
      },
    });
  }

  private async useWorldTerrain() {
    try {
      this.viewer.terrainProvider = await CesiumTerrainProvider.fromIonAssetId(1);
    } catch (e) {
      console.warn("Cesium World Terrain unavailable, using local DEM terrain", e);
    }
  }

  /** Base map: an online tile template id, "offline" (downloaded packs) or "none". */
  setImagery(id: string) {
    this.baseId = id;
    const layers = this.viewer.imageryLayers;
    for (const l of this.baseLayers) layers.remove(l, true);
    this.baseLayers = [];
    if (id === "none") return;
    if (id === "offline") {
      const ready = this.packs.filter((p) => p.ready);
      if (!ready.length) return;
      // Low zooms for the whole world from the first pack, detail inside every pack.
      this.baseLayers.push(layers.addImageryProvider(
        new VectorTileImageryProvider(ready[0], { world: true, maximumLevel: 4 }), 0));
      for (const p of ready)
        this.baseLayers.push(layers.addImageryProvider(
          new VectorTileImageryProvider(p, { minimumLevel: 0, maximumLevel: 18 }), this.baseLayers.length));
      return;
    }
    const src = this.config.imagery.find((i) => i.id === id) ?? this.config.imagery[0];
    if (!src) return;
    this.baseLayers.push(layers.addImageryProvider(
      new UrlTemplateImageryProvider({ url: src.url, credit: src.credit, maximumLevel: 19 }),
      0,
    ));
  }

  setOfflinePacks(packs: BasemapPack[]) {
    this.packs = packs;
    if (this.baseId === "offline") this.setImagery("offline");
  }

  // Imported KML layers ------------------------------------------------------
  async showKml(layer: Layer) {
    if (this.kml.has(layer.id)) return;
    try {
      const ds = await KmlDataSource.load(layer.url, {
        camera: this.viewer.scene.camera,
        canvas: this.viewer.scene.canvas,
        clampToGround: true,
      });
      await this.viewer.dataSources.add(ds);
      this.kml.set(layer.id, ds);
    } catch (e) {
      console.error("KML load failed", e);
      throw e;
    }
  }

  hideKml(id: number) {
    const ds = this.kml.get(id);
    if (ds) {
      this.viewer.dataSources.remove(ds, true);
      this.kml.delete(id);
    }
  }

  hasKml(id: number) {
    return this.kml.has(id);
  }

  flyToKml(layer: Layer) {
    const ds = this.kml.get(layer.id);
    if (ds) void this.viewer.flyTo(ds, { duration: 1.6 });
    else if (layer.bounds)
      this.viewer.camera.flyTo({ destination: Rectangle.fromDegrees(...layer.bounds), duration: 1.6 });
  }

  // Multi-plot composite -----------------------------------------------------
  showComposite(c: Composite | null, alpha = 0.75) {
    if (this.compositeLayer) {
      this.viewer.imageryLayers.remove(this.compositeLayer, true);
      this.compositeLayer = null;
    }
    if (!c) return;
    const provider = SingleTileImageryProvider.fromUrl(c.canvas.toDataURL("image/png"), {
      rectangle: Rectangle.fromDegrees(c.west, c.south, c.east, c.north),
    });
    this.compositeLayer = ImageryLayer.fromProviderAsync(provider, { alpha });
    this.viewer.imageryLayers.add(this.compositeLayer);
  }

  setCompositeAlpha(alpha: number) {
    if (this.compositeLayer) this.compositeLayer.alpha = alpha;
  }

  setExaggeration(v: number) {
    this.viewer.scene.verticalExaggeration = v;
  }

  private pickLatLon(pos: Cartesian2): [number, number] | null {
    const scene = this.viewer.scene;
    const ray = this.viewer.camera.getPickRay(pos);
    let cart = ray ? scene.globe.pick(ray, scene) : undefined;
    if (!cart) cart = this.viewer.camera.pickEllipsoid(pos, scene.globe.ellipsoid);
    if (!cart) return null;
    const c = Cartographic.fromCartesian(cart);
    return [CMath.toDegrees(c.latitude), CMath.toDegrees(c.longitude)];
  }

  pickOnce(cb: (lat: number, lon: number) => void) {
    this.pickHandler = cb;
    document.body.classList.add("picking");
  }

  cancelPick() {
    this.pickHandler = null;
    document.body.classList.remove("picking");
  }

  /** Screen position (CSS px) of a lat/lon, for tooltips. */
  toScreen(lat: number, lon: number): Cartesian2 | undefined {
    return this.viewer.scene.cartesianToCanvasCoordinates(Cartesian3.fromDegrees(lon, lat));
  }

  // Stations -------------------------------------------------------------
  setStations(stations: Station[]) {
    const keep = new Set(stations.map((s) => s.id));
    for (const [id, ent] of this.stationEntities) {
      if (!keep.has(id)) {
        this.viewer.entities.remove(ent);
        this.stationEntities.delete(id);
      }
    }
    for (const s of stations) {
      const existing = this.stationEntities.get(s.id);
      if (existing) this.viewer.entities.remove(existing);
      const ent = this.viewer.entities.add({
        name: s.name,
        position: Cartesian3.fromDegrees(s.longitude, s.latitude, s.height_m),
        properties: { stationId: s.id },
        point: {
          pixelSize: 11,
          color: Color.fromCssColorString("#ffcc33"),
          outlineColor: Color.BLACK,
          outlineWidth: 2,
          heightReference: HeightReference.RELATIVE_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: s.name,
          font: "600 13px system-ui, sans-serif",
          style: LabelStyle.FILL_AND_OUTLINE,
          outlineWidth: 3,
          outlineColor: Color.BLACK,
          fillColor: Color.WHITE,
          verticalOrigin: VerticalOrigin.BOTTOM,
          pixelOffset: new Cartesian2(0, -12),
          heightReference: HeightReference.RELATIVE_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          scaleByDistance: new NearFarScalar(1e3, 1, 2e6, 0.6),
          distanceDisplayCondition: new DistanceDisplayCondition(0, 4e6),
        },
        polyline: {
          positions: Cartesian3.fromDegreesArrayHeights([
            s.longitude, s.latitude, 0, s.longitude, s.latitude, s.height_m,
          ]),
          width: 2,
          material: Color.fromCssColorString("#ffcc33"),
        },
      });
      this.stationEntities.set(s.id, ent);
    }
  }

  flyToStation(s: Station, range = 25_000) {
    this.flyTo(s.latitude, s.longitude, range);
  }

  flyTo(lat: number, lon: number, range = 25_000) {
    this.viewer.camera.flyTo({
      destination: Cartesian3.fromDegrees(lon, lat - range / 111_000 * 0.9, range * 0.7),
      orientation: { heading: 0, pitch: CMath.toRadians(-40), roll: 0 },
      duration: 1.6,
    });
  }

  // Coverage -------------------------------------------------------------
  showCoverage(run: RunSummary, opacity = run.opacity) {
    if (run.north == null || run.south == null || run.east == null || run.west == null || !run.image_url)
      return;
    this.hideCoverage(run.plot_id);
    const provider = SingleTileImageryProvider.fromUrl(run.image_url, {
      rectangle: Rectangle.fromDegrees(run.west, run.south, run.east, run.north),
    });
    const layer = ImageryLayer.fromProviderAsync(provider, { alpha: opacity });
    this.viewer.imageryLayers.add(layer);
    this.coverage.set(run.plot_id, layer);
  }

  hideCoverage(plotId: number) {
    const layer = this.coverage.get(plotId);
    if (layer) {
      this.viewer.imageryLayers.remove(layer, true);
      this.coverage.delete(plotId);
    }
  }

  hasCoverage(plotId: number) {
    return this.coverage.has(plotId);
  }

  setCoverageOpacity(plotId: number, alpha: number) {
    const layer = this.coverage.get(plotId);
    if (layer) layer.alpha = alpha;
  }

  flyToBounds(west: number, south: number, east: number, north: number) {
    this.viewer.camera.flyTo({ destination: Rectangle.fromDegrees(west, south, east, north), duration: 1.6 });
  }

  flyToRun(run: RunSummary) {
    if (run.north == null || run.south == null || run.east == null || run.west == null) return;
    const rect = Rectangle.fromDegrees(run.west, run.south, run.east, run.north);
    this.viewer.camera.flyTo({ destination: rect, duration: 1.6 });
  }

  // Path analysis ----------------------------------------------------------
  showPath(report: PathReport | null) {
    for (const e of this.pathEntities) this.viewer.entities.remove(e);
    this.pathEntities = [];
    if (!report?.tx || !report.rx) return;
    const clear = !!report.line_of_sight_clear;
    this.pathEntities.push(
      this.viewer.entities.add({
        polyline: {
          positions: Cartesian3.fromDegreesArrayHeights([
            report.tx.longitude, report.tx.latitude, report.tx.antenna_amsl,
            report.rx.longitude, report.rx.latitude, report.rx.antenna_amsl,
          ]),
          width: 3,
          material: clear
            ? Color.fromCssColorString("#3ddc84")
            : new PolylineDashMaterialProperty({ color: Color.fromCssColorString("#ff5c5c") }),
        },
      }),
    );
    for (const o of report.obstructions ?? []) {
      this.pathEntities.push(
        this.viewer.entities.add({
          position: Cartesian3.fromDegrees(o.longitude, o.latitude, o.elevation),
          point: {
            pixelSize: 7,
            color: Color.fromCssColorString("#ff5c5c"),
            outlineColor: Color.WHITE,
            outlineWidth: 1,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
        }),
      );
    }
  }

  // Elevation tiles ----------------------------------------------------------
  showTiles(geo: GeoJSON | null) {
    for (const e of this.tileEntities) this.viewer.entities.remove(e);
    this.tileEntities = [];
    if (!geo) return;
    for (const f of geo.features) {
      const ring = f.geometry.coordinates[0];
      const hasData = f.properties.has_data as boolean;
      this.tileEntities.push(
        this.viewer.entities.add({
          rectangle: {
            coordinates: Rectangle.fromDegrees(ring[0][0], ring[0][1], ring[2][0], ring[2][1]),
            material: (hasData ? Color.fromCssColorString("#4da3ff") : Color.fromCssColorString("#888"))
              .withAlpha(0.18),
            outline: true,
            outlineColor: Color.fromCssColorString(hasData ? "#4da3ff" : "#888"),
            height: 0,
          },
        }),
      );
    }
  }

  viewRectangle(): { west: number; south: number; east: number; north: number } | null {
    const rect = this.viewer.camera.computeViewRectangle();
    if (!rect) return null;
    return {
      west: CMath.toDegrees(rect.west),
      south: CMath.toDegrees(rect.south),
      east: CMath.toDegrees(rect.east),
      north: CMath.toDegrees(rect.north),
    };
  }
}
