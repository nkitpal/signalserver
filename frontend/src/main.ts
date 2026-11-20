import "./style.css";
import { api, fileUrl, type Antenna, type Plot, type PlotInput, type RunSummary, type Station, type ViewerConfig } from "./api";
import { chartLegend, profileChart } from "./chart";
import { checkbox, clear, field, formValues, h, num, optNum, select, text, toast } from "./dom";
import { Globe } from "./globe";

interface State {
  config: ViewerConfig;
  stations: Station[];
  antennas: Antenna[];
  plots: Plot[];
  tab: "plots" | "stations" | "antennas" | "elevation";
  expanded: number | null;
}

let state: State;
let globe: Globe;
const panel = () => document.getElementById("panel-body")!;
const polling = new Set<number>();

async function main() {
  let config: ViewerConfig;
  try {
    config = await api.config();
  } catch (e) {
    document.body.innerHTML = `<p class="fatal">Cannot reach the API: ${(e as Error).message}</p>`;
    return;
  }
  state = { config, stations: [], antennas: [], plots: [], tab: "plots", expanded: null };
  globe = new Globe(document.getElementById("globe")!, config);
  globe.onStationClick = (id) => {
    state.tab = "stations";
    render();
    document.getElementById(`station-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  const coords = document.getElementById("coords")!;
  globe.onCursor = (lat, lon) => (coords.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
  setupChrome();
  await refreshAll();
  if (!config.engine_available)
    toast(`Signal-Server binary not found at "${config.engine_path}". Runs will fail until it is installed.`, "error");
  const withCoverage = state.plots.filter((p) => p.latest_run?.status === "done");
  for (const p of withCoverage) globe.showCoverage(p.latest_run!, p.opacity);
  if (withCoverage.length) globe.flyToRun(withCoverage[0].latest_run!);
  render();
}

function setupChrome() {
  document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      state.tab = b.dataset.tab as State["tab"];
      render();
    }),
  );
  const imagery = document.getElementById("imagery") as HTMLSelectElement;
  for (const i of state.config.imagery) imagery.append(h("option", { value: i.id }, i.name));
  imagery.addEventListener("change", () => globe.setImagery(imagery.value));
  const exag = document.getElementById("exaggeration") as HTMLInputElement;
  exag.addEventListener("input", () => {
    globe.setExaggeration(Number(exag.value));
    document.getElementById("exaggeration-value")!.textContent = `${exag.value}×`;
  });
  document.getElementById("toggle-panel")!.addEventListener("click", () =>
    document.body.classList.toggle("panel-collapsed"),
  );
  const goto = document.getElementById("goto") as HTMLFormElement;
  goto.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = (goto.elements.namedItem("q") as HTMLInputElement).value.trim();
    const m = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$/);
    if (m) return globe.flyTo(Number(m[1]), Number(m[2]), 30_000);
    const needle = q.toLowerCase();
    const st = state.stations.find((s) => s.name.toLowerCase().includes(needle));
    if (st) return globe.flyToStation(st);
    const pl = state.plots.find((p) => p.name.toLowerCase().includes(needle));
    if (pl?.latest_run) return globe.flyToRun(pl.latest_run);
    toast("Enter “lat, lon” or the name of a station or plot");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") globe.cancelPick();
  });
}

async function refreshAll() {
  const [stations, antennas, plots] = await Promise.all([api.stations(), api.antennas(), api.plots()]);
  Object.assign(state, { stations, antennas, plots });
  globe.setStations(stations);
  for (const p of plots) if (p.latest_run && isActive(p.latest_run)) poll(p.latest_run.id);
  render();
}

const isActive = (r: RunSummary) => r.status === "queued" || r.status === "running";

function render() {
  document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)),
  );
  const body = panel();
  clear(body);
  if (state.tab === "plots") body.append(plotsView());
  if (state.tab === "stations") body.append(stationsView());
  if (state.tab === "antennas") body.append(antennasView());
  if (state.tab === "elevation") body.append(elevationView());
  renderLegend();
}

// ---------------------------------------------------------------- Plots
function plotsView() {
  const wrap = h("div", {});
  const ready = state.stations.length > 0 && state.antennas.length > 0;
  wrap.append(
    h("div", { class: "panel-head" },
      h("h2", {}, "Coverage plots"),
      h("button", { class: "primary", disabled: !ready, onclick: () => openPlotForm(null) }, "+ New plot"),
    ),
  );
  if (!ready) wrap.append(h("p", { class: "empty" }, "Add a station first (Stations tab), then create a plot."));
  else if (!state.plots.length) wrap.append(h("p", { class: "empty" }, "No plots yet."));
  for (const p of state.plots) wrap.append(plotCard(p));
  return wrap;
}

function plotCard(p: Plot) {
  const run = p.latest_run;
  const tx = state.stations.find((s) => s.id === p.tx_station_id);
  const visible = globe.hasCoverage(p.id);
  const card = h("article", { class: "card", id: `plot-${p.id}` });
  const status = run ? statusBadge(run) : h("span", { class: "badge" }, "not run");
  card.append(
    h("header", {},
      h("div", {}, h("h3", {}, p.name),
        h("p", { class: "meta" },
          `${p.frequency_mhz} MHz · ${p.erp_watts} W ERP · ${p.radius_km} km · ${tx?.name ?? "?"}`)),
      status,
    ),
  );
  if (run && isActive(run)) {
    card.append(h("div", { class: "progress" }, h("i", { style: `width:${Math.round(run.progress * 100)}%` })),
      h("p", { class: "meta" }, run.stage || "queued"));
  }
  if (run?.status === "failed" && run.error)
    card.append(h("details", { class: "error" }, h("summary", {}, "Run failed — details"), h("pre", {}, run.error)));

  const actions = h("div", { class: "actions" },
    h("button", { class: "primary", disabled: run ? isActive(run) : false, onclick: () => startRun(p) },
      run ? "Re-run" : "Run"),
    run?.status === "done"
      ? h("button", {
          "aria-pressed": String(visible),
          onclick: () => {
            if (globe.hasCoverage(p.id)) globe.hideCoverage(p.id);
            else globe.showCoverage(run, p.opacity);
            render();
          },
        }, visible ? "Hide" : "Show")
      : null,
    run?.status === "done" ? h("button", { onclick: () => { globe.showCoverage(run, p.opacity); globe.flyToRun(run); render(); } }, "Fly to") : null,
    h("button", { onclick: () => openPlotForm(p) }, "Edit"),
    h("button", { class: "danger", onclick: () => deletePlot(p) }, "Delete"),
  );
  card.append(actions);

  if (run?.status === "done") {
    const slider = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: p.opacity });
    slider.addEventListener("input", () => globe.setCoverageOpacity(p.id, Number(slider.value)));
    card.append(
      h("div", { class: "row" }, h("span", { class: "meta" }, "Opacity"), slider),
      h("div", { class: "downloads" },
        dl(run, "coverage.kmz", "Google Earth KMZ"),
        dl(run, "coverage.tif", "GeoTIFF (COG)"),
        dl(run, "coverage.png", "PNG"),
        dl(run, "bundle.zip", "All files"),
      ),
    );
    if (p.do_p2p_analysis) {
      const det = h("details", { class: "p2p" }, h("summary", {}, "Path analysis"));
      det.addEventListener("toggle", async () => {
        if (!det.open || det.dataset.loaded) return;
        det.dataset.loaded = "1";
        const detail = await api.run(run.id);
        globe.showPath(detail.report);
        det.append(p2pSummary(detail.report));
      });
      card.append(det);
    }
  }
  return card;
}

function dl(run: RunSummary, name: string, label: string) {
  if (!run.files.includes(name)) return null;
  return h("a", { href: fileUrl(run.id, name, true), download: "" }, label);
}

function statusBadge(r: RunSummary) {
  const label = { queued: "queued", running: "running", done: "ready", failed: "failed" }[r.status];
  return h("span", { class: `badge ${r.status}` }, label);
}

function p2pSummary(r: import("./api").PathReport | null) {
  if (!r) return h("p", { class: "meta" }, "No path report was produced.");
  const u = r.units ?? { distance: "km", height: "m" };
  const rows: [string, string | undefined][] = [
    ["Distance", r.distance != null ? `${r.distance} ${r.distance_units ?? u.distance}` : undefined],
    ["Received power", r.signal_power_dbm != null ? `${r.signal_power_dbm} dBm` : undefined],
    ["Field strength", r.field_strength_dbuv_m != null ? `${r.field_strength_dbuv_m} dBµV/m` : undefined],
    ["Path loss", r.computed_path_loss_db != null ? `${r.computed_path_loss_db} dB (free space ${r.free_space_path_loss_db} dB)` : undefined],
    ["Line of sight", r.line_of_sight_clear ? "clear" : `obstructed (${r.obstructions.length} points)`],
    ["Rx height to clear path", r.rx_height_to_clear_obstructions != null ? `${r.rx_height_to_clear_obstructions} ${u.height} AGL` : undefined],
    ["Rx height for 60% Fresnel", r.rx_height_to_clear_fresnel60 != null ? `${r.rx_height_to_clear_fresnel60} ${u.height} AGL` : undefined],
  ];
  return h("div", {},
    h("dl", { class: "kv" }, rows.filter(([, v]) => v).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v!)])),
    profileChart(r),
    chartLegend(),
  );
}

async function startRun(p: Plot) {
  try {
    const run = await api.startRun(p.id);
    p.latest_run = run;
    render();
    poll(run.id);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

function poll(runId: number) {
  if (polling.has(runId)) return;
  polling.add(runId);
  const tick = async () => {
    try {
      const run = await api.run(runId);
      const plot = state.plots.find((p) => p.id === run.plot_id);
      if (plot) plot.latest_run = run;
      if (isActive(run)) {
        if (state.tab === "plots") render();
        window.setTimeout(tick, 1500);
        return;
      }
      polling.delete(runId);
      if (run.status === "done" && plot) {
        globe.showCoverage(run, plot.opacity);
        globe.flyToRun(run);
        if (run.report) globe.showPath(run.report);
        toast(`${plot.name}: coverage ready`, "ok");
      } else if (run.status === "failed") {
        toast(`Run failed: ${(run.error ?? "").split("\n")[0]}`, "error");
      }
      render();
    } catch {
      window.setTimeout(tick, 4000);
    }
  };
  void tick();
}

async function deletePlot(p: Plot) {
  if (!confirm(`Delete plot “${p.name}” and all of its runs?`)) return;
  try {
    await api.deletePlot(p.id);
    globe.hideCoverage(p.id);
    await refreshAll();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

function openPlotForm(p: Plot | null) {
  const c = state.config;
  const stations = state.stations.map((s) => [s.id, s.name] as [number, string]);
  const v: Partial<Plot> = p ?? {
    frequency_mhz: 446, erp_watts: 10, radius_km: 20, resolution: 1200, rx_height_m: 1.5,
    rx_threshold: -110, propagation_model: 1, use_dbm: true, metric_units: true, opacity: 0.7,
    color_palette: "rainbow", antenna_id: state.antennas[0]?.id, tx_station_id: state.stations[0]?.id,
  };
  const form = h("form", { class: "form" },
    field("Name", text("name", v.name ?? "", { required: true, placeholder: "e.g. Hilltop-UHF" })),
    h("div", { class: "grid2" },
      field("Transmitter station", select("tx_station_id", stations, v.tx_station_id)),
      field("Receiver station (optional)", select("rx_station_id", [["", "— none —"], ...stations], v.rx_station_id)),
    ),
    field("Antenna", select("antenna_id", state.antennas.map((a) => [a.id, `${a.name} (${a.category})`]), v.antenna_id)),
    h("div", { class: "grid3" },
      field("Frequency (MHz)", num("frequency_mhz", v.frequency_mhz, { min: 20, max: 100000, required: true })),
      field("ERP (W)", num("erp_watts", v.erp_watts, { min: 0, required: true })),
      field("Radius (km)", num("radius_km", v.radius_km, { min: 0.1, max: 300, required: true })),
    ),
    h("div", { class: "grid3" },
      field("Model", select("propagation_model", Object.entries(c.propagation_models), v.propagation_model)),
      field("Resolution", select("resolution", [[1200, "1200 ppd (~90 m)"], [600, "600 ppd"], [300, "300 ppd (fast)"]], v.resolution)),
      field("Rx threshold", num("rx_threshold", v.rx_threshold, { placeholder: "none" }), v.use_dbm ? "dBm" : "dBµV/m"),
    ),
    h("div", { class: "grid3" },
      field("Rx height (m)", num("rx_height_m", v.rx_height_m, { min: 0 })),
      field("Palette", select("color_palette", c.palettes.map((x) => [x, x]), v.color_palette)),
      field("Opacity", num("opacity", v.opacity, { min: 0, max: 1, step: 0.05 })),
    ),
    h("div", { class: "checks" },
      checkbox("use_dbm", v.use_dbm ?? true, "Plot received power (dBm)"),
      checkbox("do_p2p_analysis", v.do_p2p_analysis ?? false, "Path analysis to receiver"),
      checkbox("knife_edge", v.knife_edge ?? false, "Knife-edge diffraction"),
      checkbox("metric_units", v.metric_units ?? true, "Metric report units"),
    ),
    h("details", {}, h("summary", {}, "Advanced propagation settings"),
      h("div", { class: "grid3" },
        field("Environment", select("propagation_mode", [["", "default"], [1, "Urban"], [2, "Suburban"], [3, "Rural"]], v.propagation_mode)),
        field("Terrain", select("terrain_code", [["", "default"], [1, "Water"], [2, "Marsh"], [3, "Farmland"], [4, "Mountain"], [5, "Desert"], [6, "Urban"]], v.terrain_code)),
        field("Climate", select("climate_code", [["", "default"], [1, "Equatorial"], [2, "Continental subtropical"], [3, "Maritime subtropical"], [4, "Desert"], [5, "Continental temperate"], [6, "Maritime temperate (land)"], [7, "Maritime temperate (sea)"]], v.climate_code)),
        field("Dielectric", num("terrain_dielectric", v.terrain_dielectric, { min: 2, max: 80 })),
        field("Conductivity", num("terrain_conductivity", v.terrain_conductivity, { min: 0.0001, max: 0.01 })),
        field("Ground clutter (m)", num("ground_clutter_m", v.ground_clutter_m, { min: 0 })),
        field("Reliability %", num("reliability", v.reliability, { min: 1, max: 99, step: 1 })),
        field("Confidence %", num("confidence", v.confidence, { min: 1, max: 99, step: 1 })),
      ),
    ),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, p ? "Save" : "Create plot"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel"),
    ),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formValues(form);
    const body: PlotInput = {
      name: String(f.name),
      antenna_id: Number(f.antenna_id),
      tx_station_id: Number(f.tx_station_id),
      rx_station_id: optNum(f.rx_station_id),
      frequency_mhz: Number(f.frequency_mhz),
      erp_watts: Number(f.erp_watts),
      radius_km: Number(f.radius_km),
      resolution: Number(f.resolution) as PlotInput["resolution"],
      rx_height_m: Number(f.rx_height_m || 1.5),
      rx_threshold: optNum(f.rx_threshold),
      propagation_model: Number(f.propagation_model),
      propagation_mode: optNum(f.propagation_mode),
      terrain_code: optNum(f.terrain_code),
      terrain_dielectric: optNum(f.terrain_dielectric),
      terrain_conductivity: optNum(f.terrain_conductivity),
      climate_code: optNum(f.climate_code),
      reliability: optNum(f.reliability),
      confidence: optNum(f.confidence),
      ground_clutter_m: optNum(f.ground_clutter_m),
      knife_edge: Boolean(f.knife_edge),
      use_dbm: Boolean(f.use_dbm),
      metric_units: Boolean(f.metric_units),
      do_p2p_analysis: Boolean(f.do_p2p_analysis),
      color_palette: String(f.color_palette),
      opacity: Number(f.opacity),
    };
    try {
      const saved = p ? await api.updatePlot(p.id, body) : await api.createPlot(body);
      closeDialog();
      await refreshAll();
      if (!p && confirm(`Plot “${saved.name}” created. Run it now?`)) await startRun(saved);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog(p ? `Edit ${p.name}` : "New coverage plot", form);
}

// ---------------------------------------------------------------- Stations
function stationsView() {
  const wrap = h("div", {},
    h("div", { class: "panel-head" },
      h("h2", {}, "Stations"),
      h("button", { class: "primary", onclick: () => openStationForm(null) }, "+ New station"),
    ),
  );
  if (!state.stations.length) wrap.append(h("p", { class: "empty" }, "No stations yet. Create one and pick its location on the globe."));
  for (const s of state.stations) {
    wrap.append(
      h("article", { class: "card", id: `station-${s.id}` },
        h("header", {},
          h("div", {}, h("h3", {}, s.name),
            h("p", { class: "meta" }, `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)} · ${s.height_m} m AGL · ${s.polarization}`)),
        ),
        s.notes ? h("p", { class: "notes" }, s.notes) : null,
        h("div", { class: "actions" },
          h("button", { onclick: () => globe.flyToStation(s) }, "Fly to"),
          h("button", { onclick: () => openStationForm(s) }, "Edit"),
          h("button", { class: "danger", onclick: () => deleteStation(s) }, "Delete"),
        ),
      ),
    );
  }
  return wrap;
}

async function deleteStation(s: Station) {
  if (!confirm(`Delete station “${s.name}”?`)) return;
  try {
    await api.deleteStation(s.id);
    await refreshAll();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

function openStationForm(s: Station | null) {
  const v: Partial<Station> = s ?? { height_m: 10, polarization: "vertical", rotation_deg: 0, downtilt_deg: 0, downtilt_direction_deg: 0, notes: "" };
  const lat = num("latitude", v.latitude, { min: -70, max: 70, required: true });
  const lon = num("longitude", v.longitude, { min: -180, max: 180, required: true });
  const ground = h("small", { class: "meta" }, "");
  const updateGround = async () => {
    if (!lat.value || !lon.value) return;
    try {
      const g = await api.height(Number(lat.value), Number(lon.value));
      ground.textContent = g.height_m ? `Ground ≈ ${g.height_m} m AMSL (cached DEM)` : "Ground height unknown until elevation is downloaded";
    } catch {
      ground.textContent = "";
    }
  };
  lat.addEventListener("change", updateGround);
  lon.addEventListener("change", updateGround);
  const form = h("form", { class: "form" },
    field("Name", text("name", v.name ?? "", { required: true })),
    h("div", { class: "grid2" }, field("Latitude", lat), field("Longitude", lon)),
    h("div", { class: "row" },
      h("button", {
        type: "button",
        onclick: () => {
          document.body.classList.add("dialog-hidden");
          toast("Click the globe to place the station (Esc to cancel)");
          globe.pickOnce((la, lo) => {
            lat.value = la.toFixed(6);
            lon.value = lo.toFixed(6);
            document.body.classList.remove("dialog-hidden");
            void updateGround();
          });
        },
      }, "📍 Pick on globe"),
      ground,
    ),
    h("div", { class: "grid3" },
      field("Antenna height (m AGL)", num("height_m", v.height_m, { min: 0, required: true })),
      field("Polarization", select("polarization", [["vertical", "Vertical"], ["horizontal", "Horizontal"]], v.polarization)),
      field("Rotation (°)", num("rotation_deg", v.rotation_deg, { min: 0, max: 359.9 })),
      field("Downtilt (°)", num("downtilt_deg", v.downtilt_deg, { min: -10, max: 90 })),
      field("Downtilt dir (°)", num("downtilt_direction_deg", v.downtilt_direction_deg, { min: 0, max: 359.9 })),
    ),
    field("Notes", h("textarea", { name: "notes", rows: 2 }, v.notes ?? "")),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, s ? "Save" : "Create station"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel"),
    ),
  );
  void updateGround();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formValues(form);
    const body = {
      name: String(f.name), latitude: Number(f.latitude), longitude: Number(f.longitude),
      height_m: Number(f.height_m), polarization: f.polarization as Station["polarization"],
      rotation_deg: Number(f.rotation_deg || 0), downtilt_deg: Number(f.downtilt_deg || 0),
      downtilt_direction_deg: Number(f.downtilt_direction_deg || 0), notes: String(f.notes ?? ""),
    };
    try {
      const saved = s ? await api.updateStation(s.id, body) : await api.createStation(body);
      closeDialog();
      await refreshAll();
      globe.flyToStation(saved);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog(s ? `Edit ${s.name}` : "New station", form);
}

// ---------------------------------------------------------------- Antennas
function antennasView() {
  const form = h("form", { class: "form card" },
    h("h3", {}, "Upload antenna pattern"),
    h("div", { class: "grid2" },
      field("Name", text("name", "", { required: true })),
      field("Category", text("category", "custom")),
    ),
    h("div", { class: "grid2" },
      field("Rx gain (dBd)", num("rx_gain_dbd", 0)),
      field(".ant file", h("input", { type: "file", name: "file", accept: ".ant", required: true })),
    ),
    h("button", { class: "primary", type: "submit" }, "Upload"),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api.createAntenna(new FormData(form));
      toast("Antenna added", "ok");
      await refreshAll();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  return h("div", {},
    h("div", { class: "panel-head" }, h("h2", {}, "Antennas")),
    state.antennas.map((a) =>
      h("article", { class: "card compact" },
        h("header", {},
          h("div", {}, h("h3", {}, a.name), h("p", { class: "meta" }, `${a.category}/${a.pattern} · Rx gain ${a.rx_gain_dbd} dBd`)),
          h("button", { class: "danger small", onclick: async () => {
            if (!confirm(`Delete antenna “${a.name}”?`)) return;
            try { await api.deleteAntenna(a.id); await refreshAll(); } catch (e) { toast((e as Error).message, "error"); }
          } }, "Delete"),
        ),
      ),
    ),
    form,
  );
}

// ---------------------------------------------------------------- Elevation
let tilesShown = false;
function elevationView() {
  const summary = h("p", { class: "meta" }, "Loading…");
  void api.elevationSummary().then((s) => {
    summary.textContent = `${s.tiles} tiles prepared (${s.tiles_with_data} with land data, ${s.tiles - s.tiles_with_data} sea level) from ${s.archives} downloaded archive(s).`;
  });
  const jobLine = h("p", { class: "meta" }, "");
  const toggle = h("button", { "aria-pressed": String(tilesShown), onclick: async () => {
    tilesShown = !tilesShown;
    globe.showTiles(tilesShown ? await api.elevationTiles() : null);
    render();
  } }, tilesShown ? "Hide tile grid" : "Show downloaded tiles");
  const prefetch = h("button", { class: "primary", onclick: async () => {
    const r = globe.viewRectangle();
    if (!r) return toast("Zoom in to an area first");
    try {
      const job = await api.prefetch(r);
      const tick = async () => {
        const j = await api.job(job.id);
        jobLine.textContent = `Download job #${j.id}: ${j.status} ${j.stage ? `— ${j.stage}` : ""} ${Math.round(j.progress * 100)}%`;
        if (j.status === "queued" || j.status === "running") window.setTimeout(tick, 1500);
        else if (j.status === "failed") toast(j.error ?? "Download failed", "error");
        else { toast("Elevation data ready", "ok"); if (tilesShown) globe.showTiles(await api.elevationTiles()); }
      };
      void tick();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  } }, "Download tiles for current view");
  return h("div", {},
    h("div", { class: "panel-head" }, h("h2", {}, "Elevation data")),
    h("article", { class: "card" },
      h("p", {}, "Terrain comes from ",
        h("a", { href: "https://www.viewfinderpanoramas.org/dem3.html", target: "_blank", rel: "noopener" }, "Viewfinder Panoramas DEM3"),
        " (3 arc-second, ~90 m). Tiles are downloaded automatically for each plot, converted to Signal-Server's SDF format and cached. The same data drives the globe's terrain."),
      summary,
      h("div", { class: "actions" }, toggle, prefetch),
      jobLine,
      h("p", { class: "meta" }, state.config.elevation_auto_download ? "Automatic download is on." : "Automatic download is off: only cached tiles are used."),
    ),
  );
}

// ---------------------------------------------------------------- Legend + dialog
function renderLegend() {
  const el = document.getElementById("legend")!;
  clear(el);
  const shown = state.plots.filter((p) => globe.hasCoverage(p.id) && p.latest_run?.legend.length);
  const plot = shown[shown.length - 1];
  if (!plot?.latest_run) { el.hidden = true; return; }
  el.hidden = false;
  el.append(h("strong", {}, `${plot.name} (${plot.latest_run.units})`));
  for (const entry of plot.latest_run.legend)
    el.append(h("div", { class: "swatch" }, h("i", { style: `background:${entry.color}` }), String(entry.value)));
}

function openDialog(title: string, content: HTMLElement) {
  const dlg = document.getElementById("dialog") as HTMLDialogElement;
  clear(dlg);
  dlg.append(h("h2", {}, title), content);
  if (!dlg.open) dlg.show();
  document.body.classList.add("dialog-open");
}

function closeDialog() {
  const dlg = document.getElementById("dialog") as HTMLDialogElement;
  dlg.close();
  document.body.classList.remove("dialog-open", "dialog-hidden");
  globe.cancelPick();
}

void main();
