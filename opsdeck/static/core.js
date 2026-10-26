// Shared helpers for the OpsDeck UI. DOM is built with textContent (never
// innerHTML with untrusted data) so upstream data can't inject markup.


export const state = { me: null, integrations: [], byName: {}, timers: [], cleanups: [], chat: [], health: {},
  playbooks: [], ask: null, renderNav: null };

// ------------------------------------------------------------------ helpers
export async function api(path, opts = {}) {
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

export function h(tag, props = {}, ...kids) {
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

export const $ = (id) => document.getElementById(id);
export const titleCase = (s) => String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : ""), role: err ? "alert" : "status" }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), err ? 9000 : 5000);
}

export function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const s = Math.round((Date.now() - t) / 1000);
  const abs = Math.abs(s);
  const unit = abs < 60 ? [abs, "s"] : abs < 3600 ? [Math.round(abs / 60), "m"]
    : abs < 86400 ? [Math.round(abs / 3600), "h"] : [Math.round(abs / 86400), "d"];
  return s >= 0 ? `${unit[0]}${unit[1]} ago` : `in ${unit[0]}${unit[1]}`;
}

export const STATUS = {
  ok: ["executed", "ok", "ready", "running", "succeeded", "success", "synced", "healthy", "active", "done", "closed",
       "resolved", "true", "completed", "available", "low", "informational", "normal"],
  warn: ["prepared", "progressing", "pending", "outofsync", "unstable", "warning", "medium", "in progress", "building",
         "insufficient_data", "suspended", "monitoring", "in review", "scheduled", "high", "unknown", "stopped",
         "aborted", "notbuilt", "scaleddown", "to do", "running"],
  crit: ["failed", "failure", "degraded", "error", "crashloopbackoff", "imagepullbackoff", "errimagepull",
         "oomkilled", "alarm", "critical", "highest", "missing", "false", "createcontainerconfigerror", "evicted"],
};
export function statusClass(v) {
  const s = String(v ?? "").toLowerCase();
  if (STATUS.crit.includes(s)) return "crit";
  if (s === "running") return "ok";
  if (STATUS.ok.includes(s)) return "ok";
  if (STATUS.warn.includes(s)) return "warn";
  return "";
}

export function fmtBytes(n) {
  if (n === null || n === undefined || n === "") return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
}
export function fmtDuration(s) {
  s = Number(s || 0); if (!s) return "";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function cell(value, fmt) {
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

// --------------------------------------------------------------- modals
export function openModal({ title, body, actions = [], narrow = false }) {
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

/** Render an action summary; unified diffs (from PR proposals) get line colouring. */
function summaryBlock(text) {
  const lines = String(text || "").split("\n");
  const diffAt = lines.findIndex((l) => l.startsWith("--- ") || l.startsWith("@@"));
  if (diffAt < 0) return h("p", { class: "confirm-summary" }, text);
  const head = lines.slice(0, diffAt).join("\n").trim();
  const diff = h("pre", { class: "diff" }, lines.slice(diffAt).map((l) => h("span", {
    class: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del"
      : l.startsWith("@@") ? "hunk" : l.startsWith("---") || l.startsWith("+++") ? "file" : "" }, l + "\n")));
  return h("div", {}, h("p", { class: "confirm-summary" }, head), diff);
}

/** Show the confirmation dialog for a prepared action. Resolves with the result, or null if dismissed. */
export function confirmAction(prepared, after) {
  return new Promise((resolve) => {
    let settled = false;
    const body = h("div", {},
      summaryBlock(prepared.summary),
      h("dl", { class: "kv" },
        h("dt", {}, "Risk"), h("dd", {}, h("span", { class: `risk ${prepared.risk}` }, titleCase(prepared.risk))),
        h("dt", {}, "Target"), h("dd", {}, prepared.mode === "demo" ? "Demo integration (nothing real changes)" : "LIVE system"),
        h("dt", {}, "Expires"), h("dd", {}, new Date(prepared.expires_at * 1000).toLocaleTimeString())));
    const go = h("button", { class: `btn ${prepared.mode === "live" ? "danger" : "primary"}`, type: "button" },
      prepared.mode === "live" ? "Run on live system" : "Run");
    const dlg = openModal({ title: "Confirm action", body, actions: [go], narrow: !/\n@@/.test(prepared.summary) });
    dlg.addEventListener("close", () => { if (!settled) { settled = true; resolve(null); } }, { once: true });
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        const res = await api("/api/actions/execute", { body: { token: prepared.token } });
        settled = true;
        dlg.close();
        toast(`Done: ${prepared.summary.split("\n")[0]}${res.mode === "demo" ? " (demo)" : ""}`);
        resolve(res.result ?? res);
        if (after) setTimeout(after, 1500);
      } catch (e) { go.disabled = false; toast(e.message, true); }
    });
    go.focus();
  });
}

/** POST a JSON body and read a server-sent-event stream; onEvent(obj) for each `data:` line. */
export async function streamSSE(path, body, onEvent, signal) {
  const res = await fetch(path, { method: "POST", signal, headers: {
    "Content-Type": "application/json", Accept: "text/event-stream", "X-OpsDeck": "1" }, body: JSON.stringify(body) });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) { console.error(e); } }
      }
    }
  }
}

// ------------------------------------------------------------------- chat
export function inlineMd(text) {
  // text is escaped first; only our own tags are introduced afterwards.
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
export function markdown(text) {
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