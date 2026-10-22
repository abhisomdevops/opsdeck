// OpsDeck web UI. No build step: plain ES modules, DOM built with textContent
// (never innerHTML with untrusted data) so upstream data can't inject markup.

const state = { me: null, integrations: [], byName: {}, timers: [], chat: [], health: {} };

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const init = { headers: { Accept: "application/json" }, ...opts };
  if (opts.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.headers["X-OpsDeck"] = "1";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const titleCase = (s) => String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : ""), role: err ? "alert" : "status" }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), err ? 9000 : 5000);
}

function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const s = Math.round((Date.now() - t) / 1000);
  const abs = Math.abs(s);
  const unit = abs < 60 ? [abs, "s"] : abs < 3600 ? [Math.round(abs / 60), "m"]
    : abs < 86400 ? [Math.round(abs / 3600), "h"] : [Math.round(abs / 86400), "d"];
  return s >= 0 ? `${unit[0]}${unit[1]} ago` : `in ${unit[0]}${unit[1]}`;
}

const STATUS = {
  ok: ["executed", "ok", "ready", "running", "succeeded", "success", "synced", "healthy", "active", "done", "closed",
       "resolved", "true", "completed", "available", "low", "informational", "normal"],
  warn: ["prepared", "progressing", "pending", "outofsync", "unstable", "warning", "medium", "in progress", "building",
         "insufficient_data", "suspended", "monitoring", "in review", "scheduled", "high", "unknown", "stopped",
         "aborted", "notbuilt", "scaleddown", "to do", "running"],
  crit: ["failed", "failure", "degraded", "error", "crashloopbackoff", "imagepullbackoff", "errimagepull",
         "oomkilled", "alarm", "critical", "highest", "missing", "false", "createcontainerconfigerror", "evicted"],
};
function statusClass(v) {
  const s = String(v ?? "").toLowerCase();
  if (STATUS.crit.includes(s)) return "crit";
  if (s === "running") return "ok";
  if (STATUS.ok.includes(s)) return "ok";
  if (STATUS.warn.includes(s)) return "warn";
  return "";
}

function fmtBytes(n) {
  if (n === null || n === undefined || n === "") return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
}
function fmtDuration(s) {
  s = Number(s || 0); if (!s) return "";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function cell(value, fmt) {
  if (value === null || value === undefined) return h("td", {}, "");
  switch (fmt) {
    case "status": return h("td", {}, h("span", { class: `badge ${statusClass(value)}` }, String(value)));
    case "time": return h("td", { title: String(value), class: "nowrap" }, relTime(value));
    case "link": return h("td", {}, /^https?:\/\//.test(value)
      ? h("a", { href: value, target: "_blank", rel: "noopener noreferrer" }, "Open") : "");
    case "code": return h("td", { class: String(value).length <= 40 ? "code nowrap" : "code" }, String(value));
    case "num": return h("td", { class: "num" }, String(value));
    case "bytes": return h("td", { class: "num" }, fmtBytes(value));
    case "duration": return h("td", { class: "num nowrap" }, fmtDuration(value));
    default: {
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return h("td", { class: text.length > 60 ? "wrap" : text.length <= 24 ? "nowrap" : "" }, text);
    }
  }
}

// ------------------------------------------------------------------- boot
async function boot() {
  try {
    state.me = await api("/api/me");
    state.integrations = await api("/api/integrations");
  } catch (e) {
    $("view").replaceChildren(h("p", { class: "section-msg err" }, `Cannot reach OpsDeck API: ${e.message}`));
    return;
  }
  state.byName = Object.fromEntries(state.integrations.map((i) => [i.name, i]));
  const me = state.me;
  $("env").textContent = me.env;
  if (/prod/i.test(me.env)) $("env").classList.add("prod");
  $("writes").textContent = me.writes_enabled ? "Live actions on" : "Live actions off";
  $("writes").classList.toggle("on", me.writes_enabled);
  $("writes").title = me.writes_enabled
    ? "Actions against live systems run after confirmation"
    : "Actions only run against demo integrations. Set OPSDECK_ENABLE_WRITES=true to allow live actions.";
  $("who").textContent = `${me.user} (${me.role})`;
  $("audit-link").hidden = me.role === "viewer";
  setupChat();
  renderNav();
  window.addEventListener("hashchange", route);
  route();
}

function renderNav() {
  const nav = $("nav");
  nav.replaceChildren(h("a", { href: "#/overview", "data-route": "overview" },
    h("span", { class: "dot live" }), "Overview"));
  const cats = {};
  for (const i of state.integrations) (cats[i.category] ||= []).push(i);
  for (const [cat, items] of Object.entries(cats)) {
    nav.append(h("h3", {}, cat));
    for (const i of items) {
      const health = state.health[i.name];
      const dotCls = i.mode === "demo" ? "demo" : health && !health.ok ? "bad" : "live";
      nav.append(h("a", { href: `#/i/${i.name}`, "data-route": `i/${i.name}`,
        title: i.mode === "demo" ? "Demo data: not connected" : "Connected" },
        h("span", { class: `dot ${dotCls}` }), i.title, i.mode === "demo" ? h("span", { class: "mode" }, "demo") : null));
    }
  }
  markActive();
}

function markActive() {
  const cur = location.hash.replace(/^#\//, "") || "overview";
  for (const a of $("nav").querySelectorAll("a")) a.classList.toggle("active", a.dataset.route === cur);
}

function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }

function route() {
  clearTimers();
  markActive();
  const path = location.hash.replace(/^#\//, "") || "overview";
  const view = $("view");
  view.replaceChildren();
  if (path.startsWith("i/")) return renderIntegration(path.slice(2));
  if (path === "audit") return renderAudit();
  return renderOverview();
}

// --------------------------------------------------------------- overview
async function renderOverview() {
  const view = $("view");
  view.append(h("div", { class: "view-head" }, h("h1", {}, "Overview")),
    h("p", { class: "view-sub" }, "What needs attention across your systems, most severe first."));
  const att = h("div", { class: "attention" }, h("p", { class: "muted thinking" }, "Checking systems"));
  const hl = h("div", { class: "health" });
  view.append(att, h("h2", { class: "sec" }, "Connections"), hl);
  const load = async () => {
    let data;
    try { data = await api("/api/overview"); } catch (e) {
      att.replaceChildren(h("p", { class: "section-msg err" }, e.message)); return;
    }
    state.health = Object.fromEntries(data.health.map((x) => [x.name, x]));
    renderNav();
    att.replaceChildren();
    if (!data.attention.length) att.append(h("div", { class: "all-clear" }, "Nothing needs attention right now."));
    for (const a of data.attention) {
      att.append(h("a", { class: `att ${a.severity}`, href: `#/i/${a.integration}` },
        h("span", { class: "bar" }),
        h("div", { class: "body" }, h("div", { class: "title" }, a.title), h("div", { class: "detail", title: a.detail }, a.detail)),
        h("span", { class: "src" }, a.integration_title)));
    }
    hl.replaceChildren();
    for (const x of data.health) {
      const i = state.byName[x.name];
      hl.append(h("a", { class: "hcell", href: `#/i/${x.name}` },
        h("div", { class: "name" }, h("span", { class: `dot ${x.mode === "demo" ? "demo" : x.ok ? "live" : "bad"}` }), i.title),
        h("div", { class: "detail" }, x.mode === "demo" ? "Demo data. Add credentials to connect." :
          `${x.ok ? "" : "Unreachable: "}${x.detail}${x.ok ? ` (${x.latency_ms} ms)` : ""}`)));
    }
  };
  await load();
  state.timers.push(setInterval(load, 60000));
}

// ------------------------------------------------------------ integration
function opSpec(integ, name) { return integ.ops.find((o) => o.name === name); }

function inputFor(param, value, compact = false) {
  const id = `f-${Math.random().toString(36).slice(2)}`;
  let el;
  if (param.type === "boolean") {
    el = h("input", { type: "checkbox", id, name: param.name });
    el.checked = value === true || value === "true";
  } else if (param.enum) {
    el = h("select", { id, name: param.name },
      param.required ? null : h("option", { value: "" }, "any"),
      param.enum.map((v) => h("option", { value: v }, v)));
    el.value = value ?? "";
  } else if (param.name === "body" || param.name === "parameters") {
    el = h("textarea", { id, name: param.name, rows: 5, placeholder: param.description || "" });
    el.value = value ?? "";
  } else {
    el = h("input", { id, name: param.name, type: param.type === "integer" ? "number" : "text",
      placeholder: compact ? (param.required ? "required" : "any") : param.description || "",
      title: param.description || null, class: param.name === "jql" || param.name === "query" ? "wide" : "" });
    el.value = value ?? "";
  }
  return { id, el };
}
const readInput = (param, el) => param.type === "boolean" ? el.checked : el.value;

function renderIntegration(name) {
  const integ = state.byName[name];
  const view = $("view");
  if (!integ) { view.append(h("p", { class: "section-msg err" }, `Unknown integration "${name}".`)); return; }
  view.append(h("div", { class: "view-head" }, h("h1", {}, integ.title),
    h("span", { class: "pill" }, integ.mode === "demo" ? "Demo data" : "Connected")),
    h("p", { class: "view-sub" }, integ.description));
  if (integ.mode === "demo") {
    view.append(h("p", { class: "demo-note" }, "Showing sample data. To connect, set ",
      integ.required_env.length ? integ.required_env.flatMap((e, i) => [i ? ", " : "", h("code", {}, e)])
        : h("code", {}, name === "kubernetes" || name === "flux" ? "K8S_ENABLED=true" : "credentials"),
      " in your .env and restart."));
  }
  const grid = h("div", { class: "grid" });
  view.append(grid);
  for (const sec of integ.widget) grid.append(renderSection(integ, sec));
}

function renderSection(integ, sec) {
  const spec = opSpec(integ, sec.op);
  const params = { ...sec.params };
  const body = h("div", { class: "section-body" });
  const count = h("span", { class: "count" });
  const filters = h("form", { class: "filters" });
  const fields = [];
  for (const pname of sec.inputs) {
    const p = spec.params.find((x) => x.name === pname);
    if (!p) continue;
    const { id, el } = inputFor(p, params[pname], true);
    fields.push([p, el]);
    filters.append(h("label", { for: id }, titleCase(p.name), el));
    if (p.type === "boolean" || p.enum) el.addEventListener("change", () => load());
  }
  const refresh = h("button", { class: "btn small", type: "submit" }, "Refresh");
  filters.append(refresh);
  filters.addEventListener("submit", (e) => { e.preventDefault(); load(); });
  const box = h("section", { class: `section ${sec.width === "half" ? "half" : ""}` },
    h("div", { class: "section-head" }, h("h2", {}, sec.title), count, h("span", { class: "grow" }), filters), body);

  let sort = null;
  let rows = [];
  async function load() {
    for (const [p, el] of fields) params[p.name] = readInput(p, el);
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== "" && v !== null && v !== undefined) q.set(k, v);
    refresh.disabled = true;
    try {
      const { data } = await api(`/api/integrations/${integ.name}/ops/${sec.op}?${q}`);
      if (sec.kind === "kv" || !Array.isArray(data)) { count.textContent = ""; body.replaceChildren(kvList(data)); }
      else { rows = data; draw(); }
    } catch (e) {
      body.replaceChildren(h("p", { class: "section-msg err" }, e.message));
    } finally { refresh.disabled = false; }
  }
  function draw() {
    count.textContent = `${rows.length}`;
    if (!rows.length) { body.replaceChildren(h("p", { class: "section-msg" }, "No results for these filters.")); return; }
    const allCols = sec.columns.length ? sec.columns : Object.keys(rows[0]).map((k) => ({ key: k, label: "", fmt: "text" }));
    // Link columns render as an "Open" button next to the row actions.
    const linkCols = allCols.filter((c) => c.fmt === "link");
    const cols = allCols.filter((c) => c.fmt !== "link");
    const hasActs = sec.row_actions.length || linkCols.length;
    let data = rows;
    if (sort) {
      data = [...rows].sort((a, b) => {
        const x = a[sort.key] ?? "", y = b[sort.key] ?? "";
        const r = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
        return sort.dir * r;
      });
    }
    const thead = h("tr", {}, cols.map((c) => {
      const th = h("th", { scope: "col", tabindex: 0, "aria-sort": sort && sort.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : null },
        c.label || titleCase(c.key));
      const toggle = () => { sort = sort && sort.key === c.key ? { key: c.key, dir: -sort.dir } : { key: c.key, dir: 1 }; draw(); };
      th.addEventListener("click", toggle);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter") toggle(); });
      return th;
    }), hasActs ? h("th", { class: "acts", "aria-label": "Actions" }) : null);
    const tbody = h("tbody", {}, data.map((row) => h("tr", {},
      cols.map((c) => cell(row[c.key], c.fmt)),
      hasActs ? h("td", { class: "acts" },
        linkCols.filter((c) => /^https?:\/\//.test(row[c.key] || "")).map((c) =>
          h("a", { class: "btn small ghost", href: row[c.key], target: "_blank", rel: "noopener noreferrer" }, "Open")),
        sec.row_actions.map((ra) => {
          const ospec = opSpec(integ, ra.op);
          return h("button", { class: `btn small ${ospec && ospec.kind === "action" ? "act" : ""}`, type: "button",
            onclick: () => runRowAction(integ, ra, row, load) }, ra.label);
        })) : null)));
    body.replaceChildren(h("table", {}, h("thead", {}, thead), tbody));
  }
  load();
  if (sec.refresh_secs) state.timers.push(setInterval(load, sec.refresh_secs * 1000));
  return box;
}

function kvList(obj) {
  if (obj === null || typeof obj !== "object") return h("pre", { class: "log" }, String(obj));
  return h("dl", { class: "kv" }, Object.entries(obj).flatMap(([k, v]) => [
    h("dt", {}, titleCase(k)),
    h("dd", {}, v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)),
  ]));
}

// --------------------------------------------------------------- modals
function openModal({ title, body, actions = [], narrow = false }) {
  const dlg = $("modal");
  dlg.className = `modal ${narrow ? "narrow" : ""}`;
  const close = h("button", { class: "btn ghost small", type: "button", onclick: () => dlg.close() }, "Close");
  dlg.replaceChildren(
    h("div", { class: "modal-head" }, h("h2", {}, title), close),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("div", { class: "modal-foot" }, actions) : "");
  if (!dlg.open) dlg.showModal();
  return dlg;
}

function promptInputs(integ, spec, names, preset = {}) {
  return new Promise((resolve) => {
    const fields = names.map((n) => [spec.params.find((p) => p.name === n), null]).filter(([p]) => p);
    const form = h("form", {});
    for (const f of fields) {
      const { id, el } = inputFor(f[0], preset[f[0].name] ?? f[0].default);
      f[1] = el;
      form.append(h("label", { class: "field", for: id }, h("span", {}, `${titleCase(f[0].name)}${f[0].required ? "" : " (optional)"}`), el));
    }
    let done = false;
    const submit = h("button", { class: "btn primary", type: "button", onclick: () => {
      done = true; dlg.close(); resolve(Object.fromEntries(fields.map(([p, el]) => [p.name, readInput(p, el)])));
    } }, "Continue");
    const dlg = openModal({ title: `${integ.title}: ${spec.title}`, body: form, actions: [submit], narrow: true });
    form.addEventListener("submit", (e) => { e.preventDefault(); submit.click(); });
    dlg.addEventListener("close", () => { if (!done) resolve(null); }, { once: true });
    const first = form.querySelector("input,textarea,select"); if (first) first.focus();
  });
}

async function runRowAction(integ, ra, row, reload) {
  const spec = opSpec(integ, ra.op);
  if (!spec) return toast(`Unknown operation ${ra.op}`, true);
  let params = { ...ra.params };
  for (const [p, key] of Object.entries(ra.map)) params[p] = row[key];
  if (ra.inputs.length) {
    const extra = await promptInputs(integ, spec, ra.inputs);
    if (!extra) return;
    params = { ...params, ...extra };
  }
  if (spec.kind === "read") {
    openModal({ title: `${integ.title}: ${ra.label}`, body: h("p", { class: "muted thinking" }, "Loading") });
    try {
      const q = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) if (v !== "" && v !== null && v !== undefined) q.set(k, v);
      const { data } = await api(`/api/integrations/${integ.name}/ops/${ra.op}?${q}`);
      openModal({ title: `${integ.title}: ${ra.label}`, body: renderResult(data) });
    } catch (e) {
      openModal({ title: `${integ.title}: ${ra.label}`, body: h("p", { class: "section-msg err" }, e.message) });
    }
    return;
  }
  try {
    const prepared = await api(`/api/integrations/${integ.name}/actions/${ra.op}/prepare`, { body: { params } });
    confirmAction(prepared, reload);
  } catch (e) { toast(e.message, true); }
}

function renderResult(data) {
  if (data && typeof data === "object" && !Array.isArray(data) && typeof data.log === "string") {
    const pre = h("pre", { class: "log" }, data.log || "(empty)");
    requestAnimationFrame(() => { pre.parentElement.scrollTop = pre.scrollHeight; });
    return pre;
  }
  if (data && typeof data === "object" && !Array.isArray(data) && typeof data.text === "string") {
    return h("div", {}, h("p", { class: "muted" }, `${data.title || ""}`), h("pre", { class: "log" }, data.text));
  }
  if (Array.isArray(data)) {
    if (!data.length) return h("p", { class: "muted" }, "Nothing to show.");
    const keys = Object.keys(data[0]);
    return h("table", {}, h("thead", {}, h("tr", {}, keys.map((k) => h("th", {}, titleCase(k))))),
      h("tbody", {}, data.map((r) => h("tr", {}, keys.map((k) => cell(r[k],
        /status|sync|health|result/.test(k) ? "status" : /time|when|updated|created|since/.test(k) ? "time" : /url/.test(k) ? "link" : "text"))))));
  }
  return kvList(data);
}

function confirmAction(prepared, after) {
  const body = h("div", {},
    h("p", { class: "confirm-summary" }, prepared.summary),
    h("dl", { class: "kv" },
      h("dt", {}, "Risk"), h("dd", {}, h("span", { class: `risk ${prepared.risk}` }, titleCase(prepared.risk))),
      h("dt", {}, "Target"), h("dd", {}, prepared.mode === "demo" ? "Demo integration (nothing real changes)" : "LIVE system"),
      h("dt", {}, "Expires"), h("dd", {}, new Date(prepared.expires_at * 1000).toLocaleTimeString())));
  const go = h("button", { class: `btn ${prepared.mode === "live" ? "danger" : "primary"}`, type: "button" },
    prepared.mode === "live" ? "Run on live system" : "Run");
  const dlg = openModal({ title: "Confirm action", body, actions: [go], narrow: true });
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const res = await api("/api/actions/execute", { body: { token: prepared.token } });
      dlg.close();
      toast(`Done: ${prepared.summary.split("\n")[0]}${res.mode === "demo" ? " (demo)" : ""}`);
      if (after) setTimeout(after, 1500);
    } catch (e) { go.disabled = false; toast(e.message, true); }
  });
  go.focus();
}

// ------------------------------------------------------------------ audit
async function renderAudit() {
  const view = $("view");
  view.append(h("div", { class: "view-head" }, h("h1", {}, "Audit log")),
    h("p", { class: "view-sub" }, "Every action prepared, run or failed in this console, newest first."));
  const box = h("section", { class: "section" }, h("div", { class: "section-body" }, h("p", { class: "section-msg thinking" }, "Loading")));
  view.append(h("div", { class: "grid" }, box));
  try {
    const rows = await api("/api/audit");
    const body = box.querySelector(".section-body");
    if (!rows.length) { body.replaceChildren(h("p", { class: "section-msg" }, "No actions recorded yet.")); return; }
    body.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["When", "User", "Integration", "Action", "Mode", "Status", "Via", "Params", "Detail"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        cell(new Date(r.ts * 1000).toISOString(), "time"), cell(r.user), cell(r.integration), cell(r.op, "code"),
        cell(r.mode), cell(r.status, "status"), cell(r.via),
        cell(Object.entries(r.params).filter(([, v]) => v !== null && v !== "").map(([k, v]) => `${k}=${v}`).join(", "), "code"),
        cell(r.detail.length > 160 ? r.detail.slice(0, 160) + "..." : r.detail))))));
  } catch (e) { box.querySelector(".section-body").replaceChildren(h("p", { class: "section-msg err" }, e.message)); }
}

// ------------------------------------------------------------------- chat
function inlineMd(text) {
  // text is escaped first; only our own tags are introduced afterwards.
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
function markdown(text) {
  const root = h("div", {});
  const lines = String(text || "").split("\n");
  let list = null, code = null;
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (code) { root.append(h("pre", {}, code.join("\n"))); code = null; } else { code = []; list = null; }
      continue;
    }
    if (code) { code.push(line); continue; }
    const m = line.match(/^\s*[-*]\s+(.*)$/) || line.match(/^\s*\d+\.\s+(.*)$/);
    if (m) {
      if (!list) { list = h("ul", {}); root.append(list); }
      const li = h("li", {}); li.innerHTML = inlineMd(m[1]); list.append(li); continue;
    }
    list = null;
    if (!line.trim()) continue;
    const hd = line.match(/^#{1,4}\s+(.*)$/);
    const el = h(hd ? "h4" : "p", {}); el.innerHTML = inlineMd(hd ? hd[1] : line); root.append(el);
  }
  if (code) root.append(h("pre", {}, code.join("\n")));
  return root;
}

function setupChat() {
  const shell = document.querySelector(".shell");
  const toggle = $("chat-toggle");
  const narrow = window.matchMedia("(max-width: 1200px)").matches;
  let hidden = narrow;
  try { const saved = localStorage.getItem("opsdeck.chat"); if (saved !== null) hidden = saved === "hidden"; } catch { /* ignore */ }
  const apply = () => { shell.classList.toggle("chat-hidden", hidden); toggle.setAttribute("aria-expanded", String(!hidden)); };
  apply();
  toggle.addEventListener("click", () => {
    hidden = !hidden; apply();
    try { localStorage.setItem("opsdeck.chat", hidden ? "hidden" : "shown"); } catch { /* ignore */ }
    if (!hidden) $("chat-input").focus();
  });
  const st = state.me.assistant;
  $("chat-status").textContent = st.available ? st.model : "Not configured";
  $("chat-status").title = st.detail;
  const log = $("chat-log");
  const intro = () => log.replaceChildren(h("div", { class: "msg bot" }, st.available
    ? markdown("Ask about anything across your systems. I can read every connected tool. Changes I propose need your confirmation before they run.")
    : markdown(`The assistant is off: ${st.detail}. Every widget still works without it.`)));
  intro();
  $("chat-clear").addEventListener("click", () => { state.chat = []; intro(); });
  const input = $("chat-input");
  input.disabled = !st.available;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); } });
  $("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !st.available) return;
    input.value = "";
    state.chat.push({ role: "user", content: text });
    log.append(h("div", { class: "msg user" }, text));
    const pending = h("div", { class: "msg bot muted thinking" }, "Investigating");
    log.append(pending); log.scrollTop = log.scrollHeight;
    try {
      const res = await api("/api/chat", { body: { messages: state.chat } });
      pending.remove();
      if (res.tools.length) {
        log.append(h("div", { class: "trace" }, res.tools.map((t) =>
          h("div", { class: t.ok ? "" : "fail", title: JSON.stringify(t.input) },
            `${t.ok ? "✓" : "✗"} ${t.tool.replace("__", ".")} ${Object.values(t.input || {}).join(" ")}`))));
      }
      log.append(h("div", { class: "msg bot" }, markdown(res.reply || "(no answer)")));
      state.chat.push({ role: "assistant", content: res.reply || "(no answer)" });
      for (const p of res.proposals) {
        const card = h("div", { class: "proposal" }, h("p", {}, p.summary),
          h("div", { class: "row" },
            h("span", { class: `risk ${p.risk}` }, `${titleCase(p.risk)} risk`), h("span", { class: "grow" }),
            h("button", { class: "btn small primary", type: "button", onclick: () => confirmAction(p) }, "Review and run")));
        log.append(card);
      }
    } catch (err) {
      pending.remove();
      log.append(h("div", { class: "msg err" }, err.message));
      state.chat.pop();
    }
    log.scrollTop = log.scrollHeight;
  });
}

boot();
