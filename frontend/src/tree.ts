// Project explorer: folder tree of stations, plots and imported layers with
// visibility toggles and drag-and-drop moves.
import type { Folder, Layer, Plot, ProjectTree, Station } from "./api";
import { h } from "./dom";

export type NodeKind = "folder" | "station" | "plot" | "layer";
export interface Selection {
  kind: NodeKind;
  id: number;
}

export interface TreeHandlers {
  isVisible: (kind: NodeKind, id: number) => boolean;
  setVisible: (kind: NodeKind, id: number, visible: boolean) => void;
  select: (sel: Selection) => void;
  selected: () => Selection | null;
  move: (kind: "stations" | "plots" | "layers" | "folders", id: number, folderId: number | null) => void;
  folderMenu: (folder: Folder | null, ev: MouseEvent) => void;
  plotStatus: (plot: Plot) => HTMLElement | null;
  collapsed: Set<number>;
  toggleCollapse: (id: number) => void;
}

const ICON: Record<NodeKind, string> = { folder: "📁", station: "📡", plot: "🗺️", layer: "🧭" };

export function renderTree(tree: ProjectTree, hd: TreeHandlers): HTMLElement {
  const root = h("ul", { class: "tree", role: "tree" });
  appendChildren(root, null, tree, hd, 0);
  // Dropping on the empty area moves items to the project root.
  root.addEventListener("dragover", (e) => {
    if (e.target === root) e.preventDefault();
  });
  root.addEventListener("drop", (e) => {
    if (e.target !== root) return;
    e.preventDefault();
    handleDrop(e, null, hd);
  });
  return root;
}

function appendChildren(ul: HTMLElement, folderId: number | null, tree: ProjectTree, hd: TreeHandlers, depth: number) {
  const folders = tree.folders.filter((f) => f.parent_id === folderId);
  for (const f of folders) ul.append(folderNode(f, tree, hd, depth));
  for (const s of tree.stations.filter((x) => x.folder_id === folderId)) ul.append(itemNode("station", s, hd, depth));
  for (const p of tree.plots.filter((x) => x.folder_id === folderId)) ul.append(itemNode("plot", p, hd, depth));
  for (const l of tree.layers.filter((x) => x.folder_id === folderId)) ul.append(itemNode("layer", l, hd, depth));
}

function folderNode(f: Folder, tree: ProjectTree, hd: TreeHandlers, depth: number) {
  const open = !hd.collapsed.has(f.id);
  const sel = hd.selected();
  const li = h("li", { role: "treeitem", "aria-expanded": String(open), class: "node folder" });
  const vis = h("input", {
    type: "checkbox",
    title: "Show/hide everything in this folder",
    checked: hd.isVisible("folder", f.id),
    onclick: (e: Event) => {
      e.stopPropagation();
      hd.setVisible("folder", f.id, (e.target as HTMLInputElement).checked);
    },
  });
  const row = h(
    "div",
    {
      class: `row${sel?.kind === "folder" && sel.id === f.id ? " selected" : ""}`,
      style: `padding-left:${depth * 14 + 4}px`,
      draggable: "true",
      onclick: () => hd.select({ kind: "folder", id: f.id }),
      oncontextmenu: (e: MouseEvent) => {
        e.preventDefault();
        hd.folderMenu(f, e);
      },
    },
    h("button", { class: "twisty", "aria-label": open ? "Collapse" : "Expand", onclick: (e: Event) => { e.stopPropagation(); hd.toggleCollapse(f.id); } }, open ? "▾" : "▸"),
    vis,
    h("span", { class: "icon" }, ICON.folder),
    h("span", { class: "label" }, f.name),
    h("button", { class: "more", title: "Folder actions", onclick: (e: MouseEvent) => { e.stopPropagation(); hd.folderMenu(f, e); } }, "⋯"),
  );
  dragSource(row, "folders", f.id);
  dropTarget(row, f.id, hd);
  li.append(row);
  if (open) {
    const ul = h("ul", { role: "group" });
    appendChildren(ul, f.id, tree, hd, depth + 1);
    li.append(ul);
  }
  return li;
}

function itemNode(kind: "station" | "plot" | "layer", item: Station | Plot | Layer, hd: TreeHandlers, depth: number) {
  const sel = hd.selected();
  const vis = h("input", {
    type: "checkbox",
    title: "Show/hide on the globe",
    checked: hd.isVisible(kind, item.id),
    onclick: (e: Event) => {
      e.stopPropagation();
      hd.setVisible(kind, item.id, (e.target as HTMLInputElement).checked);
    },
  });
  const row = h(
    "div",
    {
      class: `row${sel?.kind === kind && sel.id === item.id ? " selected" : ""}`,
      style: `padding-left:${depth * 14 + 22}px`,
      draggable: "true",
      onclick: () => hd.select({ kind, id: item.id }),
      id: `${kind}-${item.id}`,
    },
    vis,
    h("span", { class: "icon" }, ICON[kind]),
    h("span", { class: "label" }, item.name),
    kind === "plot" ? hd.plotStatus(item as Plot) : null,
  );
  dragSource(row, `${kind}s` as "stations" | "plots" | "layers", item.id);
  return h("li", { role: "treeitem", class: `node ${kind}` }, row);
}

function dragSource(el: HTMLElement, kind: string, id: number) {
  el.addEventListener("dragstart", (e) => {
    e.stopPropagation();
    e.dataTransfer!.setData("application/x-ssg", JSON.stringify({ kind, id }));
    e.dataTransfer!.effectAllowed = "move";
  });
}

function dropTarget(el: HTMLElement, folderId: number, hd: TreeHandlers) {
  el.addEventListener("dragover", (e) => {
    e.preventDefault();
    el.classList.add("drop");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove("drop");
    handleDrop(e, folderId, hd);
  });
}

function handleDrop(e: DragEvent, folderId: number | null, hd: TreeHandlers) {
  const raw = e.dataTransfer?.getData("application/x-ssg");
  if (!raw) return;
  const { kind, id } = JSON.parse(raw) as { kind: "stations" | "plots" | "layers" | "folders"; id: number };
  if (kind === "folders" && id === folderId) return;
  hd.move(kind, id, folderId);
}
