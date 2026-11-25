import "./style.css";
import {
  api,
  fileUrl,
  type Antenna,
  type BasemapPack,
  type ElevationModel,
  type Folder,
  type Layer,
  type Localization,
  type LocalizationInput,
  type Measurement,
  type PatternData,
  type Plot,
  type PlotInput,
  type Project,
  type ProjectTree,
  type RunSummary,
  type Station,
  type ViewerConfig,
} from "./api";
import { OVERLAP_COLORS, PLOT_COLORS, composite, loadValues, valueAt, higherIsBetter, type CompositeMode, type ValueGrid } from "./analysis";
import { chartLegend, profileChart, terrainChart } from "./chart";
import { checkbox, clear, field, formValues, h, num, optNum, select, text, toast } from "./dom";
import { polarChart } from "./polar";
import { Globe } from "./globe";
import { MeasureTool, formatArea, formatDistance, type MeasureMode, type MeasureResult } from "./measure";
import { renderTree, type NodeKind, type Selection } from "./tree";

type Tab = "explorer" | "analyze" | "data";

interface State {
  config: ViewerConfig;
  projects: Project[];
  projectId: number | null;
  tree: ProjectTree | null;
  antennas: Antenna[];
  packs: BasemapPack[];
  tab: Tab;
  selection: Selection | null;
  hidden: Set<string>;
  collapsed: Set<number>;
  compareMode: "individual" | CompositeMode;
  threshold: number | null;
  inspect: boolean;
}

let state: State;
let globe: Globe;
let measure: MeasureTool | null = null;
const polling = new Set<number>();
const grids = new Map<number, ValueGrid>(); // plot id -> loaded values of its latest run
const panel = () => document.getElementById("panel-body")!;

// ---------------------------------------------------------------- bootstrap
async function main() {
  let config: ViewerConfig;
  try {
    config = await api.config();
  } catch (e) {
    document.body.innerHTML = `<p class="fatal">Cannot reach the API: ${(e as Error).message}</p>`;
    return;
  }
  state = {
    config, projects: [], projectId: null, tree: null, antennas: [], packs: [], tab: "explorer",
    selection: null, hidden: new Set(), collapsed: new Set(), compareMode: "individual",
    threshold: null, inspect: true,
  };
  globe = new Globe(document.getElementById("globe")!, config);
  globe.onStationClick = (id) => {
    state.tab = "explorer";
    selectNode({ kind: "station", id });
  };
  const coords = document.getElementById("coords")!;
  globe.onCursor = (lat, lon) => {
    coords.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    scheduleInspect(lat, lon);
  };
  setupChrome();
  const [projects, antennas, packs] = await Promise.all([api.projects(), api.antennas(), api.basemaps()]);
  Object.assign(state, { projects, antennas, packs });
  globe.setOfflinePacks(packs);
  const remembered = Number(store.get("project"));
  const initial = projects.find((p) => p.id === remembered) ?? projects[0];
  if (initial) await openProject(initial.id, true);
  else render();
  if (!config.engine_available)
    toast(`Signal-Server binary not found at "${config.engine_path}". Runs will fail until it is installed.`, "error");
}

const store = {
  get(key: string): string | null {
    try { return localStorage.getItem(`ssg:${key}`); } catch { return null; }
  },
  set(key: string, value: string) {
    try { localStorage.setItem(`ssg:${key}`, value); } catch { /* private mode */ }
  },
};

function setupChrome() {
  document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      state.tab = b.dataset.tab as Tab;
      render();
    }),
  );
  const imagery = document.getElementById("imagery") as HTMLSelectElement;
  for (const i of state.config.imagery) imagery.append(h("option", { value: i.id }, i.name));
  imagery.append(h("option", { value: "offline" }, "Offline streets"), h("option", { value: "offline_sat" }, "Offline satellite"),
    h("option", { value: "none" }, "None"));
  const savedBase = store.get("base");
  if (savedBase) {
    imagery.value = savedBase;
    globe.setImagery(savedBase);
  }
  imagery.addEventListener("change", () => {
    globe.setImagery(imagery.value);
    store.set("base", imagery.value);
    if (imagery.value.startsWith("offline") && !state.packs.some((p) => p.ready && (p.kind === "raster") === (imagery.value === "offline_sat")))
      toast("No offline map packs yet: create one in the Data tab", "error");
  });
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
    const st = state.tree?.stations.find((s) => s.name.toLowerCase().includes(needle));
    if (st) return globe.flyToStation(st);
    const pl = state.tree?.plots.find((p) => p.name.toLowerCase().includes(needle));
    if (pl?.latest_run) return globe.flyToRun(pl.latest_run);
    toast("Enter “lat, lon” or the name of a station or plot");
  });
  document.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) =>
    b.addEventListener("click", () => toggleTool(b.dataset.tool as MeasureMode)),
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      globe.cancelPick();
      if (measure) toggleTool(measure.mode);
      closeMenu();
    }
  });
  document.addEventListener("click", (e) => {
    if (!(e.target as HTMLElement).closest(".menu")) closeMenu();
  });
}

// ---------------------------------------------------------------- project data
async function openProject(id: number, fly = false) {
  state.projectId = id;
  store.set("project", String(id));
  state.selection = null;
  state.hidden = new Set(JSON.parse(store.get(`hidden:${id}`) ?? "[]"));
  state.collapsed = new Set(JSON.parse(store.get(`collapsed:${id}`) ?? "[]"));
  for (const p of state.tree?.plots ?? []) globe.hideCoverage(p.id);
  for (const l of state.tree?.layers ?? []) globe.hideKml(l.id);
  globe.showPath(null);
  grids.clear();
  await refreshTree();
  if (fly) {
    const run = state.tree?.plots.find((p) => p.latest_run?.status === "done")?.latest_run;
    if (run) globe.flyToRun(run);
    else if (state.tree?.stations[0]) globe.flyToStation(state.tree.stations[0], 60_000);
  }
}

async function refreshTree() {
  if (state.projectId == null) return;
  state.tree = await api.tree(state.projectId);
  for (const p of state.tree.plots) if (p.latest_run && isActive(p.latest_run)) poll(p.latest_run.id);
  await applyVisibility();
  render();
}

async function refreshProjects() {
  state.projects = await api.projects();
}

const isActive = (r: RunSummary) => r.status === "queued" || r.status === "running";
const key = (kind: NodeKind, id: number) => `${kind}:${id}`;

function folderHidden(folderId: number | null): boolean {
  let fid = folderId;
  const folders = state.tree?.folders ?? [];
  while (fid != null) {
    if (state.hidden.has(key("folder", fid))) return true;
    fid = folders.find((f) => f.id === fid)?.parent_id ?? null;
  }
  return false;
}

function visible(kind: NodeKind, item: { id: number; folder_id?: number | null }): boolean {
  if (state.hidden.has(key(kind, item.id))) return false;
  return !folderHidden(item.folder_id ?? null);
}

/** Sync globe layers with the tree's checkboxes and the comparison mode. */
async function applyVisibility() {
  const tree = state.tree;
  if (!tree) return;
  store.set(`hidden:${tree.project.id}`, JSON.stringify([...state.hidden]));
  globe.setStations(tree.stations.filter((s) => visible("station", s)));
  const individual = state.compareMode === "individual";
  for (const p of tree.plots) {
    const run = p.latest_run;
    const show = visible("plot", p) && run?.status === "done";
    if (show && individual) {
      if (!globe.hasCoverage(p.id)) globe.showCoverage(run!, p.opacity);
    } else globe.hideCoverage(p.id);
    if (show && run && !grids.has(p.id)) {
      const g = await loadValues(run);
      if (g) grids.set(p.id, g);
    }
  }
  for (const l of tree.layers) {
    if (visible("layer", l)) {
      if (!globe.hasKml(l.id)) globe.showKml(l).catch(() => toast(`Could not display layer “${l.name}”`, "error"));
    } else globe.hideKml(l.id);
  }
  updateComposite();
  renderLegend();
}

function visibleRunGrids(): { plot: Plot; grid: ValueGrid; color: string }[] {
  const tree = state.tree;
  if (!tree) return [];
  const out: { plot: Plot; grid: ValueGrid; color: string }[] = [];
  tree.plots.forEach((p) => {
    const g = grids.get(p.id);
    if (g && g.run.id === p.latest_run?.id && visible("plot", p)) out.push({ plot: p, grid: g, color: plotColor(p) });
  });
  return out;
}

function plotColor(p: Plot): string {
  const idx = state.tree?.plots.findIndex((x) => x.id === p.id) ?? 0;
  return PLOT_COLORS[Math.max(idx, 0) % PLOT_COLORS.length];
}

let lastComposite: ReturnType<typeof composite> = null;
function updateComposite() {
  if (state.compareMode === "individual") {
    globe.showComposite(null);
    lastComposite = null;
    return;
  }
  const items = visibleRunGrids();
  const units = new Set(items.map((i) => i.grid.run.units));
  if (units.size > 1) {
    toast("Compare plots with the same unit (all dBm, all dBµV/m or all path loss)", "error");
  }
  const sameUnit = items.filter((i) => i.grid.run.units === items[0]?.grid.run.units);
  lastComposite = composite(
    sameUnit.map((i) => i.grid),
    state.compareMode,
    state.threshold,
    sameUnit.map((i) => i.color),
    sameUnit[0]?.grid.run.legend ?? [],
  );
  globe.showComposite(lastComposite, 0.8);
}

// ---------------------------------------------------------------- hover inspector
let inspectFrame = 0;
let inspectAt: [number, number] | null = null;
function scheduleInspect(lat: number, lon: number) {
  inspectAt = [lat, lon];
  if (inspectFrame) return;
  inspectFrame = requestAnimationFrame(() => {
    inspectFrame = 0;
    if (inspectAt) inspect(inspectAt[0], inspectAt[1]);
  });
}

function inspect(lat: number, lon: number) {
  const tip = document.getElementById("inspector")!;
  if (!state.inspect || measure) {
    tip.hidden = true;
    return;
  }
  const items = visibleRunGrids()
    .map((i) => ({ ...i, value: valueAt(i.grid, lat, lon) }))
    .filter((i) => i.value != null);
  if (!items.length) {
    tip.hidden = true;
    return;
  }
  const better = higherIsBetter(items[0].grid.run.units);
  items.sort((a, b) => (better ? b.value! - a.value! : a.value! - b.value!));
  clear(tip);
  tip.append(
    h("div", { class: "tip-head" }, `${lat.toFixed(5)}, ${lon.toFixed(5)}`),
    ...items.map((i, n) =>
      h("div", { class: `tip-row${n === 0 && items.length > 1 ? " best" : ""}` },
        h("i", { style: `background:${i.color}` }),
        h("span", { class: "tip-name" }, i.plot.name),
        h("b", {}, `${i.value} ${i.grid.run.units}`),
      ),
    ),
  );
  if (items.length > 1)
    tip.append(h("div", { class: "tip-foot" }, `${items.length} plots cover this point · best: ${items[0].plot.name}`));
  const pos = globe.toScreen(lat, lon);
  if (!pos) return;
  tip.hidden = false;
  const map = document.getElementById("map")!.getBoundingClientRect();
  const x = Math.min(pos.x + 16, map.width - tip.offsetWidth - 8);
  const y = Math.min(pos.y + 16, map.height - tip.offsetHeight - 8);
  tip.style.transform = `translate(${x}px, ${y}px)`;
}

// ---------------------------------------------------------------- rendering
function render() {
  document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)),
  );
  const body = panel();
  const scroll = body.scrollTop;
  clear(body);
  if (state.tab === "explorer") body.append(explorerView());
  if (state.tab === "analyze") body.append(analyzeView());
  if (state.tab === "data") body.append(dataView());
  body.scrollTop = scroll;
  renderLegend();
}

function selectNode(sel: Selection | null) {
  state.selection = sel;
  render();
  if (sel) document.getElementById(`${sel.kind}-${sel.id}`)?.scrollIntoView({ block: "nearest" });
}

// ---------------------------------------------------------------- explorer
function explorerView() {
  const wrap = h("div", { class: "explorer" });
  const projSelect = h(
    "select",
    { "aria-label": "Project", class: "project-select" },
    state.projects.map((p) => h("option", { value: p.id, selected: p.id === state.projectId }, p.name)),
  );
  projSelect.addEventListener("change", () => void openProject(Number(projSelect.value), true));
  wrap.append(
    h("div", { class: "project-bar" },
      state.projects.length ? projSelect : h("span", { class: "meta" }, "No projects yet"),
      h("button", { title: "New project", onclick: newProject }, "+ Project"),
      state.tree ? h("button", { class: "icon-btn", title: "Project actions", onclick: (e: MouseEvent) => projectMenu(e) }, "⋯") : null,
    ),
  );
  const tree = state.tree;
  if (!tree) {
    wrap.append(h("p", { class: "empty" }, "Create a project to organise stations, plots and imported layers in folders."));
    return wrap;
  }
  const folderId = currentFolderId();
  wrap.append(
    h("div", { class: "toolbar-row" },
      h("button", { onclick: () => promptFolder(null, folderId) }, "+ Folder"),
      h("button", { onclick: () => openStationForm(null, folderId) }, "+ Station"),
      h("button", { disabled: !tree.stations.length, onclick: () => openPlotForm(null, folderId) }, "+ Plot"),
      h("button", { onclick: () => openImportForm(folderId) }, "Import KML"),
    ),
  );
  const empty = !tree.folders.length && !tree.stations.length && !tree.plots.length && !tree.layers.length;
  if (empty) wrap.append(h("p", { class: "empty" }, "Empty project. Add a station (click the globe to place it), then a plot."));
  wrap.append(
    renderTree(tree, {
      isVisible: (kind, id) => !state.hidden.has(key(kind, id)),
      setVisible: (kind, id, v) => {
        if (v) state.hidden.delete(key(kind, id));
        else state.hidden.add(key(kind, id));
        void applyVisibility();
      },
      select: (sel) => selectNode(sel),
      selected: () => state.selection,
      move: (kind, id, fid) => void moveItem(kind, id, fid),
      folderMenu: (f, e) => folderMenu(f, e),
      plotStatus: (p) => (p.latest_run ? statusBadge(p.latest_run, true) : null),
      collapsed: state.collapsed,
      toggleCollapse: (id) => {
        if (state.collapsed.has(id)) state.collapsed.delete(id);
        else state.collapsed.add(id);
        store.set(`collapsed:${tree.project.id}`, JSON.stringify([...state.collapsed]));
        render();
      },
    }),
  );
  const details = selectionDetails();
  if (details) wrap.append(details);
  return wrap;
}

function currentFolderId(): number | null {
  const sel = state.selection;
  const tree = state.tree;
  if (!sel || !tree) return null;
  if (sel.kind === "folder") return sel.id;
  const item = sel.kind === "station" ? tree.stations.find((s) => s.id === sel.id)
    : sel.kind === "plot" ? tree.plots.find((p) => p.id === sel.id)
    : tree.layers.find((l) => l.id === sel.id);
  return item?.folder_id ?? null;
}

async function moveItem(kind: "stations" | "plots" | "layers" | "folders", id: number, folderId: number | null) {
  try {
    if (kind === "folders") {
      const f = state.tree!.folders.find((x) => x.id === id)!;
      await api.updateFolder(id, { name: f.name, parent_id: folderId, position: f.position });
    } else await api.move(kind, id, folderId);
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

function selectionDetails(): HTMLElement | null {
  const sel = state.selection;
  const tree = state.tree;
  if (!sel || !tree) return null;
  if (sel.kind === "plot") {
    const p = tree.plots.find((x) => x.id === sel.id);
    return p ? plotCard(p) : null;
  }
  if (sel.kind === "station") {
    const s = tree.stations.find((x) => x.id === sel.id);
    return s ? stationCard(s) : null;
  }
  if (sel.kind === "layer") {
    const l = tree.layers.find((x) => x.id === sel.id);
    return l ? layerCard(l) : null;
  }
  const f = tree.folders.find((x) => x.id === sel.id);
  if (!f) return null;
  const count = (fid: number): number =>
    tree.stations.filter((s) => s.folder_id === fid).length + tree.plots.filter((p) => p.folder_id === fid).length +
    tree.layers.filter((l) => l.folder_id === fid).length +
    tree.folders.filter((x) => x.parent_id === fid).reduce((n, x) => n + count(x.id), 0);
  return h("article", { class: "card details" },
    h("header", {}, h("h3", {}, `📁 ${f.name}`)),
    h("p", { class: "meta" }, `${count(f.id)} item(s). New stations, plots and imports go into the selected folder. Drag items onto a folder to move them.`),
    h("div", { class: "actions" },
      h("button", { onclick: () => promptFolder(null, f.id) }, "+ Sub-folder"),
      h("button", { onclick: () => promptFolder(f, f.parent_id) }, "Rename"),
      h("button", { class: "danger", onclick: () => deleteFolder(f) }, "Delete"),
    ),
  );
}

function plotCard(p: Plot) {
  const run = p.latest_run;
  const tx = state.tree?.stations.find((s) => s.id === p.tx_station_id);
  const card = h("article", { class: "card details", id: `plot-card-${p.id}` });
  card.append(
    h("header", {},
      h("div", {}, h("h3", {}, h("i", { class: "dot", style: `background:${plotColor(p)}` }), p.name),
        h("p", { class: "meta" },
          `${p.frequency_mhz} MHz · ${p.erp_watts} W ERP · ${p.radius_km} km · ${tx?.name ?? "?"} · ${state.config.elevation_models[p.elevation_model] ?? p.elevation_model}`)),
      run ? statusBadge(run) : h("span", { class: "badge" }, "not run"),
    ),
  );
  if (run && isActive(run)) {
    card.append(h("div", { class: "progress" }, h("i", { style: `width:${Math.round(run.progress * 100)}%` })),
      h("p", { class: "meta" }, run.stage || "queued"));
  }
  if (run?.status === "failed" && run.error)
    card.append(h("details", { class: "error" }, h("summary", {}, "Run failed — details"), h("pre", {}, run.error)));
  card.append(
    h("div", { class: "actions" },
      h("button", { class: "primary", disabled: run ? isActive(run) : false, onclick: () => startRun(p) }, run ? "Re-run" : "Run"),
      run?.status === "done" ? h("button", { onclick: () => globe.flyToRun(run) }, "Fly to") : null,
      h("button", { onclick: () => openPlotForm(p, p.folder_id) }, "Edit"),
      h("button", { class: "danger", onclick: () => deletePlot(p) }, "Delete"),
    ),
  );
  if (run?.status === "done") {
    const slider = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: p.opacity, "aria-label": "Coverage opacity" });
    slider.addEventListener("input", () => globe.setCoverageOpacity(p.id, Number(slider.value)));
    card.append(
      h("div", { class: "row" }, h("span", { class: "meta" }, "Opacity"), slider),
      h("div", { class: "downloads" },
        h("button", { class: "link", onclick: () => openInGoogleEarth(p, run) }, "🌍 Open in Google Earth"),
        dl(run, "coverage.kmz", "KMZ"),
        dl(run, "coverage.tif", "GeoTIFF"),
        dl(run, "values.tif", "Values GeoTIFF"),
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

function stationCard(s: Station) {
  return h("article", { class: "card details" },
    h("header", {}, h("div", {}, h("h3", {}, `📡 ${s.name}`),
      h("p", { class: "meta" }, `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)} · ${s.height_m} m AGL · ${s.polarization}`))),
    s.notes ? h("p", { class: "notes" }, s.notes) : null,
    h("div", { class: "actions" },
      h("button", { onclick: () => globe.flyToStation(s) }, "Fly to"),
      h("button", { onclick: () => openStationForm(s, s.folder_id) }, "Edit"),
      h("button", { onclick: () => openPlotForm(null, s.folder_id, s.id) }, "+ Plot from here"),
      h("button", { class: "danger", onclick: () => deleteStation(s) }, "Delete"),
    ),
  );
}

function layerCard(l: Layer) {
  return h("article", { class: "card details" },
    h("header", {}, h("div", {}, h("h3", {}, `🧭 ${l.name}`),
      h("p", { class: "meta" }, `Imported ${l.kind.toUpperCase()} · ${l.feature_count} feature(s)`))),
    h("div", { class: "actions" },
      h("button", { onclick: () => globe.flyToKml(l) }, "Fly to"),
      h("a", { class: "btn", href: l.url, download: "" }, "Download"),
      h("button", { class: "danger", onclick: () => deleteLayer(l) }, "Delete"),
    ),
  );
}

function dl(run: RunSummary, name: string, label: string) {
  if (!run.files.includes(name)) return null;
  return h("a", { href: fileUrl(run.id, name, true), download: "" }, label);
}

function statusBadge(r: RunSummary, small = false) {
  const label = { queued: "queued", running: `${Math.round(r.progress * 100)}%`, done: "ready", failed: "failed" }[r.status];
  return h("span", { class: `badge ${r.status}${small ? " small" : ""}` }, label);
}

function openInGoogleEarth(p: Plot, run: RunSummary) {
  // Google Earth Web can't load a KML from a URL; download it and open Earth at the plot.
  const a = h("a", { href: fileUrl(run.id, "coverage.kmz", true), download: `${p.name}.kmz` });
  document.body.append(a);
  a.click();
  a.remove();
  const lat = ((run.north ?? 0) + (run.south ?? 0)) / 2;
  const lon = ((run.east ?? 0) + (run.west ?? 0)) / 2;
  const range = Math.max(5000, Math.round(p.radius_km * 2600));
  window.open(`https://earth.google.com/web/@${lat.toFixed(5)},${lon.toFixed(5)},0a,${range}d,35y,0h,45t,0r`, "_blank", "noopener");
  toast("KMZ downloaded. In Google Earth: Projects → New project → Import KML file from computer.", "ok");
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

// ---------------------------------------------------------------- menus
function closeMenu() {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
}

function showMenu(e: MouseEvent, items: [string, () => void, boolean?][]) {
  closeMenu();
  e.stopPropagation();
  const menu = h("div", { class: "menu", role: "menu" },
    items.map(([label, fn, danger]) =>
      h("button", { role: "menuitem", class: danger ? "danger" : "", onclick: () => { closeMenu(); fn(); } }, label)),
  );
  document.body.append(menu);
  const x = Math.min(e.clientX, window.innerWidth - menu.offsetWidth - 8);
  const y = Math.min(e.clientY, window.innerHeight - menu.offsetHeight - 8);
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
}

function folderMenu(f: Folder | null, e: MouseEvent) {
  if (!f) return;
  showMenu(e, [
    ["New sub-folder", () => promptFolder(null, f.id)],
    ["New station here", () => openStationForm(null, f.id)],
    ["Import KML here", () => openImportForm(f.id)],
    ["Rename", () => promptFolder(f, f.parent_id)],
    ["Delete folder and contents", () => deleteFolder(f), true],
  ]);
}

function projectMenu(e: MouseEvent) {
  const p = state.tree!.project;
  showMenu(e, [
    ["Rename project", () => renameProject(p)],
    ["Export project as KMZ (Google Earth)", () => window.open(`/api/projects/${p.id}/export.kmz`, "_blank")],
    ["Delete project", () => deleteProject(p), true],
  ]);
}

async function newProject() {
  const name = prompt("Project name");
  if (!name?.trim()) return;
  try {
    const p = await api.createProject({ name: name.trim() });
    await refreshProjects();
    await openProject(p.id);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function renameProject(p: Project) {
  const name = prompt("Project name", p.name);
  if (!name?.trim()) return;
  try {
    await api.updateProject(p.id, { name: name.trim(), description: p.description });
    await refreshProjects();
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function deleteProject(p: Project) {
  if (!confirm(`Delete project “${p.name}” with all its folders, stations, plots and layers?`)) return;
  try {
    await api.deleteProject(p.id);
    for (const pl of state.tree?.plots ?? []) globe.hideCoverage(pl.id);
    for (const l of state.tree?.layers ?? []) globe.hideKml(l.id);
    state.tree = null;
    await refreshProjects();
    if (state.projects[0]) await openProject(state.projects[0].id, true);
    else { state.projectId = null; globe.setStations([]); render(); }
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function promptFolder(f: Folder | null, parentId: number | null) {
  const name = prompt(f ? "Folder name" : "New folder name", f?.name ?? "");
  if (!name?.trim() || !state.projectId) return;
  try {
    if (f) await api.updateFolder(f.id, { name: name.trim(), parent_id: f.parent_id, position: f.position });
    else {
      const nf = await api.createFolder(state.projectId, { name: name.trim(), parent_id: parentId });
      state.selection = { kind: "folder", id: nf.id };
    }
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function deleteFolder(f: Folder) {
  if (!confirm(`Delete folder “${f.name}” and everything inside it?`)) return;
  try {
    const tree = state.tree!;
    await api.deleteFolder(f.id);
    for (const p of tree.plots) globe.hideCoverage(p.id);
    for (const l of tree.layers) globe.hideKml(l.id);
    state.selection = null;
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

// ---------------------------------------------------------------- runs
async function startRun(p: Plot) {
  try {
    const run = await api.startRun(p.id);
    p.latest_run = run;
    grids.delete(p.id);
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
      const plot = state.tree?.plots.find((p) => p.id === run.plot_id);
      if (plot) plot.latest_run = run;
      if (isActive(run)) {
        if (state.tab === "explorer") render();
        window.setTimeout(tick, 1500);
        return;
      }
      polling.delete(runId);
      if (run.status === "done" && plot) {
        state.hidden.delete(key("plot", plot.id));
        globe.hideCoverage(plot.id);
        grids.delete(plot.id);
        await applyVisibility();
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
    grids.delete(p.id);
    state.selection = null;
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function deleteStation(s: Station) {
  if (!confirm(`Delete station “${s.name}”?`)) return;
  try {
    await api.deleteStation(s.id);
    state.selection = null;
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function deleteLayer(l: Layer) {
  if (!confirm(`Delete imported layer “${l.name}”?`)) return;
  try {
    await api.deleteLayer(l.id);
    globe.hideKml(l.id);
    state.selection = null;
    await refreshTree();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

// ---------------------------------------------------------------- forms
function folderOptions(): [string | number, string][] {
  const tree = state.tree;
  if (!tree) return [["", "Project root"]];
  const out: [string | number, string][] = [["", "Project root"]];
  const walk = (parent: number | null, depth: number) => {
    for (const f of tree.folders.filter((x) => x.parent_id === parent)) {
      out.push([f.id, `${"  ".repeat(depth)}📁 ${f.name}`]);
      walk(f.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** Antenna picker + power entry: ERP directly, or transmitter power + antenna gain - cable loss. */
function powerSection(v: Partial<Plot>) {
  const antenna = select("antenna_id", state.antennas.map((a) =>
    [a.id, `${a.name}${a.gain_dbi != null ? ` · ${a.gain_dbi} dBi` : ""}`]), v.antenna_id) as HTMLSelectElement;
  const mode = select("power_mode", [["erp", "Enter ERP"], ["tx", "Transmitter power + antenna gain"]],
    v.tx_power_w != null ? "tx" : "erp") as HTMLSelectElement;
  const erpIn = num("erp_watts", v.erp_watts, { min: 0 });
  const txIn = num("tx_power_w", v.tx_power_w ?? 10, { min: 0 });
  const lossIn = num("cable_loss_db", v.cable_loss_db ?? 0, { min: 0, max: 60, step: 0.1 });
  const out = h("p", { class: "meta erp-out" });
  const gain = () => state.antennas.find((a) => a.id === Number(antenna.value))?.gain_dbi ?? null;
  const computed = () => {
    const g = gain();
    return g == null ? null : Number(txIn.value || 0) * 10 ** ((g - 2.15 - Number(lossIn.value || 0)) / 10);
  };
  const erpRow = field("ERP (W)", erpIn, "Effective radiated power, dipole-referenced, including antenna gain");
  const txRow = h("div", { class: "grid2" },
    field("Transmitter power (W)", txIn),
    field("Cable & connector loss (dB)", lossIn));
  const update = () => {
    const tx = mode.value === "tx";
    erpRow.hidden = tx;
    txRow.hidden = !tx;
    out.hidden = !tx;
    erpIn.required = !tx;
    if (!tx) return;
    const g = gain();
    const e = computed();
    const dbm = 10 * Math.log10(Number(txIn.value || 0) * 1000);
    out.textContent = g == null
      ? "This antenna has no gain set. Set it in Data → Antennas, or enter the ERP directly."
      : `${Number(txIn.value || 0)} W (${dbm.toFixed(1)} dBm) + ${g} dBi − 2.15 − ${Number(lossIn.value || 0)} dB loss → ERP ${e!.toFixed(2)} W (${(10 * Math.log10(e! * 1000)).toFixed(1)} dBm)`;
    out.classList.toggle("warn", g == null);
  };
  for (const el of [mode, antenna, txIn, lossIn]) el.addEventListener("input", update);
  update();
  return {
    antennaField: field("Antenna", antenna, "Manage patterns in Data → Antennas"),
    el: h("div", { class: "power" }, field("Power", mode), erpRow, txRow, out),
    erp: () => (mode.value === "tx" ? computed() ?? Number(erpIn.value || 0) : Number(erpIn.value)),
    txPower: () => (mode.value === "tx" ? Number(txIn.value) : null),
    loss: () => Number(lossIn.value || 0),
  };
}

function openPlotForm(p: Plot | null, folderId: number | null, txId?: number) {
  const c = state.config;
  const tree = state.tree!;
  const stations = tree.stations.map((s) => [s.id, s.name] as [number, string]);
  const v: Partial<Plot> = p ?? {
    frequency_mhz: 446, erp_watts: 10, radius_km: 20, resolution: 1200, rx_height_m: 1.5,
    rx_threshold: -110, propagation_model: 1, use_dbm: true, metric_units: true, opacity: 0.7,
    color_palette: "rainbow", antenna_id: state.antennas[0]?.id, tx_station_id: txId ?? tree.stations[0]?.id,
    elevation_model: "dem3", folder_id: folderId,
  };
  const model = select("elevation_model", Object.entries(c.elevation_models), v.elevation_model);
  const resolution = select("resolution", [[1200, "1200 ppd"], [600, "600 ppd"], [300, "300 ppd (fast)"], [3600, "3600 ppd (30 m, experimental)"]], v.resolution);
  const syncRes = () => {
    const hd = resolution.querySelector<HTMLOptionElement>('option[value="3600"]')!;
    hd.disabled = model.value === "dem3";
    if (hd.disabled && resolution.value === "3600") resolution.value = "1200";
  };
  model.addEventListener("change", syncRes);
  syncRes();
  const power = powerSection(v);
  const form = h("form", { class: "form" },
    h("div", { class: "grid2" },
      field("Name", text("name", v.name ?? "", { required: true, placeholder: "e.g. Hilltop-UHF" })),
      field("Folder", select("folder_id", folderOptions(), v.folder_id ?? folderId ?? "")),
    ),
    h("div", { class: "grid2" },
      field("Transmitter station", select("tx_station_id", stations, v.tx_station_id)),
      field("Receiver station (optional)", select("rx_station_id", [["", "— none —"], ...stations], v.rx_station_id)),
    ),
    power.antennaField,
    h("div", { class: "grid2" },
      field("Frequency (MHz)", num("frequency_mhz", v.frequency_mhz, { min: 20, max: 100000, required: true })),
      field("Radius (km)", num("radius_km", v.radius_km, { min: 0.1, max: 300, required: true })),
    ),
    power.el,
    h("div", { class: "grid3" },
      field("Elevation model", model),
      field("Resolution", resolution),
      field("Model", select("propagation_model", Object.entries(c.propagation_models), v.propagation_model)),
    ),
    h("div", { class: "grid3" },
      field("Rx threshold", num("rx_threshold", v.rx_threshold, { placeholder: "none" }), v.use_dbm ? "dBm" : "dBµV/m"),
      field("Rx height (m)", num("rx_height_m", v.rx_height_m, { min: 0 })),
      field("Palette", select("color_palette", c.palettes.map((x) => [x, x]), v.color_palette)),
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
        field("Opacity", num("opacity", v.opacity, { min: 0, max: 1, step: 0.05 })),
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
      project_id: tree.project.id,
      folder_id: optNum(f.folder_id),
      name: String(f.name),
      antenna_id: Number(f.antenna_id),
      tx_station_id: Number(f.tx_station_id),
      rx_station_id: optNum(f.rx_station_id),
      frequency_mhz: Number(f.frequency_mhz),
      erp_watts: power.erp(),
      tx_power_w: power.txPower(),
      cable_loss_db: power.loss(),
      radius_km: Number(f.radius_km),
      resolution: Number(f.resolution) as PlotInput["resolution"],
      elevation_model: f.elevation_model as ElevationModel,
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
      opacity: Number(f.opacity ?? 0.7),
    };
    try {
      const saved = p ? await api.updatePlot(p.id, body) : await api.createPlot(body);
      closeDialog();
      state.selection = { kind: "plot", id: saved.id };
      await refreshTree();
      if (!p && confirm(`Plot “${saved.name}” created. Run it now?`)) await startRun(saved);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog(p ? `Edit ${p.name}` : "New coverage plot", form);
}

function openStationForm(s: Station | null, folderId: number | null, asNew = false) {
  const editing = s != null && !asNew;
  const v: Partial<Station> = s ?? { height_m: 10, polarization: "vertical", rotation_deg: 0, downtilt_deg: 0, downtilt_direction_deg: 0, notes: "", folder_id: folderId };
  const lat = num("latitude", v.latitude, { min: -70, max: 70, required: true });
  const lon = num("longitude", v.longitude, { min: -180, max: 180, required: true });
  const ground = h("small", { class: "meta" }, "");
  const updateGround = async () => {
    if (!lat.value || !lon.value) return;
    try {
      const g = await api.height(Number(lat.value), Number(lon.value));
      ground.textContent = g.height_m ? `Ground ≈ ${g.height_m} m AMSL` : "Ground height unknown until elevation is downloaded";
    } catch {
      ground.textContent = "";
    }
  };
  lat.addEventListener("change", updateGround);
  lon.addEventListener("change", updateGround);
  const form = h("form", { class: "form" },
    h("div", { class: "grid2" },
      field("Name", text("name", v.name ?? "", { required: true })),
      field("Folder", select("folder_id", folderOptions(), v.folder_id ?? folderId ?? "")),
    ),
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
      h("button", { class: "primary", type: "submit" }, editing ? "Save" : "Create station"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel"),
    ),
  );
  void updateGround();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formValues(form);
    const body = {
      project_id: state.projectId, folder_id: optNum(f.folder_id),
      name: String(f.name), latitude: Number(f.latitude), longitude: Number(f.longitude),
      height_m: Number(f.height_m), polarization: f.polarization as Station["polarization"],
      rotation_deg: Number(f.rotation_deg || 0), downtilt_deg: Number(f.downtilt_deg || 0),
      downtilt_direction_deg: Number(f.downtilt_direction_deg || 0), notes: String(f.notes ?? ""),
    };
    try {
      const saved = editing ? await api.updateStation(s!.id, body) : await api.createStation(body);
      closeDialog();
      state.selection = { kind: "station", id: saved.id };
      await refreshTree();
      globe.flyToStation(saved);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog(editing ? `Edit ${s!.name}` : "New station", form);
}

function openImportForm(folderId: number | null) {
  const form = h("form", { class: "form" },
    field("KML or KMZ file", h("input", { type: "file", name: "file", accept: ".kml,.kmz", required: true })),
    h("div", { class: "grid2" },
      field("Layer name (optional)", text("name", "")),
      field("Folder", select("folder_id", folderOptions(), folderId ?? "")),
    ),
    checkbox("create_stations", false, "Also create stations from the file's points"),
    field("Height for created stations (m AGL)", num("station_height_m", 10, { min: 0 })),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, "Import"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel"),
    ),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = new FormData(form);
    if (!data.get("folder_id")) data.delete("folder_id");
    data.set("create_stations", String((form.elements.namedItem("create_stations") as HTMLInputElement).checked));
    try {
      const res = await api.importLayer(state.projectId!, data);
      closeDialog();
      state.selection = { kind: "layer", id: res.layer.id };
      await refreshTree();
      globe.flyToKml(res.layer);
      const extra = res.stations_created.length ? `, ${res.stations_created.length} station(s) created` : "";
      const skipped = res.stations_skipped.length ? ` (${res.stations_skipped.length} skipped: duplicate name or beyond ±70°)` : "";
      toast(`Imported “${res.layer.name}” (${res.layer.feature_count} features${extra})${skipped}`, "ok");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog("Import KML / KMZ", form);
}

// ---------------------------------------------------------------- analyze tab
function analyzeView() {
  const wrap = analyzeCompareView();
  wrap.append(locatorView());
  return wrap;
}

// ---------------------------------------------------------------- transmitter locator

let activeLoc: number | null = null;

function locatorView() {
  const box = h("div", {}, h("div", { class: "panel-head" }, h("h2", {}, "Locate a transmitter"),
    h("button", { class: "primary small", onclick: () => openLocatorForm(null) }, "+ New")));
  const list = h("article", { class: "card" }, h("p", { class: "meta" }, "Loading…"));
  box.append(list);
  if (state.projectId == null) return box;
  void api.localizations(state.projectId).then((xs) => {

    clear(list);
    if (!xs.length) list.append(h("p", { class: "meta" }, "Enter received power (dBm) at 3 or more places; the app searches for the transmitter position that best explains them, using terrain."));
    for (const l of xs) list.append(locCard(l));
    if (xs.some((l) => l.status === "queued" || l.status === "running")) window.setTimeout(() => { if (state.tab === "analyze") render(); }, 2000);
  });
  return box;
}

function locCard(l: Localization) {
  const r = l.result;
  const busy = l.status === "queued" || l.status === "running";
  return h("div", { class: `loc-row${activeLoc === l.id ? " active" : ""}` },
    h("div", { class: "row" }, h("b", {}, l.name),
      h("span", { class: `badge ${l.status}` }, busy ? `${Math.round(l.progress * 100)}%` : l.status === "done" ? "ready" : l.status)),
    busy ? h("p", { class: "meta" }, l.stage || "queued") : null,
    l.error ? h("p", { class: "meta warn" }, l.error.split("\n")[0]) : null,
    r ? h("dl", { class: "kv" }, [
      ["Position", `${r.latitude.toFixed(5)}, ${r.longitude.toFixed(5)}`],
      ["Fit (RMS)", `${r.rms_db} dB from ${l.measurements.length} measurements`],
      ["Likely area", `${r.likely_area_km2} km² (yellow box)`],
      r.eirp_dbm != null ? ["Est. EIRP", `${r.eirp_dbm} dBm (≈ ${r.erp_w} W ERP)`] : ["Path-loss exponent", `${r.path_loss_exponent}`],
    ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : null,
    h("div", { class: "actions" },
      r ? h("button", { class: "small", onclick: () => { activeLoc = l.id; globe.showLocalization(l); globe.flyTo(r.latitude, r.longitude, 25_000); render(); } }, "Show") : null,
      r ? h("button", { class: "small", onclick: () => openStationAt(r.latitude, r.longitude, l.name) }, "Create station here") : null,
      h("button", { class: "small", onclick: () => openLocatorForm(l) }, "Edit / re-run"),
      h("button", { class: "small danger", onclick: async () => {
        if (!confirm(`Delete “${l.name}”?`)) return;
        await api.deleteLocalization(l.id);
        if (activeLoc === l.id) { activeLoc = null; globe.showLocalization(null); }
        render();
      } }, "Delete")),
  );
}

function openStationAt(lat: number, lon: number, name: string) {
  openStationForm({ id: 0, project_id: state.projectId!, folder_id: null, name: `${name} (estimated)`, latitude: Number(lat.toFixed(6)),
    longitude: Number(lon.toFixed(6)), height_m: 10, polarization: "vertical", rotation_deg: 0, downtilt_deg: 0,
    downtilt_direction_deg: 0, notes: "Estimated from received-power measurements" } as Station, null, true);
}

function openLocatorForm(l: Localization | null) {
  const v: Partial<LocalizationInput> = l ?? { method: "terrain", frequency_mhz: 446, tx_height_m: 10, rx_height_m: 1.5,
    radius_km: 10, elevation_model: "dem3", propagation_model: 1, measurements: [] };
  const rows = h("textarea", { name: "measurements", rows: 6, placeholder: "latitude, longitude, dBm — one per line\n47.36, 9.38, -92\n47.30, 9.36, -117" },
    (v.measurements ?? []).map((m) => `${m.latitude}, ${m.longitude}, ${m.dbm}`).join("\n")) as HTMLTextAreaElement;
  const parse = (): Measurement[] => rows.value.split(/\n+/).map((line) => line.split(/[,;\s]+/).filter(Boolean).map(Number))
    .filter((a) => a.length >= 3 && a.slice(0, 3).every(Number.isFinite))
    .map(([latitude, longitude, dbm]) => ({ latitude, longitude, dbm }));
  const count = h("small", { class: "meta" });
  const sync = () => { const m = parse(); count.textContent = `${m.length} measurement(s)`; globe.showLocalization(null, m); };
  rows.addEventListener("input", sync);
  const form = h("form", { class: "form" },
    h("div", { class: "grid2" },
      field("Name", text("name", l?.name ?? "", { required: true, placeholder: "e.g. Unknown 446 MHz" })),
      field("Method", select("method", [["terrain", "Terrain-aware (Signal-Server)"], ["logdistance", "Distance only (fast)"]], v.method))),
    field("Measurements", rows, "Paste “lat, lon, dBm” lines or add them by clicking the globe"),
    h("div", { class: "row" },
      h("button", { type: "button", onclick: () => {
        document.body.classList.add("dialog-hidden");
        toast("Click where you measured (Esc to cancel)");
        globe.pickOnce((la, lo) => {
          document.body.classList.remove("dialog-hidden");
          const dbm = prompt(`Received power at ${la.toFixed(5)}, ${lo.toFixed(5)} (dBm)`, "-90");
          if (dbm == null || !Number.isFinite(Number(dbm))) return;
          rows.value = `${rows.value.trim()}${rows.value.trim() ? "\n" : ""}${la.toFixed(6)}, ${lo.toFixed(6)}, ${Number(dbm)}`;
          sync();
        });
      } }, "📍 Add point on globe"), count),
    h("div", { class: "grid3" },
      field("Frequency (MHz)", num("frequency_mhz", v.frequency_mhz, { min: 20, max: 100000, required: true })),
      field("Tx height guess (m)", num("tx_height_m", v.tx_height_m, { min: 0 }), "Above ground"),
      field("Rx height (m)", num("rx_height_m", v.rx_height_m, { min: 0 }), "Measuring antenna"),
      field("Search margin (km)", num("radius_km", v.radius_km, { min: 0.5, max: 100 }), "Around the measured area"),
      field("Elevation", select("elevation_model", Object.entries(state.config.elevation_models), v.elevation_model)),
      field("Prop. model", select("propagation_model", Object.entries(state.config.propagation_models), v.propagation_model))),
    h("p", { class: "meta" }, "Works best with 6+ measurements spread around the transmitter. Assumes an omnidirectional transmitter; the result shows a heat map of how well each position fits (green = best) and the likely area."),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, l ? "Save & re-run" : "Locate"),
      h("button", { type: "button", onclick: () => { closeDialog(); globe.showLocalization(null); } }, "Cancel")),
  );
  sync();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = formValues(form);
    const body: LocalizationInput = {
      name: String(f.name), method: f.method as LocalizationInput["method"], frequency_mhz: Number(f.frequency_mhz),
      tx_height_m: Number(f.tx_height_m), rx_height_m: Number(f.rx_height_m), radius_km: Number(f.radius_km),
      elevation_model: f.elevation_model as ElevationModel, propagation_model: Number(f.propagation_model),
      measurements: parse(),
    };
    if (body.measurements.length < 3) return toast("Enter at least 3 measurements", "error");
    try {
      const saved = l ? await api.updateLocalization(l.id, body) : await api.createLocalization(state.projectId!, body);
      closeDialog();
      activeLoc = saved.id;
      state.tab = "analyze";
      render();
      const tick = async () => {
        const x = await api.localization(saved.id);
        if (x.status === "queued" || x.status === "running") { window.setTimeout(tick, 2000); return; }
        if (x.status === "failed") toast(`Locating failed: ${(x.error ?? "").split("\n")[0]}`, "error");
        else if (x.result) { globe.showLocalization(x); globe.flyTo(x.result.latitude, x.result.longitude, 25_000); toast("Transmitter position estimated", "ok"); }
        if (state.tab === "analyze") render();
      };
      void tick();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog(l ? `Edit ${l.name}` : "Locate a transmitter", form);
}

function analyzeCompareView() {
  const items = visibleRunGrids();
  const tree = state.tree;
  const wrap = h("div", {}, h("div", { class: "panel-head" }, h("h2", {}, "Compare coverage")));
  if (!tree) return wrap;
  const modes: [State["compareMode"], string, string][] = [
    ["individual", "Individual layers", "Each plot drawn with its own palette, stacked."],
    ["best", "Best server", "Each pixel shows which plot gives the strongest signal."],
    ["overlap", "Overlap count", "How many plots cover each pixel above the threshold."],
    ["strongest", "Combined coverage", "The strongest signal of any plot, in one palette."],
  ];
  wrap.append(
    h("div", { class: "card" },
      h("div", { class: "seg", role: "radiogroup", "aria-label": "Comparison mode" },
        modes.map(([m, label, hint]) =>
          h("button", { role: "radio", "aria-checked": String(state.compareMode === m), title: hint,
            onclick: () => { state.compareMode = m; void applyVisibility(); render(); } }, label)),
      ),
      h("p", { class: "meta" }, modes.find((m) => m[0] === state.compareMode)![2]),
      state.compareMode !== "individual" ? thresholdControl(items) : null,
      h("label", { class: "check" },
        h("input", { type: "checkbox", checked: state.inspect, onchange: (e: Event) => { state.inspect = (e.target as HTMLInputElement).checked; } }),
        h("span", {}, "Show every plot's value under the cursor")),
    ),
  );
  const list = h("div", { class: "card" }, h("h3", {}, "Plots on the globe"));
  if (!tree.plots.some((p) => p.latest_run?.status === "done"))
    list.append(h("p", { class: "meta" }, "Run some plots first. Tick them in the Explorer to include them here."));
  for (const p of tree.plots) {
    if (p.latest_run?.status !== "done") continue;
    const on = visible("plot", p);
    const won = lastComposite && state.compareMode === "best" ? lastComposite.counts[items.findIndex((i) => i.plot.id === p.id)] : undefined;
    list.append(
      h("label", { class: "check plot-row" },
        h("input", { type: "checkbox", checked: on, onchange: (e: Event) => {
          if ((e.target as HTMLInputElement).checked) state.hidden.delete(key("plot", p.id));
          else state.hidden.add(key("plot", p.id));
          void applyVisibility().then(render);
        } }),
        h("i", { class: "dot", style: `background:${plotColor(p)}` }),
        h("span", {}, p.name),
        h("small", { class: "meta" }, `${p.latest_run.units}${won != null && on ? ` · ${share(won)}` : ""}`),
      ),
    );
  }
  wrap.append(list);
  if (state.compareMode === "overlap" && lastComposite) {
    wrap.append(h("div", { class: "card" }, h("h3", {}, "Overlap"),
      ...OVERLAP_COLORS.slice(0, Math.max(items.length, 1)).map((c, i) =>
        h("div", { class: "swatch" }, h("i", { style: `background:${c}` }), `${i + 1}${i + 1 === OVERLAP_COLORS.length ? "+" : ""} plot(s) · ${share(lastComposite!.counts[i + 1] ?? 0)}`))));
  }
  return wrap;
}

function share(px: number) {
  const total = lastComposite ? lastComposite.counts.reduce((a, b) => a + b, 0) : 0;
  return total ? `${Math.round((px / total) * 100)}% of covered area` : "";
}

function thresholdControl(items: { grid: ValueGrid }[]) {
  const unit = items[0]?.grid.run.units ?? "dBm";
  const input = num("threshold", state.threshold, { placeholder: "none", step: 1 });
  input.addEventListener("change", () => {
    state.threshold = input.value === "" ? null : Number(input.value);
    updateComposite();
    render();
  });
  return field(`Threshold (${unit})`, input, higherIsBetter(unit) ? "Only count signal at or above this level" : "Only count path loss at or below this level");
}

// ---------------------------------------------------------------- data tab
let tilesModel: ElevationModel | null = null;
function dataView() {
  const wrap = h("div", {});
  // Elevation
  const summary = h("div", { class: "meta" }, "Loading…");
  void api.elevationSummary().then((s) => {
    clear(summary);
    for (const [m, info] of Object.entries(s.models)) {
      summary.append(h("div", { class: "model-row" },
        h("b", {}, state.config.elevation_models[m as ElevationModel] ?? m),
        h("span", {}, `${info.tiles} tile(s)${info.buildings ? `, ${info.buildings.toLocaleString()} buildings` : ""}`)));
    }
  });
  const model = select("model", Object.entries(state.config.elevation_models), "dem3") as HTMLSelectElement;
  const jobLine = h("p", { class: "meta" }, "");
  wrap.append(
    h("div", { class: "panel-head" }, h("h2", {}, "Elevation")),
    h("article", { class: "card" },
      h("p", {}, "Plots download what they need automatically. ",
        h("b", {}, "DEM3"), " (Viewfinder Panoramas, 90 m) is bare terrain; ",
        h("b", {}, "Copernicus GLO-30"), " (30 m) is a surface model including forests and large buildings; ",
        h("b", {}, "+ OSM buildings"), " adds OpenStreetMap building heights on top. Pick the model per plot."),
      summary,
      h("div", { class: "row" }, model,
        h("button", { onclick: async () => {
          const r = globe.viewRectangle();
          if (!r) return toast("Zoom in to an area first");
          try {
            const job = await api.prefetch(r, model.value as ElevationModel);
            trackJob(job.id, jobLine, "Elevation ready");
          } catch (e) { toast((e as Error).message, "error"); }
        } }, "Download for view")),
      h("div", { class: "row" },
        h("button", { "aria-pressed": String(tilesModel !== null), onclick: async () => {
          tilesModel = tilesModel ? null : (model.value as ElevationModel);
          globe.showTiles(tilesModel ? await api.elevationTiles(tilesModel) : null);
          render();
        } }, tilesModel ? "Hide tile grid" : "Show downloaded tiles")),
      jobLine,
    ),
  );
  // Offline maps
  const packList = h("div", {});
  for (const p of state.packs) {
    const job = p.job;
    packList.append(h("div", { class: "pack-row" },
      h("div", {}, h("b", {}, p.name),
        h("div", { class: "meta" }, p.ready ? `${p.kind === "raster" ? "Satellite" : "Streets"} · ${p.tile_count.toLocaleString()} tiles · ${(p.size_bytes / 1e6).toFixed(1)} MB · zoom ≤ ${p.max_zoom}`
          : job ? `${job.status} ${job.stage} ${Math.round(job.progress * 100)}%${job.error ? ` — ${job.error}` : ""}` : "pending")),
      h("div", { class: "actions" },
        h("button", { class: "small", onclick: () => globe.flyToBounds(p.west, p.south, p.east, p.north) }, "View"),
        h("button", { class: "small danger", onclick: async () => {
          if (!confirm(`Delete offline map “${p.name}”?`)) return;
          try { await api.deletePack(p.id); await refreshPacks(); } catch (e) { toast((e as Error).message, "error"); }
        } }, "Delete")),
    ));
  }
  const kind = select("kind", [["vector", "Street map"], ["satellite", "Satellite (Sentinel-2, 10 m)"]], "vector") as HTMLSelectElement;
  const zoom = select("max_zoom", [[14, "Detail: zoom 14"], [12, "Towns: zoom 12"], [10, "Regions: zoom 10"]], 14) as HTMLSelectElement;
  const mbtiles = h("input", { type: "file", accept: ".mbtiles", hidden: true }) as HTMLInputElement;
  mbtiles.addEventListener("change", async () => {
    const file = mbtiles.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", file.name.replace(/\.mbtiles$/i, ""));
    try {
      toast("Importing imagery…");
      await api.importPack(fd);
      toast("Imagery imported: choose “Offline satellite” in the Map menu", "ok");
      await refreshPacks();
    } catch (e) { toast((e as Error).message, "error"); }
  });
  const estimate = h("p", { class: "meta" }, "");
  const refreshEstimate = async () => {
    const r = globe.viewRectangle();
    if (!r) return;
    const e = await api.estimatePack(clampRect(r), Number(zoom.value), kind.value);
    estimate.textContent = `Current view: ${e.tiles.toLocaleString()} tiles, about ${e.approx_mb} MB${e.ok ? "" : " — too large, zoom in or lower the detail"}`;
  };
  zoom.addEventListener("change", refreshEstimate);
  kind.addEventListener("change", refreshEstimate);
  void refreshEstimate();
  wrap.append(
    h("div", { class: "panel-head" }, h("h2", {}, "Offline maps")),
    h("article", { class: "card" },
      h("p", { class: "meta" }, "Download street maps (OpenStreetMap via Protomaps) or satellite imagery (Sentinel-2 cloudless by EOX, 10 m, non-commercial licence) plus elevation for an area, then choose “Offline streets” or “Offline satellite” in the Map menu. You can also import your own imagery as .mbtiles."),
      packList,
      h("div", { class: "row" }, kind, zoom),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: async () => {
          const r = globe.viewRectangle();
          if (!r) return toast("Zoom in to an area first");
          const name = prompt("Name for this offline map", "My area");
          if (!name) return;
          try {
            await api.createPack({ name, ...clampRect(r), max_zoom: Number(zoom.value), with_elevation: true, kind: kind.value as "vector" | "satellite" });
            await refreshPacks();
            pollPacks();
          } catch (e) { toast((e as Error).message, "error"); }
        } }, "Download current view"),
        h("button", { onclick: () => mbtiles.click() }, "Import .mbtiles"), mbtiles),
      estimate,
    ),
  );
  wrap.append(antennasView());
  return wrap;
}

function clampRect(r: { west: number; south: number; east: number; north: number }) {
  return { west: Math.max(-180, r.west), south: Math.max(-85, r.south), east: Math.min(180, r.east), north: Math.min(85, r.north) };
}

async function refreshPacks() {
  state.packs = await api.basemaps();
  globe.setOfflinePacks(state.packs);
  if (state.tab === "data") render();
}

let packTimer = 0;
function pollPacks() {
  window.clearTimeout(packTimer);
  const busy = state.packs.some((p) => !p.ready && p.job && (p.job.status === "queued" || p.job.status === "running"));
  if (!busy) return;
  packTimer = window.setTimeout(async () => {
    await refreshPacks();
    const done = state.packs.filter((p) => p.ready);
    if (done.length) globe.setOfflinePacks(state.packs);
    pollPacks();
  }, 2000);
}

function trackJob(id: number, line: HTMLElement, doneMsg: string) {
  const tick = async () => {
    const j = await api.job(id);
    line.textContent = `Job #${j.id}: ${j.status}${j.stage ? ` — ${j.stage}` : ""} ${Math.round(j.progress * 100)}%`;
    if (j.status === "queued" || j.status === "running") window.setTimeout(tick, 1500);
    else if (j.status === "failed") toast(j.error ?? "Job failed", "error");
    else toast(doneMsg, "ok");
  };
  void tick();
}

function antennasView() {
  const fmtNames: Record<string, string> = {
    msi: "MSI/Planet", adf: "NSMA ADF", radiomobile_v1: "Radio Mobile V1", radiomobile_v2: "Radio Mobile V2",
    radiomobile_v3: "Radio Mobile V3", generated_omni: "Generated omni", generated_sector: "Generated sector",
  };
  return h("div", {},
    h("div", { class: "panel-head" }, h("h2", {}, "Antennas"),
      h("div", { class: "actions tight" },
        h("button", { class: "primary small", onclick: openAntennaImport }, "Import pattern"),
        h("button", { class: "small", onclick: openAntennaGenerator }, "Generate"))),
    h("article", { class: "card" },
      state.antennas.map((a) =>
        h("div", { class: "pack-row antenna-row", onclick: () => openAntennaDetails(a) },
          h("div", {},
            h("b", {}, a.name),
            h("div", { class: "meta" }, [
              a.gain_dbi != null ? `${a.gain_dbi} dBi` : "gain not set",
              a.h_beamwidth != null ? `H ${a.h_beamwidth}°` : null,
              a.v_beamwidth != null ? `V ${a.v_beamwidth}°` : null,
              a.electrical_tilt ? `tilt ${a.electrical_tilt}°` : null,
              fmtNames[a.source_format] ?? (a.source_format || a.category),
            ].filter(Boolean).join(" · "))),
          h("button", { class: "small", onclick: (e: Event) => { e.stopPropagation(); openAntennaDetails(a); } }, "View"),
        ),
      ),
      h("p", { class: "meta" }, "Imports MSI/Planet (.msi, .pln, .txt), NSMA (.adf) and Radio Mobile (.ant) files from manufacturers, CloudRF or Ubiquiti, or generates omni/sector patterns."),
    ),
  );
}

async function refreshAntennas() {
  state.antennas = await api.antennas();
  render();
}

function patternView(p: PatternData) {
  const tilt = p.electrical_tilt ?? p.peak_tilt;
  return h("div", { class: "pattern-view" },
    h("div", { class: "polars" },
      polarChart(p.horizontal, "horizontal", 220, "Horizontal (top view)"),
      polarChart(p.vertical, "vertical", 220, "Vertical (side view)")),
    h("dl", { class: "kv" }, [
      ["Gain", p.gain_dbi != null ? `${p.gain_dbi} dBi (${(p.gain_dbi - 2.15).toFixed(2)} dBd)` : "not in file"],
      ["Beamwidth", `H ${p.h_beamwidth ?? "?"}° · V ${p.v_beamwidth ?? "?"}°`],
      ["Front-to-back", p.front_to_back != null ? `${p.front_to_back} dB` : "?"],
      ["Downtilt", tilt != null ? `${tilt}°` : "?"],
      ["Format", p.source_format || "?"],
    ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    ...(p.warnings ?? []).map((w) => h("p", { class: "meta warn" }, `⚠ ${w}`)),
    p.vertical_complete === false && !(p.warnings ?? []).length
      ? h("p", { class: "meta" }, "Only part of the vertical cut is known; the rest is filled in.") : null,
  );
}

async function openAntennaDetails(a: Antenna) {
  const body = h("div", {}, h("p", { class: "meta" }, "Loading pattern…"));
  openDialog(a.name, body);
  try {
    const p = await api.antennaPattern(a.id);
    clear(body);
    const gainIn = num("gain_dbi", a.gain_dbi, { step: 0.01, placeholder: "unknown" });
    body.append(patternView(p));
    if (a.manufacturer)
      body.append(h("p", { class: "meta" }, `Manufacturer: ${a.manufacturer}${a.frequency_mhz ? ` · ${a.frequency_mhz} MHz` : ""}`));
    body.append(
      h("form", { class: "row", onsubmit: async (e: Event) => {
        e.preventDefault();
        try {
          await api.updateAntenna(a.id, { gain_dbi: optNum(gainIn.value) });
          toast("Gain saved", "ok");
          await refreshAntennas();
        } catch (err) { toast((err as Error).message, "error"); }
      } }, field("Gain (dBi)", gainIn, "Used to compute ERP from transmitter power"), h("button", { type: "submit" }, "Save gain")),
      h("div", { class: "actions" },
        h("button", { class: "danger", onclick: async () => {
          if (!confirm(`Delete antenna “${a.name}”?`)) return;
          try { await api.deleteAntenna(a.id); closeDialog(); await refreshAntennas(); }
          catch (e) { toast((e as Error).message, "error"); }
        } }, "Delete"),
        h("button", { onclick: closeDialog }, "Close")),
    );
  } catch (e) {
    body.textContent = (e as Error).message;
  }
}

function openAntennaImport() {
  const fileIn = h("input", { type: "file", name: "file", accept: ".msi,.pln,.txt,.adf,.ant", required: true });
  const flip = h("input", { type: "checkbox", name: "flip_vertical" });
  const name = text("name", "");
  const manufacturer = text("manufacturer", "");
  const gain = num("gain_dbi", null, { step: 0.01, placeholder: "from file" });
  const preview = h("div", { class: "preview" }, h("p", { class: "meta" }, "Choose a file to preview its pattern."));
  const doPreview = async () => {
    const file = fileIn.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("flip_vertical", String(flip.checked));
    try {
      const p = await api.previewAntenna(fd);
      clear(preview);
      preview.append(patternView(p));
      if (!name.value) name.value = p.name ?? "";
      if (!manufacturer.value) manufacturer.value = p.manufacturer ?? "";
      gain.placeholder = p.gain_dbi != null ? `${p.gain_dbi} (from file)` : "not in file: enter it";
    } catch (e) {
      clear(preview);
      preview.append(h("p", { class: "meta warn" }, (e as Error).message));
    }
  };
  fileIn.addEventListener("change", doPreview);
  flip.addEventListener("change", doPreview);
  const form = h("form", { class: "form" },
    field("Pattern file", fileIn, "MSI/Planet (.msi .pln .txt), NSMA (.adf) or Radio Mobile (.ant)"),
    preview,
    h("label", { class: "check" }, flip, h("span", {}, "Flip vertical pattern (if up and down look swapped compared with the datasheet)")),
    h("div", { class: "grid2" }, field("Name", name), field("Manufacturer", manufacturer)),
    h("div", { class: "grid3" },
      field("Gain (dBi)", gain),
      field("Category", text("category", "custom")),
      field("Rx gain (dBd)", num("rx_gain_dbd", 0), "For path analysis")),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, "Save antenna"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel")),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    fd.set("flip_vertical", String(flip.checked));
    if (!gain.value) fd.delete("gain_dbi");
    try {
      const a = await api.createAntenna(fd);
      closeDialog();
      toast(`Antenna “${a.name}” added`, "ok");
      await refreshAntennas();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog("Import antenna pattern", form);
}

function openAntennaGenerator() {
  const kind = select("kind", [["sector", "Sector (panel)"], ["omni", "Omnidirectional"]], "sector") as HTMLSelectElement;
  const hbw = num("h_beamwidth", 65, { min: 1, max: 360 });
  const vbw = num("v_beamwidth", 10, { min: 1, max: 180 });
  const tilt = num("electrical_tilt", 0, { min: -30, max: 30, step: 0.5 });
  const ftb = num("front_to_back", 25, { min: 0, max: 60 });
  const gain = num("gain_dbi", null, { step: 0.1, placeholder: "estimate" });
  const name = text("name", "");
  const preview = h("div", { class: "preview" });
  const values = () => ({
    kind: kind.value as "omni" | "sector", h_beamwidth: Number(hbw.value || 65), v_beamwidth: Number(vbw.value || 10),
    electrical_tilt: Number(tilt.value || 0), front_to_back: Number(ftb.value || 0), gain_dbi: optNum(gain.value),
    name: name.value, category: "generated",
  });
  let timer = 0;
  const doPreview = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      hbw.disabled = kind.value === "omni";
      ftb.disabled = kind.value === "omni";
      try {
        const p = await api.generateAntenna(values(), true);
        clear(preview);
        preview.append(patternView(p));
        gain.placeholder = `${p.gain_dbi} (estimate)`;
      } catch (e) {
        clear(preview);
        preview.append(h("p", { class: "meta warn" }, (e as Error).message));
      }
    }, 200);
  };
  for (const el of [kind, hbw, vbw, tilt, ftb, gain]) el.addEventListener("input", doPreview);
  const form = h("form", { class: "form" },
    h("div", { class: "grid3" },
      field("Type", kind), field("Horizontal beamwidth (°)", hbw), field("Vertical beamwidth (°)", vbw)),
    h("div", { class: "grid3" },
      field("Electrical downtilt (°)", tilt), field("Front-to-back (dB)", ftb), field("Gain (dBi)", gain)),
    preview,
    field("Name", name, "Leave empty for an automatic name"),
    h("p", { class: "meta" }, "3GPP-style pattern: parabolic main lobe, side lobes capped at 20 dB. Gain is estimated from the beamwidths unless you enter it."),
    h("div", { class: "actions" },
      h("button", { class: "primary", type: "submit" }, "Save antenna"),
      h("button", { type: "button", onclick: closeDialog }, "Cancel")),
  );
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const a = await api.generateAntenna(values(), false);
      closeDialog();
      toast(`Antenna “${a.name}” added`, "ok");
      await refreshAntennas();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  });
  openDialog("Generate antenna pattern", form);
  doPreview();
}

// ---------------------------------------------------------------- measuring
function toggleTool(mode: MeasureMode) {
  const panelEl = document.getElementById("measure")!;
  const same = measure?.mode === mode;
  measure?.destroy();
  measure = null;
  document.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) => b.setAttribute("aria-pressed", "false"));
  if (same) {
    panelEl.hidden = true;
    return;
  }
  document.querySelector(`[data-tool="${mode}"]`)?.setAttribute("aria-pressed", "true");
  panelEl.hidden = false;
  let profileFor = "";
  measure = new MeasureTool(globe, mode, (r) => {
    renderMeasure(r);
    if (r.mode === "profile" && r.done && r.points.length === 2) {
      const sig = JSON.stringify(r.points);
      if (sig !== profileFor) {
        profileFor = sig;
        void loadProfile(r.points);
      }
    }
  });
  renderMeasure(measure.result());
}

function renderMeasure(r: MeasureResult) {
  const el = document.getElementById("measure-body")!;
  clear(el);
  const title = { distance: "Distance", area: "Area", profile: "Terrain profile" }[r.mode];
  const hint = r.mode === "profile" ? "Click two points." : "Click to add points; double-click or right-click to finish.";
  el.append(h("div", { class: "measure-head" }, h("b", {}, title),
    h("button", { class: "small", onclick: () => measure?.reset() }, "Clear"),
    h("button", { class: "small", onclick: () => toggleTool(r.mode) }, "Close")));
  if (r.points.length < 1) {
    el.append(h("p", { class: "meta" }, hint));
    return;
  }
  const rows: [string, string][] = [];
  if (r.mode === "area") {
    rows.push(["Area", formatArea(r.area_m2)], ["Perimeter", formatDistance(r.distance_m)]);
  } else {
    rows.push(["Distance", formatDistance(r.distance_m)]);
    if (r.segments_m.length > 1) rows.push(["Segments", r.segments_m.map(formatDistance).join(" + ")]);
    if (r.bearing_deg != null) rows.push(["Bearing", `${r.bearing_deg.toFixed(1)}°`]);
  }
  el.append(h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
  if (!r.done) el.append(h("p", { class: "meta" }, hint));
  const slot = h("div", { id: "profile-slot" });
  if (r.mode === "profile" && r.done && profileView?.sig === JSON.stringify(r.points)) slot.append(...profileView.nodes);
  el.append(slot);
}

let profileView: { sig: string; nodes: Node[] } | null = null;
async function loadProfile(points: [number, number][]) {
  const sig = JSON.stringify(points);
  const show = (nodes: Node[]) => {
    profileView = { sig, nodes };
    const slot = document.getElementById("profile-slot");
    if (slot) { clear(slot); slot.append(...nodes); }
  };
  show([h("p", { class: "meta" }, "Loading terrain…")]);
  try {
    const prof = await api.profile(points, 300);
    const hs = prof.points.map((p) => p[1]);
    show([
      terrainChart(prof.points.map((p) => [p[0] / 1000, p[1]] as [number, number])),
      h("p", { class: "meta" }, `Min ${Math.min(...hs).toFixed(0)} m · max ${Math.max(...hs).toFixed(0)} m · from cached elevation (download tiles in the Data tab if flat)`),
    ]);
  } catch (e) {
    show([h("p", { class: "meta" }, (e as Error).message)]);
  }
}

// ---------------------------------------------------------------- legend + dialog
function renderLegend() {
  const el = document.getElementById("legend")!;
  clear(el);
  if (state.compareMode === "best" && lastComposite) {
    el.hidden = false;
    el.append(h("strong", {}, "Best server"));
    for (const i of visibleRunGrids())
      el.append(h("div", { class: "swatch" }, h("i", { style: `background:${i.color}` }), i.plot.name));
    return;
  }
  if (state.compareMode === "overlap" && lastComposite) {
    el.hidden = false;
    el.append(h("strong", {}, "Plots covering"));
    OVERLAP_COLORS.slice(0, Math.max(visibleRunGrids().length, 1)).forEach((c, i) =>
      el.append(h("div", { class: "swatch" }, h("i", { style: `background:${c}` }), String(i + 1))));
    return;
  }
  const shown = (state.tree?.plots ?? []).filter((p) => (globe.hasCoverage(p.id) || state.compareMode === "strongest") && visible("plot", p) && p.latest_run?.legend.length);
  const plot = shown[shown.length - 1];
  if (!plot?.latest_run) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.append(h("strong", {}, `${state.compareMode === "strongest" ? "Combined" : plot.name} (${plot.latest_run.units})`));
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
