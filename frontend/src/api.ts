// Typed client for the Signal Server GUI REST API.

export interface Station {
  id: number;
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
export type StationInput = Omit<Station, "id">;

export interface Antenna {
  id: number;
  name: string;
  category: string;
  pattern: string;
  rx_gain_dbd: number;
  description: string;
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
  name: string;
  antenna_id: number;
  tx_station_id: number;
  rx_station_id: number | null;
  frequency_mhz: number;
  erp_watts: number;
  radius_km: number;
  resolution: 300 | 600 | 1200;
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
export type PlotInput = Omit<Plot, "id" | "latest_run">;

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
  stations: () => request<Station[]>("GET", "/api/stations"),
  createStation: (s: StationInput) => request<Station>("POST", "/api/stations", s),
  updateStation: (id: number, s: StationInput) => request<Station>("PUT", `/api/stations/${id}`, s),
  deleteStation: (id: number) => request<void>("DELETE", `/api/stations/${id}`),
  antennas: () => request<Antenna[]>("GET", "/api/antennas"),
  createAntenna: (form: FormData) => request<Antenna>("POST", "/api/antennas", form),
  deleteAntenna: (id: number) => request<void>("DELETE", `/api/antennas/${id}`),
  plots: () => request<Plot[]>("GET", "/api/plots"),
  createPlot: (p: PlotInput) => request<Plot>("POST", "/api/plots", p),
  updatePlot: (id: number, p: PlotInput) => request<Plot>("PUT", `/api/plots/${id}`, p),
  deletePlot: (id: number) => request<void>("DELETE", `/api/plots/${id}`),
  startRun: (plotId: number) => request<RunSummary>("POST", `/api/plots/${plotId}/runs`),
  plotRuns: (plotId: number) => request<RunSummary[]>("GET", `/api/plots/${plotId}/runs`),
  run: (id: number) => request<RunDetail>("GET", `/api/runs/${id}`),
  deleteRun: (id: number) => request<void>("DELETE", `/api/runs/${id}`),
  job: (id: number) => request<Job>("GET", `/api/jobs/${id}`),
  elevationSummary: () =>
    request<{ tiles: number; tiles_with_data: number; archives: number; source: string }>(
      "GET",
      "/api/elevation/summary",
    ),
  elevationTiles: () => request<GeoJSON>("GET", "/api/elevation/tiles.geojson"),
  prefetch: (bbox: { west: number; south: number; east: number; north: number }) =>
    request<Job>("POST", "/api/elevation/prefetch", bbox),
  height: (lat: number, lon: number) =>
    request<{ height_m: number }>("GET", `/api/elevation/height?lat=${lat}&lon=${lon}`),
};

export interface GeoJSON {
  type: "FeatureCollection";
  features: { geometry: { coordinates: number[][][] }; properties: Record<string, unknown> }[];
}

export const fileUrl = (runId: number, name: string, download = false) =>
  `/api/runs/${runId}/files/${encodeURIComponent(name)}${download ? "?download=true" : ""}`;
