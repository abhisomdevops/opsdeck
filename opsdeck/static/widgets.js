// Widgets: sandboxed hosts, hot reload, dashboard, gallery, editor and AI builder.
import { $, api, confirmAction, h, markdown, openModal, state, streamSSE, titleCase, toast } from "./core.js";

const hosts = new Set();
const canAuthor = () => state.me && state.me.role !== "viewer";
const isAdmin = () => state.me && state.me.role === "admin";

// ------------------------------------------------------------- hot reload
export function startHotReload() {
  let es;
  const connect = () => {
    es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let ev; try { ev = JSON.parse(e.data); } catch { return; }
      if (!ev.type || !ev.type.startsWith("widget_")) return;
      for (const host of hosts) if (host.id === ev.id) host.hotReload(ev);
      if (state.onWidgetEvent) state.onWidgetEvent(ev);
      if (ev.type === "widget_changed" && ev.reason === "changed") toast(`Widget ${ev.id} reloaded from disk`);
    };
    es.onerror = () => { /* EventSource reconnects by itself (retry: 3000) */ };
  };
  connect();
}

// ------------------------------------------------------------------ host
const q = (params) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === "") continue;
    s.set(k, typeof v === "object" ? JSON.stringify(v) : v);
  }
  return s.toString();
};

function badgeRow(w) {
  const out = [];
  if (w.status === "draft") out.push(h("span", { class: "tag warn", title: "Only visible in the gallery until published" }, "draft"));
  if (w.python === "needs_approval") out.push(h("span", { class: "tag warn", title: "data.py changed and waits for an admin" }, "python: needs approval"));
  if (w.python === "approved") out.push(h("span", { class: "tag ok" }, "python: approved"));
  if (w.errors && w.errors.length) out.push(h("span", { class: "tag crit", title: w.errors.join("\n") }, `${w.errors.length} error(s)`));
  if (w.source === "custom" && w.builtin_available) out.push(h("span", { class: "tag", title: "Edited copy of a built-in widget" }, "customized"));
  return out;
}

export class WidgetHost {
  constructor(summary, { inputs = {}, controls = null, onError = null, onLog = null, onData = null, editorMode = false } = {}) {
    this.w = summary;
    this.id = summary.id;
    this.inputs = { ...Object.fromEntries((summary.inputs || []).map((i) => [i.name, i.default ?? ""])), ...inputs };
    this.onError = onError; this.onLog = onLog; this.onData = onData; this.editorMode = editorMode;
    this.ready = false; this.pendingMsg = null; this.expectLoad = true; this.timer = null; this.lastData = null;
    this.el = h("section", { class: `section widget-host w-${summary.width || "half"}`, "data-widget": summary.id });
    this.title = h("h2", {});
    this.badges = h("span", { class: "tags" });
    this.status = h("span", { class: "count" });
    this.form = h("form", { class: "filters" });
    this.form.addEventListener("submit", (e) => { e.preventDefault(); this.load(); });
    this.menu = h("span", { class: "host-menu" });
    this.controls = controls;
    this.banner = h("div", { class: "host-banner", hidden: true });
    this.frame = h("iframe", { sandbox: "allow-scripts", class: "widget-frame", title: summary.title, loading: "lazy",
      referrerpolicy: "no-referrer" });
    this.frame.style.height = `${summary.height || 320}px`;
    this.frame.addEventListener("load", () => {
      if (!this.expectLoad) { // the widget navigated its own frame: never send it data again
        this.ready = false;
        this.showBanner("This widget tried to navigate away and was stopped.", "crit");
        this.frame.src = "about:blank";
        return;
      }
      this.expectLoad = false;
    });
    this.el.append(h("div", { class: "section-head" }, this.title, this.badges, this.status, h("span", { class: "grow" }),
      this.form, this.menu, controls), this.banner, h("div", { class: "widget-body" }, this.frame));
    this.onMessage = (e) => this.handle(e);
    window.addEventListener("message", this.onMessage);
    hosts.add(this);
    this.drawChrome();
    this.loadFrame();
  }

  drawChrome() {
    const w = this.w;
    this.title.textContent = w.title;
    this.title.title = w.description || "";
    this.badges.replaceChildren(...badgeRow(w));
    this.form.replaceChildren();
    for (const inp of w.inputs || []) {
      const id = `wi-${this.id}-${inp.name}-${Math.random().toString(36).slice(2, 6)}`;
      let el;
      if (inp.type === "boolean") { el = h("input", { type: "checkbox", id }); el.checked = !!this.inputs[inp.name]; }
      else if (inp.type === "select") {
        el = h("select", { id }, (inp.options || []).map((o) => h("option", { value: o }, String(o))));
        el.value = this.inputs[inp.name] ?? "";
      } else { el = h("input", { id, type: inp.type === "integer" ? "number" : "text", placeholder: inp.label || inp.name }); el.value = this.inputs[inp.name] ?? ""; }
      const read = () => (inp.type === "boolean" ? el.checked : el.value);
      el.addEventListener("change", () => { this.inputs[inp.name] = read(); this.load(); });
      this.form.append(h("label", { for: id }, inp.label || titleCase(inp.name), el));
    }
    const refresh = h("button", { class: "btn small ghost", type: "button", title: "Refresh", onclick: () => this.load() }, "↻");
    const items = [refresh];
    if (!this.editorMode) {
      items.push(h("a", { class: "btn small ghost", href: `#/widgets/${this.id}`, title: "Open" }, "⤢"));
      if (canAuthor()) items.push(h("a", { class: "btn small ghost", href: `#/widgets/${this.id}/edit`, title: "Edit" }, "✎"));
    }
    this.menu.replaceChildren(...items);
    clearInterval(this.timer);
    if (w.refresh_secs) this.timer = setInterval(() => { if (!document.hidden) this.load(); }, w.refresh_secs * 1000);
  }

  loadFrame() {
    this.ready = false;
    this.expectLoad = true;
    this.frame.src = `/w/${encodeURIComponent(this.id)}/frame?rev=${this.w.rev}`;
  }

  showBanner(text, cls = "warn", action = null) {
    this.banner.hidden = !text;
    this.banner.className = `host-banner ${cls}`;
    this.banner.replaceChildren(text ? h("span", {}, text) : "", action || "");
  }

  post(msg) {
    if (!this.ready) { this.pendingMsg = msg; return; }
    this.frame.contentWindow.postMessage(msg, "*");
  }

  async load() {
    if (this.w.errors && this.w.errors.length) {
      this.showBanner(`Widget has errors: ${this.w.errors.slice(0, 2).join("; ")}`, "crit");
      if (this.onError) this.onError({ kind: "manifest", message: this.w.errors.join("\n") });
      return;
    }
    this.status.textContent = "loading…";
    try {
      const res = await api(`/api/widgets/${this.id}/data?${q(this.inputs)}`);
      this.lastData = res;
      const errs = Object.entries(res.errors || {});
      const demo = res.demo && res.demo.length ? "demo data" : "";
      this.status.textContent = [demo, `${res.elapsed_ms} ms`].filter(Boolean).join(" · ");
      this.status.title = demo ? `Sample data from: ${res.demo.join(", ")}` : "";
      this.showBanner(errs.length ? errs.map(([k, v]) => `${k}: ${v}`).join(" · ") : "", errs.some(([k]) => k !== "transform") ? "crit" : "warn");
      if (this.onLog) this.onLog(res.logs || []);
      if (this.onData) this.onData(res);
      this.post({ type: "render", data: res.data, inputs: res.inputs, errors: res.errors, demo: res.demo,
        widget: { id: this.id, title: this.w.title } });
    } catch (e) {
      this.status.textContent = "";
      this.showBanner(e.message, "crit");
      if (this.onError) this.onError({ kind: "data", message: e.message });
    }
  }

  async hotReload(ev) {
    if (ev.type === "widget_deleted") { this.showBanner("This widget was deleted.", "crit"); return; }
    try { this.w = await api(`/api/widgets/${this.id}`); } catch { return; }
    this.drawChrome();
    this.loadFrame();
    this.el.classList.add("flash"); setTimeout(() => this.el.classList.remove("flash"), 900);
  }

  async handle(e) {
    if (e.source !== this.frame.contentWindow) return;
    const msg = e.data || {};
    const reply = (ok, data, error) => this.frame.contentWindow.postMessage({ type: "result", id: msg.id, ok, data, error }, "*");
    switch (msg.type) {
      case "ready":
        this.ready = true;
        if (this.pendingMsg) { const m = this.pendingMsg; this.pendingMsg = null; this.post(m); } else this.load();
        break;
      case "height": {
        const hgt = Math.max(120, Math.min(1600, Number(msg.h) || 0));
        this.frame.style.height = `${hgt}px`;
        break;
      }
      case "read":
        try {
          const res = await api(`/api/widgets/${this.id}/ops/${encodeURIComponent(msg.integration)}/${encodeURIComponent(msg.op)}?${q(msg.params)}`);
          reply(true, res.data);
        } catch (err) { reply(false, null, err.message); }
        break;
      case "action":
        try {
          const prepared = await api(`/api/integrations/${encodeURIComponent(msg.integration)}/actions/${encodeURIComponent(msg.op)}/prepare`,
            { body: { params: msg.params || {}, widget: this.id } });
          const result = await confirmAction(prepared);
          reply(true, result);
        } catch (err) { toast(err.message, true); reply(false, null, err.message); }
        break;
      case "ask":
        if (state.ask) state.ask(`${msg.text}\n\n(from widget "${this.w.title}")`);
        break;
      case "refresh": this.load(); break;
      case "inputs": Object.assign(this.inputs, msg.inputs || {}); this.drawChrome(); this.load(); break;
      case "error":
        this.showBanner(`Widget error: ${msg.message}`, "crit", this.editorMode || !canAuthor() ? null
          : h("a", { class: "btn small", href: `#/widgets/${this.id}/edit` }, "Fix"));
        if (this.onError) this.onError({ kind: "runtime", message: msg.message, stack: msg.stack });
        break;
      default: break;
    }
  }

  destroy() {
    clearInterval(this.timer);
    window.removeEventListener("message", this.onMessage);
    hosts.delete(this);
  }
}

function mount(container, summary, opts) {
  const host = new WidgetHost(summary, opts);
  container.append(host.el);
  state.cleanups.push(() => host.destroy());
  return host;
}

// -------------------------------------------------------------- dashboard
export async function renderDashboard() {
  const view = $("view");
  const editBtn = h("button", { class: "btn small", type: "button" }, "Edit layout");
  view.append(h("div", { class: "view-head" }, h("h1", {}, "My dashboard"), h("span", { class: "grow" }), editBtn),
    h("p", { class: "view-sub" }, "Your widgets, live. Edit a widget and it reloads here instantly."));
  const grid = h("div", { class: "wgrid" });
  const bar = h("div", { class: "editbar", hidden: true });
  view.append(bar, grid);
  let [all, dash] = [[], { items: [] }];
  try { [all, dash] = await Promise.all([api("/api/widgets"), api("/api/dashboard")]); } catch (e) {
    grid.append(h("p", { class: "section-msg err" }, e.message)); return;
  }
  const byId = Object.fromEntries(all.map((w) => [w.id, w]));
  let items = dash.items;
  let editing = false;
  const save = async () => { try { items = (await api("/api/dashboard", { method: "PUT", body: { items } })).items; } catch (e) { toast(e.message, true); } };
  const draw = () => {
    state.cleanups.forEach((f) => f()); state.cleanups = [];
    grid.replaceChildren();
    if (!items.length) grid.append(h("div", { class: "all-clear" }, "No widgets yet. ", h("a", { href: "#/widgets" }, "Browse or build one"), "."));
    items.forEach((it, idx) => {
      const w = byId[it.widget]; if (!w) return;
      const controls = editing ? h("span", { class: "layout-ctl" },
        h("button", { class: "btn small ghost", type: "button", title: "Move up", disabled: idx === 0,
          onclick: () => { [items[idx - 1], items[idx]] = [items[idx], items[idx - 1]]; save(); draw(); } }, "↑"),
        h("button", { class: "btn small ghost", type: "button", title: "Move down", disabled: idx === items.length - 1,
          onclick: () => { [items[idx + 1], items[idx]] = [items[idx], items[idx + 1]]; save(); draw(); } }, "↓"),
        h("button", { class: "btn small ghost", type: "button", title: "Change width",
          onclick: () => { it.width = { third: "half", half: "full", full: "third" }[it.width || "half"]; save(); draw(); } }, it.width || "half"),
        h("button", { class: "btn small ghost", type: "button", title: "Remove",
          onclick: () => { items.splice(idx, 1); save(); draw(); } }, "✕")) : null;
      mount(grid, { ...w, width: it.width || w.width }, { inputs: it.inputs || {}, controls });
    });
    bar.hidden = !editing;
    if (editing) {
      const sel = h("select", {}, all.filter((w) => !items.some((i) => i.widget === w.id)).map((w) =>
        h("option", { value: w.id }, `${w.title}${w.status === "draft" ? " (draft)" : ""}`)));
      bar.replaceChildren(h("span", {}, "Add widget"), sel,
        h("button", { class: "btn small primary", type: "button", onclick: () => {
          if (!sel.value) return; items.push({ widget: sel.value, width: byId[sel.value].width || "half" }); save(); draw();
        } }, "Add"), h("span", { class: "grow" }), h("a", { class: "btn small", href: "#/widgets" }, "Build a new widget"));
    }
  };
  editBtn.addEventListener("click", () => { editing = !editing; editBtn.textContent = editing ? "Done" : "Edit layout"; draw(); });
  draw();
}

// ---------------------------------------------------------------- gallery
function builderPanel({ widgetId = null, onSaved = null, placeholder } = {}) {
  const ta = h("textarea", { rows: 3, placeholder: placeholder || "Describe a widget, e.g. \"p99 latency and 5xx rate for checkout-web " +
    "and payments-api side by side, with the firing alerts for those services below\"" });
  const go = h("button", { class: "btn primary", type: "button" }, widgetId ? "Apply with AI" : "Build with AI");
  const log = h("div", { class: "build-log", hidden: true });
  const panel = h("div", { class: "builder" }, h("div", { class: "builder-row" }, ta, go), log);
  const st = state.me.assistant;
  if (!st.available) { ta.disabled = true; go.disabled = true; ta.placeholder = `AI builder unavailable: ${st.detail}`; }
  if (!canAuthor()) { ta.disabled = true; go.disabled = true; ta.placeholder = "Building widgets requires the operator role."; }
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("opsdeck.llm") || "{}"); } catch { /* ignore */ }
  const run = async (prompt) => {
    if (!prompt) return;
    go.disabled = true; log.hidden = false;
    log.replaceChildren(h("div", { class: "muted thinking" }, "Designing widget"));
    let last = null;
    try {
      await streamSSE("/api/widgets/generate/stream", { prompt, widget_id: widgetId, provider: saved.provider || null,
        model: saved.model || null }, (ev) => {
        if (ev.type === "tool") log.append(h("div", { class: "trace" }, ev.tool === "sample_op"
          ? `… sampling ${ev.input.integration}.${ev.input.op}` : "… saving and validating"));
        else if (ev.type === "saved") { last = ev.widget; log.append(h("div", { class: `trace ${ev.ok ? "" : "fail"}` },
          ev.ok ? `✓ saved ${ev.widget.id}` : `✗ ${ev.widget.id} saved with problems, fixing`)); }
        else if (ev.type === "text") log.append(h("div", { class: "msg bot" }, markdown(ev.text)));
        else if (ev.type === "error") log.append(h("div", { class: "msg err" }, ev.detail));
        else if (ev.type === "done" && ev.widget) last = ev.widget;
      });
      log.querySelector(".thinking")?.remove();
      if (last) { ta.value = ""; if (onSaved) onSaved(last); }
    } catch (e) { log.append(h("div", { class: "msg err" }, e.message)); log.querySelector(".thinking")?.remove(); }
    finally { go.disabled = false; }
  };
  go.addEventListener("click", () => run(ta.value.trim()));
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(ta.value.trim()); });
  panel.run = run;
  return panel;
}

export async function renderGallery() {
  const view = $("view");
  view.append(h("div", { class: "view-head" }, h("h1", {}, "Widgets"), h("span", { class: "grow" }),
    canAuthor() ? h("a", { class: "btn small", href: "#/widgets/new" }, "New blank widget") : null),
    h("p", { class: "view-sub" }, "Built-in and team widgets. Describe a new one and the assistant builds it from your connected " +
      "systems; edit any widget's code and it hot-reloads everywhere."));
  view.append(builderPanel({ onSaved: (w) => { location.hash = `#/widgets/${w.id}/edit`; } }));
  const list = h("div", { class: "cards" });
  view.append(list);
  const draw = async () => {
    let items;
    try { items = await api("/api/widgets"); } catch (e) { list.replaceChildren(h("p", { class: "section-msg err" }, e.message)); return; }
    let dash = [];
    try { dash = (await api("/api/dashboard")).items.map((i) => i.widget); } catch { /* ignore */ }
    const groups = {};
    for (const w of items) (groups[w.category || "Custom"] ||= []).push(w);
    list.replaceChildren();
    for (const [cat, ws] of Object.entries(groups).sort()) {
      list.append(h("h2", { class: "sec" }, cat));
      list.append(h("div", { class: "card-grid" }, ws.map((w) => h("div", { class: "wcard" },
        h("div", { class: "wcard-head" }, h("a", { href: `#/widgets/${w.id}` }, h("strong", {}, w.title)), h("span", { class: "grow" }),
          h("span", { class: "tags" }, badgeRow(w), h("span", { class: "tag" }, w.source))),
        h("p", { class: "muted" }, w.description || ""),
        h("div", { class: "wcard-meta" }, `uses ${w.uses.join(", ") || "nothing"}`),
        h("div", { class: "row" },
          h("a", { class: "btn small", href: `#/widgets/${w.id}` }, "Open"),
          canAuthor() ? h("a", { class: "btn small", href: `#/widgets/${w.id}/edit` }, "Edit") : null,
          h("span", { class: "grow" }),
          dash.includes(w.id) ? h("span", { class: "muted small" }, "on dashboard") : h("button", { class: "btn small", type: "button",
            onclick: async () => { const d = (await api("/api/dashboard")).items; d.push({ widget: w.id, width: w.width });
              await api("/api/dashboard", { method: "PUT", body: { items: d } }); toast(`Added ${w.title} to your dashboard`); draw(); } },
          "Add to dashboard"))))));
    }
  };
  state.onWidgetEvent = () => draw();
  state.cleanups.push(() => { state.onWidgetEvent = null; });
  draw();
}

export async function renderWidgetPage(id) {
  const view = $("view");
  let w;
  try { w = await api(`/api/widgets/${id}`); } catch (e) { view.append(h("p", { class: "section-msg err" }, e.message)); return; }
  view.append(h("div", { class: "view-head" }, h("h1", {}, w.title), h("span", { class: "tags" }, badgeRow(w)), h("span", { class: "grow" }),
    canAuthor() ? h("a", { class: "btn small", href: `#/widgets/${id}/edit` }, "Edit") : null),
  h("p", { class: "view-sub" }, w.description || ""));
  const grid = h("div", { class: "wgrid" });
  view.append(grid);
  mount(grid, { ...w, width: "full" }, {});
}

// ----------------------------------------------------------------- editor
const BLANK = {
  manifest: { title: "New widget", description: "", category: "Custom", width: "half", height: 320, refresh_secs: 60,
    inputs: [{ name: "namespace", label: "Namespace", type: "string", default: "" }],
    sources: [{ id: "pods", integration: "kubernetes", op: "pods", params: { namespace: "{{inputs.namespace}}", problems_only: true } }],
    uses: ["kubernetes.pod_logs"] },
  view_js: `function render({ root, data, sdk }) {
  const pods = data.pods || [];
  root.append(sdk.stats([{ label: "Unhealthy pods", value: pods.length, status: pods.length ? "crit" : "ok" }]));
  root.append(sdk.table(pods, {
    columns: [{ key: "namespace" }, { key: "name", fmt: "code" }, { key: "status", fmt: "status" }, { key: "restarts", fmt: "num" }],
    actions: [{ label: "Logs", onClick: async (p) => {
      const res = await sdk.read("kubernetes", "pod_logs", { namespace: p.namespace, pod: p.name, tail: 30 });
      root.append(sdk.h("pre", { class: "od-code" }, res.log));
    } }],
  }));
}
`,
  data_py: null,
};

export async function renderEditor(id) {
  const view = $("view");
  const isNew = !id;
  let w;
  if (isNew) w = { id: "", ...BLANK, python: "none", errors: [], warnings: [], python_errors: [], history: [], source: "custom", status: "draft" };
  else {
    try { w = await api(`/api/widgets/${id}`); } catch (e) { view.append(h("p", { class: "section-msg err" }, e.message)); return; }
  }
  const files = {
    "widget.json": JSON.stringify(Object.fromEntries(Object.entries(w.manifest).filter(([k]) => !["id", "created", "updated",
      "updated_by", "author", "rolled_back_from"].includes(k))), null, 2),
    "view.js": w.view_js || "",
    "data.py": w.data_py || "",
  };
  let tab = "view.js";
  let dirty = false;
  const idInput = h("input", { value: w.id, placeholder: "widget-id", pattern: "[a-z0-9][a-z0-9-]{1,48}", disabled: !isNew, class: "code" });
  const ta = h("textarea", { class: "code-editor", spellcheck: "false", wrap: "off", "aria-label": "Code" });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const problems = h("div", { class: "problems" });
  const logs = h("pre", { class: "log small", hidden: true });
  const saveBtn = h("button", { class: "btn primary", type: "button" }, "Save");
  const dirtyMark = h("span", { class: "muted small" });
  const drawTabs = () => tabs.replaceChildren(...Object.keys(files).map((name) => h("button", {
    type: "button", role: "tab", class: `tab ${tab === name ? "on" : ""}`, "aria-selected": String(tab === name),
    onclick: () => { files[tab] = ta.value; tab = name; ta.value = files[tab]; drawTabs(); } },
  name, name === "data.py" && !files["data.py"].trim() ? h("span", { class: "muted" }, " (optional)") : null)));
  ta.value = files[tab];
  drawTabs();
  ta.addEventListener("input", () => { dirty = true; dirtyMark.textContent = "unsaved changes"; });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab") { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText("  ", s, ta.selectionEnd, "end"); dirty = true; }
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
  });

  const runtimeErrors = [];
  const drawProblems = (res) => {
    const list = [];
    for (const m of res?.errors || w.errors || []) list.push(["crit", m]);
    for (const m of res?.python_errors || w.python_errors || []) list.push(["crit", `data.py: ${m}`]);
    for (const m of res?.warnings || w.warnings || []) list.push(["warn", m]);
    for (const m of runtimeErrors.slice(-3)) list.push(["crit", `runtime: ${m}`]);
    if (w.python === "needs_approval") list.push(["warn", "data.py is waiting for an admin to approve this exact version."]);
    problems.replaceChildren(list.length ? h("ul", {}, list.map(([cls, m]) => h("li", { class: cls }, m)))
      : h("p", { class: "muted small" }, "No problems."));
    if (list.some(([c]) => c === "crit") && canAuthor() && state.me.assistant.available && !isNew) {
      problems.append(h("button", { class: "btn small", type: "button", onclick: () => ai.run(
        `Fix these problems:\n${list.filter(([c]) => c === "crit").map(([, m]) => `- ${m}`).join("\n")}`) }, "Fix with AI"));
    }
  };

  const collect = () => {
    files[tab] = ta.value;
    let manifest;
    try { manifest = JSON.parse(files["widget.json"]); } catch (e) { throw new Error(`widget.json: ${e.message}`); }
    return { manifest, view_js: files["view.js"], data_py: files["data.py"].trim() ? files["data.py"] : null };
  };

  async function save() {
    let body;
    try { body = collect(); } catch (e) { toast(e.message, true); return; }
    const wid = isNew ? idInput.value.trim() : id;
    if (!/^[a-z0-9][a-z0-9-]{1,48}$/.test(wid)) { toast("Set a widget id: lowercase letters, digits and dashes", true); idInput.focus(); return; }
    saveBtn.disabled = true;
    try {
      const res = await api(`/api/widgets/${wid}`, { method: "PUT", body });
      dirty = false; dirtyMark.textContent = "saved";
      runtimeErrors.length = 0;
      if (isNew) { location.hash = `#/widgets/${wid}/edit`; return; }
      w = { ...w, ...res.widget };
      drawProblems(res); drawActions();
    } catch (e) { toast(e.message, true); } finally { saveBtn.disabled = false; }
  }
  saveBtn.addEventListener("click", save);

  const actions = h("div", { class: "row" });
  const drawActions = () => {
    const btns = [saveBtn, dirtyMark, h("span", { class: "grow" })];
    if (!isNew) {
      btns.push(h("button", { class: "btn small", type: "button", onclick: async () => {
        try { await api(`/api/widgets/${id}/status`, { body: { status: w.status === "published" ? "draft" : "published" } });
          w.status = w.status === "published" ? "draft" : "published"; drawActions(); toast(`Widget ${w.status}`);
        } catch (e) { toast(e.message, true); } } }, w.status === "published" ? "Unpublish" : "Publish"));
      if (w.python === "needs_approval" && isAdmin()) btns.push(h("button", { class: "btn small act", type: "button", onclick: approve }, "Approve Python"));
      btns.push(h("button", { class: "btn small", type: "button", onclick: showHistory }, `History (${(w.history || []).length})`));
      if (isAdmin() && w.source === "custom") btns.push(h("button", { class: "btn small", type: "button", onclick: remove },
        w.builtin_available ? "Reset to built-in" : "Delete"));
    }
    actions.replaceChildren(...btns);
  };

  async function approve() {
    const body = h("div", {}, h("p", {}, "Approving lets this exact data.py run on the server with read access to the operations " +
      "this widget declares. Review it first:"), h("pre", { class: "log" }, files["data.py"]));
    const ok = h("button", { class: "btn danger", type: "button" }, "Approve this version");
    const dlg = openModal({ title: `Approve Python for ${id}`, body, actions: [ok] });
    ok.addEventListener("click", async () => {
      try { await api(`/api/widgets/${id}/approve`, { body: {} }); dlg.close(); w.python = "approved"; drawActions(); drawProblems(); toast("Approved"); }
      catch (e) { toast(e.message, true); }
    });
  }
  async function showHistory() {
    const hist = (await api(`/api/widgets/${id}`)).history;
    const body = hist.length ? h("table", {}, h("tbody", {}, hist.map((v) => h("tr", {},
      h("td", { class: "code" }, v.version), h("td", {}, v.by || ""), h("td", {}, v.updated || ""), h("td", {}, v.has_python ? "python" : ""),
      h("td", { class: "acts" }, h("button", { class: "btn small", type: "button", onclick: async () => {
        try { await api(`/api/widgets/${id}/rollback`, { body: { version: v.version } }); $("modal").close(); toast("Rolled back"); route(); }
        catch (e) { toast(e.message, true); } } }, "Restore")))))) : h("p", { class: "muted" }, "No earlier versions.");
    openModal({ title: "Version history", body });
  }
  async function remove() {
    try { await api(`/api/widgets/${id}`, { method: "DELETE", body: {} }); toast("Done"); location.hash = "#/widgets"; }
    catch (e) { toast(e.message, true); }
  }
  const route = () => window.dispatchEvent(new HashChangeEvent("hashchange"));

  // layout
  const preview = h("div", { class: "wgrid preview" });
  const ai = builderPanel({ widgetId: id, placeholder: "Describe a change, e.g. \"add a namespace filter and colour rows by restart count\"",
    onSaved: () => toast("AI change saved; preview reloaded") });
  view.append(
    h("div", { class: "view-head" }, h("h1", {}, isNew ? "New widget" : `Edit: ${w.manifest.title}`),
      h("span", { class: "tags" }, isNew ? null : badgeRow(w)), h("span", { class: "grow" }),
      isNew ? null : h("a", { class: "btn small", href: `#/widgets/${id}` }, "View")),
    h("p", { class: "view-sub" }, "Save (⌘/Ctrl+S) and the preview, plus every dashboard showing this widget, hot-reloads. ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); showGuide(); } }, "Widget guide"), " · ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); showCatalog(); } }, "Available operations")),
    h("div", { class: "editor" },
      h("div", { class: "editor-left" },
        h("div", { class: "row" }, h("label", { class: "muted small" }, "id ", idInput), h("span", { class: "grow" }), tabs),
        ta, actions,
        h("h2", { class: "sec" }, "Problems"), problems,
        h("details", {}, h("summary", { class: "muted small" }, "data.py log"), logs)),
      h("div", { class: "editor-right" },
        isNew ? h("p", { class: "muted" }, "Save once to see the live preview.") : preview,
        isNew ? null : h("h2", { class: "sec" }, "Change it with AI"), isNew ? null : ai)));
  drawActions();
  drawProblems();
  if (!isNew) {
    mount(preview, { ...w, width: "full" }, {
      editorMode: true,
      onError: (err) => { if (err.kind === "runtime") { runtimeErrors.push(err.message); drawProblems(); } },
      onLog: (lines) => { logs.hidden = !lines.length; logs.textContent = lines.join("\n"); },
    });
    // When the files change underneath us (AI builder, disk, another user), refresh the editor text if not dirty.
    state.onWidgetEvent = async (ev) => {
      if (ev.id !== id || ev.type !== "widget_changed") return;
      const fresh = await api(`/api/widgets/${id}`);
      w = fresh;
      if (!dirty) {
        files["widget.json"] = JSON.stringify(Object.fromEntries(Object.entries(fresh.manifest).filter(([k]) => !["id", "created",
          "updated", "updated_by", "author", "rolled_back_from"].includes(k))), null, 2);
        files["view.js"] = fresh.view_js || ""; files["data.py"] = fresh.data_py || "";
        ta.value = files[tab];
      } else toast("This widget changed on the server; your unsaved edits were kept", true);
      runtimeErrors.length = 0;
      drawProblems(); drawActions();
    };
    state.cleanups.push(() => { state.onWidgetEvent = null; });
  }
  const leave = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", leave);
  state.cleanups.push(() => window.removeEventListener("beforeunload", leave));
}

async function showGuide() {
  const { markdown: text } = await api("/api/widgets/guide");
  openModal({ title: "Widget guide", body: h("pre", { class: "log guide" }, text) });
}

async function showCatalog() {
  const ops = await api("/api/widgets/catalog");
  const filter = h("input", { placeholder: "filter, e.g. prometheus", class: "wide" });
  const body = h("div", {});
  const draw = () => {
    const f = filter.value.toLowerCase();
    body.replaceChildren(h("table", {}, h("tbody", {}, ops.filter((o) => !f || o.id.includes(f) || o.description.toLowerCase().includes(f))
      .map((o) => h("tr", {}, h("td", { class: "code" }, o.id), h("td", {}, o.kind === "action" ? h("span", { class: "tag warn" }, "action") : "read"),
        h("td", { class: "code small" }, Object.entries(o.params).map(([n, p]) => `${n}${p.required ? "*" : ""}: ${p.type}`).join(", ")),
        h("td", {}, o.description, o.mode === "disabled" ? h("span", { class: "muted" }, " (not enabled)") : ""))))));
  };
  filter.addEventListener("input", draw);
  draw();
  openModal({ title: "Operations widgets can use", body: h("div", {}, filter, body) });
}
