// OpsDeck web UI. No build step: plain ES modules, DOM built with textContent
// (never innerHTML with untrusted data) so upstream data can't inject markup.
import { $, api, cell, confirmAction, h, markdown, openModal, state, streamSSE, titleCase, toast } from "./core.js";
import { renderDashboard, renderEditor, renderGallery, renderWidgetPage, startHotReload } from "./widgets.js";

// ------------------------------------------------------------------- boot
async function boot() {
  try {
    [state.me, state.integrations, state.playbooks] = await Promise.all([
      api("/api/me"), api("/api/integrations"), api("/api/playbooks").catch(() => [])]);
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
  startHotReload();
  window.addEventListener("hashchange", route);
  route();
}

function renderNav() {
  const nav = $("nav");
  nav.replaceChildren(
    h("a", { href: "#/overview", "data-route": "overview" }, h("span", { class: "dot live" }), "Overview"),
    h("a", { href: "#/dashboard", "data-route": "dashboard" }, h("span", { class: "ico" }, "▦"), "My dashboard"),
    h("a", { href: "#/widgets", "data-route": "widgets" }, h("span", { class: "ico" }, "✦"), "Widgets"));
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
state.renderNav = renderNav;

function markActive() {
  const cur = location.hash.replace(/^#\//, "") || "overview";
  const top = cur.startsWith("widgets") ? "widgets" : cur;
  for (const a of $("nav").querySelectorAll("a")) a.classList.toggle("active", a.dataset.route === top);
}

function clearTimers() {
  state.timers.forEach(clearInterval); state.timers = [];
  state.cleanups.forEach((f) => { try { f(); } catch { /* ignore */ } }); state.cleanups = [];
}

function route() {
  clearTimers();
  markActive();
  const path = location.hash.replace(/^#\//, "") || "overview";
  const view = $("view");
  view.replaceChildren();
  view.scrollTop = 0;
  if (path.startsWith("i/")) return renderIntegration(path.slice(2));
  if (path === "audit") return renderAudit();
  if (path === "dashboard") return renderDashboard();
  if (path === "widgets") return renderGallery();
  if (path === "widgets/new") return renderEditor(null);
  const m = path.match(/^widgets\/([a-z0-9-]+)(\/edit)?$/);
  if (m) return m[2] ? renderEditor(m[1]) : renderWidgetPage(m[1]);
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
function setupChat() {
  const shell = document.querySelector(".shell");
  const toggle = $("chat-toggle");
  const narrow = window.matchMedia("(max-width: 1200px)").matches;
  let hidden = narrow;
  try { const saved = localStorage.getItem("opsdeck.chat"); if (saved !== null) hidden = saved === "hidden"; } catch { /* ignore */ }
  const apply = () => { shell.classList.toggle("chat-hidden", hidden); toggle.setAttribute("aria-expanded", String(!hidden)); };
  apply();
  const show = () => { if (hidden) { hidden = false; apply(); } };
  toggle.addEventListener("click", () => {
    hidden = !hidden; apply();
    try { localStorage.setItem("opsdeck.chat", hidden ? "hidden" : "shown"); } catch { /* ignore */ }
    if (!hidden) $("chat-input").focus();
  });

  const st = state.me.assistant;
  const usable = st.providers.filter((p) => p.configured);
  const provSel = $("chat-provider");
  const modelIn = $("chat-model");
  provSel.replaceChildren(...(usable.length ? usable : [{ name: "", title: "No provider configured", model: "" }]).map((p) =>
    h("option", { value: p.name }, p.title)));
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("opsdeck.llm") || "{}"); } catch { /* ignore */ }
  provSel.value = usable.some((p) => p.name === saved.provider) ? saved.provider : (st.provider || "");
  const loadModels = async () => {
    const p = usable.find((x) => x.name === provSel.value);
    modelIn.value = saved.provider === provSel.value && saved.model ? saved.model : p?.model || "";
    modelIn.placeholder = p?.model || "model";
    $("chat-models").replaceChildren();
    if (!p) return;
    try {
      const { models } = await api(`/api/llm/models?provider=${encodeURIComponent(p.name)}`);
      $("chat-models").replaceChildren(...models.map((m) => h("option", { value: m })));
    } catch { /* listing is optional */ }
  };
  const remember = () => { saved = { provider: provSel.value, model: modelIn.value.trim() };
    try { localStorage.setItem("opsdeck.llm", JSON.stringify(saved)); } catch { /* ignore */ } };
  provSel.addEventListener("change", () => { saved = {}; loadModels(); remember(); });
  modelIn.addEventListener("change", remember);
  loadModels();
  $("chat-status").textContent = st.available ? "" : "Not configured";
  $("chat-status").title = st.detail;

  // playbooks
  let playbook = null;
  const pbBox = $("playbooks");
  const drawPlaybooks = () => pbBox.replaceChildren(...state.playbooks.map((p) => h("button", {
    type: "button", class: `chip ${playbook === p.id ? "on" : ""}`, title: p.description,
    onclick: () => {
      playbook = playbook === p.id ? null : p.id; drawPlaybooks();
      if (playbook && p.starter && !$("chat-input").value.trim()) $("chat-input").value = p.starter;
      $("chat-input").focus();
    } }, p.title)));
  drawPlaybooks();

  const log = $("chat-log");
  const intro = () => log.replaceChildren(h("div", { class: "msg bot" }, st.available
    ? markdown("Ask about anything across your systems: incidents, metrics, logs, traces, tickets, code, CVEs. " +
      "Pick a **playbook** for guided work (RCA, ticket to PR, CVE remediation), or ask me to **build a widget**. " +
      "Changes I propose need your confirmation before they run.")
    : markdown(`The assistant is off: ${st.detail}. Every widget still works without it.`)));
  intro();
  let controller = null;
  $("chat-clear").addEventListener("click", () => { if (controller) controller.abort(); state.chat = []; intro(); });
  const input = $("chat-input");
  input.disabled = !st.available;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); } });

  state.ask = (text, { send = false } = {}) => {
    show();
    input.value = text;
    input.focus();
    if (send) $("chat-form").requestSubmit();
  };

  $("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !st.available || controller) return;
    input.value = "";
    state.chat.push({ role: "user", content: text });
    log.append(h("div", { class: "msg user" }, text));
    const turn = h("div", { class: "turn" });
    const trace = h("div", { class: "trace" });
    const pending = h("div", { class: "msg bot muted thinking" }, "Investigating");
    turn.append(trace, pending);
    log.append(turn); log.scrollTop = log.scrollHeight;
    const toolRows = {};
    let reply = "";
    const scroll = () => { log.scrollTop = log.scrollHeight; };
    controller = new AbortController();
    $("chat-send").textContent = "…";
    try {
      await streamSSE("/api/chat/stream", { messages: state.chat, provider: provSel.value || null,
        model: modelIn.value.trim() || null, playbook }, (ev) => {
        if (ev.type === "start") {
          $("chat-status").textContent = `${ev.model}${ev.playbook ? " · " + ev.playbook : ""}`;
        } else if (ev.type === "tool") {
          const args = Object.entries(ev.input || {}).map(([k, v]) => `${k}=${typeof v === "object" ? "…" : v}`).join(" ");
          const row = h("div", { class: "run", title: JSON.stringify(ev.input) }, `… ${ev.tool.replace("__", ".")} ${args}`);
          toolRows[ev.id] = row; trace.append(row); pending.textContent = "Investigating";
        } else if (ev.type === "tool_result") {
          const row = toolRows[ev.id];
          if (row) { row.className = ev.ok ? "" : "fail"; row.textContent = row.textContent.replace(/^…/, ev.ok ? "✓" : "✗"); }
        } else if (ev.type === "text") {
          reply += (reply ? "\n\n" : "") + ev.text;
          turn.insertBefore(h("div", { class: "msg bot" }, markdown(ev.text)), pending);
        } else if (ev.type === "proposal") {
          turn.insertBefore(proposalCard(ev), pending);
        } else if (ev.type === "widget") {
          turn.insertBefore(h("div", { class: "proposal widgetcard" },
            h("p", {}, h("strong", {}, ev.title || ev.id), ` ${ev.ok ? "built" : "saved with problems"} (${ev.status})`),
            ev.errors && ev.errors.length ? h("p", { class: "muted small" }, ev.errors.slice(0, 2).join("; ")) : null,
            h("div", { class: "row" }, h("span", { class: "grow" }),
              h("a", { class: "btn small", href: `#/widgets/${ev.id}/edit` }, "Edit"),
              h("a", { class: "btn small primary", href: `#/widgets/${ev.id}` }, "Open"))), pending);
        } else if (ev.type === "error") {
          turn.insertBefore(h("div", { class: "msg err" }, ev.detail), pending);
        } else if (ev.type === "done") {
          const u = ev.usage || {};
          if (u.input_tokens || u.output_tokens) {
            turn.append(h("div", { class: "usage" }, `${ev.model} · ${(u.input_tokens || 0).toLocaleString()} in / ` +
              `${(u.output_tokens || 0).toLocaleString()} out tokens · ${ev.tools.length} tool call(s)`));
          }
        }
        scroll();
      }, controller.signal);
      pending.remove();
      state.chat.push({ role: "assistant", content: reply || "(no answer)" });
    } catch (err) {
      pending.remove();
      if (err.name !== "AbortError") turn.append(h("div", { class: "msg err" }, err.message));
      state.chat.pop();
    } finally {
      controller = null;
      $("chat-send").textContent = "Send";
      scroll();
    }
  });
}

function proposalCard(p) {
  const first = p.summary.split("\n")[0];
  const run = h("button", { class: "btn small primary", type: "button" }, "Review and run");
  const card = h("div", { class: "proposal" }, h("p", {}, first),
    p.summary.includes("\n") ? h("p", { class: "muted small" }, "Details and diff in the review dialog.") : null,
    h("div", { class: "row" },
      h("span", { class: `risk ${p.risk}` }, `${titleCase(p.risk)} risk`), h("span", { class: "grow" }), run));
  run.addEventListener("click", async () => {
    const res = await confirmAction(p);
    if (res) { run.disabled = true; run.textContent = "Done"; }
  });
  return card;
}

boot();
