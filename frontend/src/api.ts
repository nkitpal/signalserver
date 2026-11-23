// Typed client for the Signal Server GUI REST API.

export interface Project {
  id: number;
  name: string;
  description: string;
}

export interface Folder {
  id: number;
  project_id: number;
  parent_id: number | null;
  name: string;
  position: number;
}

export interface Layer {
  id: number;
  project_id: number;
  folder_id: number | null;
  name: string;
  kind: string;
  feature_count: number;
  url: string;
  bounds: [number, number, number, number] | null;
}

export interface ProjectTree {
  project: Project;
  folders: Folder[];
  stations: Station[];
  plots: Plot[];
  layers: Layer[];
}

export interface BasemapPack {
  id: number;
  name: string;
  west: number;
  south: number;
  east: number;
  north: number;
  max_zoom: number;
  tile_count: number;
  size_bytes: number;
  ready: boolean;
  url: string;
  job: { id: number; status: string; stage: string; progress: number; error: string | null } | null;
}

export type ElevationModel = "dem3" | "cop30" | "cop30_buildings";

export interface Station {
  id: number;
  project_id: number;
  folder_id: number | null;
  name: string;
  latitude: number;
  longitude: number;
  height_m: number;
  polarization: "vertical" | "horizontal";
  rotation_deg: number;
  downtilt_deg: number;
  downtilt_direction_deg: number;
  notes: string;
}
export type StationInput = Omit<Station, "id" | "project_id"> & { project_id: number | null };

export interface Antenna {
  id: number;
  name: string;
  category: string;
  pattern: string;
  rx_gain_dbd: number;
  description: string;
  gain_dbi: number | null;
  frequency_mhz: number | null;
  h_beamwidth: number | null;
  v_beamwidth: number | null;
  electrical_tilt: number | null;
  front_to_back: number | null;
  manufacturer: string;
  source_format: string;
}

export interface PatternData {
  horizontal: number[];
  vertical: number[];
  vertical_complete?: boolean;
  name?: string;
  manufacturer?: string;
  gain_dbi: number | null;
  frequency_mhz?: number | null;
  electrical_tilt: number | null;
  source_format: string;
  h_beamwidth: number | null;
  v_beamwidth: number | null;
  front_to_back: number | null;
  peak_tilt?: number;
  warnings?: string[];
}

export interface GenerateInput {
  name?: string;
  category?: string;
  kind: "omni" | "sector";
  h_beamwidth: number;
  v_beamwidth: number;
  gain_dbi: number | null;
  front_to_back: number;
  electrical_tilt: number;
  frequency_mhz?: number | null;
}

export interface LegendEntry {
  value: number;
  color: string;
}

export interface RunSummary {
  id: number;
  plot_id: number;
  plot_name: string | null;
  status: "queued" | "running" | "done" | "failed";
  stage: string;
  progress: number;
  error: string | null;
  north: number | null;
  south: number | null;
  east: number | null;
  west: number | null;
  created_at: string;
  finished_at: string | null;
  files: string[];
  legend: LegendEntry[];
  image_url: string | null;
  values_url: string | null;
  value_top: number | null;
  value_step: number | null;
  opacity: number;
  units: string;
}

export interface PathReport {
  distance?: number;
  distance_units?: string;
  signal_power_dbm?: number;
  field_strength_dbuv_m?: number;
  computed_path_loss_db?: number;
  free_space_path_loss_db?: number;
  terrain_shielding_db?: number;
  line_of_sight_clear?: boolean;
  rx_height_to_clear_obstructions?: number;
  rx_height_to_clear_fresnel60?: number;
  obstructions: { latitude: number; longitude: number; distance: number; elevation: number }[];
  series?: Record<string, [number, number][]>;
  units?: { distance: string; height: string };
  tx?: { latitude: number; longitude: number; antenna_amsl: number };
  rx?: { latitude: number; longitude: number; antenna_amsl: number };
}

export interface RunDetail extends RunSummary {
  params: Record<string, unknown>;
  command: string[];
  log: string;
  report: PathReport | null;
}

export interface Plot {
  id: number;
  project_id: number;
  folder_id: number | null;
  name: string;
  elevation_model: ElevationModel;
  antenna_id: number;
  tx_station_id: number;
  rx_station_id: number | null;
  frequency_mhz: number;
  erp_watts: number;
  tx_power_w: number | null;
  cable_loss_db: number;
  radius_km: number;
  resolution: 300 | 600 | 1200 | 3600;
  rx_height_m: number;
  rx_threshold: number | null;
  propagation_model: number;
  propagation_mode: number | null;
  terrain_code: number | null;
  terrain_dielectric: number | null;
  terrain_conductivity: number | null;
  climate_code: number | null;
  reliability: number | null;
  confidence: number | null;
  ground_clutter_m: number | null;
  knife_edge: boolean;
  use_dbm: boolean;
  metric_units: boolean;
  do_p2p_analysis: boolean;
  color_palette: string;
  opacity: number;
  latest_run: RunSummary | null;
}
export type PlotInput = Omit<Plot, "id" | "latest_run" | "project_id"> & { project_id: number | null };

export interface Job {
  id: number;
  kind: string;
  status: string;
  stage: string;
  progress: number;
  error: string | null;
}

export interface ViewerConfig {
  version: string;
  engine_available: boolean;
  engine_path: string;
  elevation_auto_download: boolean;
  cesium_ion_token: string;
  imagery: { id: string; name: string; url: string; credit: string }[];
  palettes: string[];
  elevation_models: Record<ElevationModel, string>;
  propagation_models: Record<string, string>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") msg = data.detail;
      else if (Array.isArray(data.detail))
        msg = data.detail
          .map((d: { loc?: string[]; msg: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`)
          .join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  config: () => request<ViewerConfig>("GET", "/api/config"),
  projects: () => request<Project[]>("GET", "/api/projects"),
  createProject: (p: { name: string; description?: string }) => request<Project>("POST", "/api/projects", p),
  updateProject: (id: number, p: { name: string; description: string }) =>
    request<Project>("PUT", `/api/projects/${id}`, p),
  deleteProject: (id: number) => request<void>("DELETE", `/api/projects/${id}`),
  tree: (id: number) => request<ProjectTree>("GET", `/api/projects/${id}/tree`),
  createFolder: (projectId: number, f: { name: string; parent_id: number | null }) =>
    request<Folder>("POST", `/api/projects/${projectId}/folders`, f),
  updateFolder: (id: number, f: { name: string; parent_id: number | null; position: number }) =>
    request<Folder>("PUT", `/api/folders/${id}`, f),
  deleteFolder: (id: number) => request<void>("DELETE", `/api/folders/${id}`),
  move: (kind: "stations" | "plots" | "layers", id: number, folderId: number | null) =>
    request<void>("PATCH", `/api/${kind}/${id}/move`, { folder_id: folderId }),
  importLayer: (projectId: number, form: FormData) =>
    request<{ layer: Layer; stations_created: string[]; stations_skipped: string[]; points: number }>(
      "POST",
      `/api/projects/${projectId}/layers`,
      form,
    ),
  deleteLayer: (id: number) => request<void>("DELETE", `/api/layers/${id}`),
  profile: (path: [number, number][], samples = 300) =>
    request<{ distance_m: number; points: [number, number, number, number][] }>(
      "GET",
      `/api/elevation/profile?samples=${samples}&path=${encodeURIComponent(path.map((p) => p.join(",")).join(";"))}`,
    ),
  basemaps: () => request<BasemapPack[]>("GET", "/api/basemaps"),
  estimatePack: (b: { west: number; south: number; east: number; north: number }, maxZoom: number) =>
    request<{ tiles: number; approx_mb: number; limit: number; ok: boolean }>(
      "GET",
      `/api/basemaps/estimate?west=${b.west}&south=${b.south}&east=${b.east}&north=${b.north}&max_zoom=${maxZoom}`,
    ),
  createPack: (p: { name: string; west: number; south: number; east: number; north: number; max_zoom: number; with_elevation: boolean }) =>
    request<BasemapPack>("POST", "/api/basemaps", p),
  deletePack: (id: number) => request<void>("DELETE", `/api/basemaps/${id}`),
  stations: (projectId?: number) =>
    request<Station[]>("GET", `/api/stations${projectId ? `?project_id=${projectId}` : ""}`),
  createStation: (s: StationInput) => request<Station>("POST", "/api/stations", s),
  updateStation: (id: number, s: StationInput) => request<Station>("PUT", `/api/stations/${id}`, s),
  deleteStation: (id: number) => request<void>("DELETE", `/api/stations/${id}`),
  antennas: () => request<Antenna[]>("GET", "/api/antennas"),
  createAntenna: (form: FormData) => request<Antenna>("POST", "/api/antennas", form),
  previewAntenna: (form: FormData) => request<PatternData>("POST", "/api/antennas/preview", form),
  generateAntenna: (g: GenerateInput, preview: boolean) =>
    request<PatternData & Antenna>("POST", `/api/antennas/generate${preview ? "?preview=true" : ""}`, g),
  antennaPattern: (id: number) => request<PatternData>("GET", `/api/antennas/${id}/pattern`),
  updateAntenna: (id: number, a: Partial<Pick<Antenna, "name" | "gain_dbi" | "manufacturer" | "rx_gain_dbd" | "description">>) =>
    request<Antenna>("PATCH", `/api/antennas/${id}`, a),
  deleteAntenna: (id: number) => request<void>("DELETE", `/api/antennas/${id}`),
  plots: (projectId?: number) => request<Plot[]>("GET", `/api/plots${projectId ? `?project_id=${projectId}` : ""}`),
  createPlot: (p: PlotInput) => request<Plot>("POST", "/api/plots", p),
  updatePlot: (id: number, p: PlotInput) => request<Plot>("PUT", `/api/plots/${id}`, p),
  deletePlot: (id: number) => request<void>("DELETE", `/api/plots/${id}`),
  startRun: (plotId: number) => request<RunSummary>("POST", `/api/plots/${plotId}/runs`),
  plotRuns: (plotId: number) => request<RunSummary[]>("GET", `/api/plots/${plotId}/runs`),
  run: (id: number) => request<RunDetail>("GET", `/api/runs/${id}`),
  deleteRun: (id: number) => request<void>("DELETE", `/api/runs/${id}`),
  job: (id: number) => request<Job>("GET", `/api/jobs/${id}`),
  elevationSummary: () =>
    request<{
      tiles: number;
      tiles_with_data: number;
      archives: number;
      source: string;
      models: Record<ElevationModel, { description: string; tiles: number; tiles_with_data: number; buildings: number }>;
    }>("GET", "/api/elevation/summary"),
  elevationTiles: (model: ElevationModel = "dem3") =>
    request<GeoJSON>("GET", `/api/elevation/tiles.geojson?model=${model}`),
  prefetch: (bbox: { west: number; south: number; east: number; north: number }, model: ElevationModel = "dem3") =>
    request<Job>("POST", "/api/elevation/prefetch", { ...bbox, model }),
  height: (lat: number, lon: number) =>
    request<{ height_m: number }>("GET", `/api/elevation/height?lat=${lat}&lon=${lon}`),
};

export interface GeoJSON {
  type: "FeatureCollection";
  features: { geometry: { coordinates: number[][][] }; properties: Record<string, unknown> }[];
}

export const fileUrl = (runId: number, name: string, download = false) =>
  `/api/runs/${runId}/files/${encodeURIComponent(name)}${download ? "?download=true" : ""}`;
