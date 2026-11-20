// Tiny DOM helpers (no framework).

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Handler = (e: any) => unknown;
type Attrs = Record<string, string | number | boolean | Handler | undefined | null>;
type Child = Node | string | number | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v as EventListener);
    } else if (k === "class") {
      el.className = String(v);
    } else if (v === true) {
      el.setAttribute(k, "");
    } else {
      el.setAttribute(k, String(v));
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el: Element) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

let toastTimer: number | undefined;
export function toast(message: string, kind: "info" | "error" | "ok" = "info") {
  const el = document.getElementById("toast")!;
  el.textContent = message;
  el.className = `toast show ${kind}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (el.className = "toast"), kind === "error" ? 7000 : 3500);
}

export function field(
  label: string,
  input: HTMLElement,
  hint?: string,
): HTMLLabelElement {
  return h("label", { class: "field" }, h("span", {}, label), input, hint ? h("small", {}, hint) : null);
}

export function num(
  name: string,
  value: number | null | undefined,
  opts: { step?: number | string; min?: number; max?: number; required?: boolean; placeholder?: string } = {},
) {
  return h("input", {
    type: "number",
    name,
    value: value ?? "",
    step: opts.step ?? "any",
    min: opts.min,
    max: opts.max,
    required: opts.required,
    placeholder: opts.placeholder,
  });
}

export function text(name: string, value: string, opts: { required?: boolean; placeholder?: string } = {}) {
  return h("input", { type: "text", name, value, required: opts.required, placeholder: opts.placeholder });
}

export function select(name: string, options: [string | number, string][], value: unknown) {
  return h(
    "select",
    { name },
    options.map(([v, label]) => h("option", { value: v, selected: String(v) === String(value ?? "") }, label)),
  );
}

export function checkbox(name: string, checked: boolean, label: string) {
  return h("label", { class: "check" }, h("input", { type: "checkbox", name, checked }), h("span", {}, label));
}

export function formValues(form: HTMLFormElement): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const el of Array.from(form.elements) as HTMLInputElement[]) {
    if (!el.name) continue;
    out[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  return out;
}

export const optNum = (v: string | boolean | undefined): number | null =>
  v === undefined || v === "" || typeof v === "boolean" ? null : Number(v);
