/* OpsDeck widget SDK. Inlined into every widget frame (a sandboxed iframe with
 * no network access). The widget's view.js defines `function render(ctx)`;
 * ctx = { root, data, inputs, errors, demo, widget, sdk }.
 * All data and actions go through the parent window via postMessage, and the
 * parent only allows operations the widget declared in widget.json.
 */
(function () {
  "use strict";
  const pending = new Map();
  let seq = 0;
  let last = null;
  const post = (msg) => parent.postMessage(msg, "*");

  function call(type, payload) {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      post({ type, id, ...payload });
    });
  }

  // ------------------------------------------------------------- DOM helper
  function h(tag, props, ...kids) {
    const svg = ["svg", "g", "path", "line", "rect", "circle", "text", "polyline", "title", "defs", "linearGradient", "stop"].includes(tag);
    const el = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") svg ? el.setAttribute("class", v) : (el.className = v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  // ------------------------------------------------------------- formatting
  const fmt = {
    num(v, digits = 1) {
      if (v === null || v === undefined || v === "") return "";
      const n = Number(v); if (!Number.isFinite(n)) return String(v);
      const a = Math.abs(n);
      if (a >= 1e9) return (n / 1e9).toFixed(digits) + "B";
      if (a >= 1e6) return (n / 1e6).toFixed(digits) + "M";
      if (a >= 1e4) return (n / 1e3).toFixed(digits) + "k";
      if (a >= 100 || Number.isInteger(n)) return String(Math.round(n));
      if (a >= 1) return n.toFixed(digits + 1).replace(/\.?0+$/, "");
      return n.toPrecision(2);
    },
    pct(v, digits = 1) { return v === null || v === undefined ? "" : `${(Number(v) * 100).toFixed(digits)}%`; },
    bytes(n) {
      if (n === null || n === undefined || n === "") return "";
      const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = Number(n);
      while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
    },
    ms(v) { const n = Number(v); if (!Number.isFinite(n)) return ""; return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${Math.round(n)}ms`; },
    duration(s) {
      s = Math.round(Number(s || 0)); if (!s) return "";
      if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
      return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    },
    rel(iso) {
      if (!iso) return ""; const t = typeof iso === "number" ? (iso < 1e12 ? iso * 1000 : iso) : new Date(iso).getTime();
      if (Number.isNaN(t)) return String(iso);
      const s = Math.round((Date.now() - t) / 1000), a = Math.abs(s);
      const u = a < 60 ? [a, "s"] : a < 3600 ? [Math.round(a / 60), "m"] : a < 86400 ? [Math.round(a / 3600), "h"] : [Math.round(a / 86400), "d"];
      return s >= 0 ? `${u[0]}${u[1]} ago` : `in ${u[0]}${u[1]}`;
    },
    time(v) { const d = new Date(typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : v); return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); },
  };

  const STATUS = {
    ok: /^(ok|ready|running|succeeded|success|synced|healthy|active|done|resolved|completed|available|up|normal|green|not_alerting|low|informational|p3)$/i,
    warn: /^(warn|warning|pending|progressing|degraded?|outofsync|acknowledged|medium|high|updating|no data|unknown|p2|yellow|in progress)$/i,
    crit: /^(crit|critical|error|failed|failure|alert|firing|triggered|down|crashloopbackoff|imagepullbackoff|errimagepull|oomkilled|highest|p1|red|true)$/i,
  };
  function status(v) {
    const s = String(v ?? "").trim();
    if (STATUS.crit.test(s)) return "crit";
    if (STATUS.ok.test(s)) return "ok";
    if (STATUS.warn.test(s)) return "warn";
    return "";
  }

  // ------------------------------------------------------------- components
  function badge(text, st) { return h("span", { class: `od-badge ${st ?? status(text)}` }, String(text ?? "")); }

  function stat({ label, value, unit = "", status: st = "", sub = "", spark = null }) {
    return h("div", { class: `od-stat ${st}` },
      h("div", { class: "od-stat-label" }, label),
      h("div", { class: "od-stat-value" }, String(value ?? "–"), unit ? h("span", { class: "od-unit" }, unit) : null),
      sub ? h("div", { class: "od-stat-sub" }, sub) : null,
      spark ? sparkline(spark, { status: st }) : null);
  }
  function stats(items) { return h("div", { class: "od-stats" }, items.map(stat)); }

  function tiles(items) {
    return h("div", { class: "od-tiles" }, items.map((it) => h("div", {
      class: `od-tile ${it.status ?? status(it.value)}`, title: it.title || "", role: it.onClick ? "button" : null,
      tabindex: it.onClick ? 0 : null, onclick: it.onClick || null,
    }, h("div", { class: "od-tile-label" }, it.label), h("div", { class: "od-tile-value" }, String(it.value ?? "")),
      it.sub ? h("div", { class: "od-tile-sub" }, it.sub) : null)));
  }

  function cellFor(value, col) {
    const f = col.fmt || "text";
    if (value === null || value === undefined) return h("td", {}, "");
    if (typeof col.render === "function") return h("td", {}, col.render(value));
    switch (f) {
      case "status": return h("td", {}, badge(value));
      case "time": return h("td", { class: "od-nowrap", title: String(value) }, fmt.rel(value));
      case "num": return h("td", { class: "od-num" }, fmt.num(value));
      case "pct": return h("td", { class: "od-num" }, fmt.pct(value));
      case "bytes": return h("td", { class: "od-num" }, fmt.bytes(value));
      case "ms": return h("td", { class: "od-num" }, fmt.ms(value));
      case "code": return h("td", { class: String(value).length <= 24 ? "od-code od-nowrap" : "od-code" }, String(value));
      case "link": return h("td", {}, /^https?:\/\//.test(value) ? h("span", { class: "od-muted" }, "link") : "");
      default: return h("td", {}, typeof value === "object" ? JSON.stringify(value) : String(value));
    }
  }

  function table(rows, opts = {}) {
    rows = Array.isArray(rows) ? rows : [];
    if (!rows.length) return h("p", { class: "od-empty" }, opts.empty || "Nothing to show.");
    const columns = opts.columns || Object.keys(rows[0]).map((k) => ({ key: k }));
    const wrap = h("div", { class: "od-table-wrap" });
    let sort = null;
    const draw = () => {
      let data = rows;
      if (sort) data = [...rows].sort((a, b) => {
        const x = a[sort.key] ?? "", y = b[sort.key] ?? "";
        return sort.dir * (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true }));
      });
      if (opts.maxRows) data = data.slice(0, opts.maxRows);
      const head = h("tr", {}, columns.map((c) => h("th", {
        onclick: () => { sort = sort && sort.key === c.key ? { key: c.key, dir: -sort.dir } : { key: c.key, dir: 1 }; draw(); },
      }, c.label || c.key.replace(/_/g, " "))), opts.actions ? h("th", {}) : null);
      const body = data.map((r) => h("tr", { class: opts.onRowClick ? "od-click" : "", onclick: opts.onRowClick ? () => opts.onRowClick(r) : null },
        columns.map((c) => cellFor(r[c.key], c)),
        opts.actions ? h("td", { class: "od-acts" }, opts.actions.filter((a) => !a.when || a.when(r)).map((a) =>
          h("button", { class: `od-btn ${a.kind || ""}`, onclick: (e) => { e.stopPropagation(); a.onClick(r); } }, a.label))) : null));
      wrap.replaceChildren(h("table", { class: "od-table" }, h("thead", {}, head), h("tbody", {}, body)));
    };
    draw();
    return wrap;
  }

  // ----------------------------------------------------------------- charts
  const PALETTE = ["#8FA8FF", "#3FB68B", "#E3A63B", "#EF5F5F", "#6CA6E8", "#C08CF0", "#4FC1C9", "#E07FB0"];

  function niceTicks(min, max, count = 4) {
    if (min === max) { max = min + 1; min = min - 1; }
    const span = max - min, step0 = span / count, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || mag * 10;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toPrecision(12));
    return out;
  }

  /** series: [{name, points: [[unixSeconds, value], ...]}] (the shape prometheus.query_range returns
   *  as {series, points}); opts: {height, yFormat, title, area, thresholds:[{value,label,status}]} */
  function lineChart(series, opts = {}) {
    series = (series || []).filter((s) => s && (s.points || []).length).map((s, i) => ({
      name: s.name || s.series || `series ${i + 1}`, points: s.points, color: s.color || PALETTE[i % PALETTE.length] }));
    const box = h("div", { class: "od-chart" });
    if (opts.title) box.append(h("div", { class: "od-chart-title" }, opts.title));
    if (!series.length) { box.append(h("p", { class: "od-empty" }, "No data points.")); return box; }
    const W = Math.max(320, Math.min(1400, (document.body.clientWidth || 668) - 28)), H = opts.height || 200, L = 52, R = 12, T = 10, B = 24;
    const xs = series.flatMap((s) => s.points.map((p) => p[0])), ys = series.flatMap((s) => s.points.map((p) => p[1]));
    (opts.thresholds || []).forEach((t) => ys.push(t.value));
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const ticks = niceTicks(opts.min ?? Math.min(0, ...ys), opts.max ?? Math.max(...ys));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const X = (t) => L + ((t - x0) / Math.max(1, x1 - x0)) * (W - L - R);
    const Y = (v) => T + (1 - (v - y0) / Math.max(1e-12, y1 - y0)) * (H - T - B);
    const yf = opts.yFormat || fmt.num;
    const svg = h("svg", { viewBox: `0 0 ${W} ${H}`, class: "od-svg", role: "img", "aria-label": opts.title || "chart" });
    ticks.forEach((v) => svg.append(h("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "od-grid" }),
      h("text", { x: L - 6, y: Y(v) + 4, class: "od-axis", "text-anchor": "end" }, yf(v))));
    for (let i = 0; i <= 4; i++) {
      const t = x0 + ((x1 - x0) * i) / 4;
      svg.append(h("text", { x: X(t), y: H - 6, class: "od-axis", "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, fmt.time(t)));
    }
    (opts.thresholds || []).forEach((t) => svg.append(
      h("line", { x1: L, x2: W - R, y1: Y(t.value), y2: Y(t.value), class: `od-threshold ${t.status || "crit"}` }),
      h("text", { x: W - R, y: Y(t.value) - 4, class: `od-axis od-th-label ${t.status || "crit"}`, "text-anchor": "end" }, t.label || yf(t.value))));
    series.forEach((s) => {
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
      if (opts.area && series.length === 1) svg.append(h("path", { d: `${d}L${X(s.points.at(-1)[0])},${Y(y0)}L${X(s.points[0][0])},${Y(y0)}Z`, fill: s.color, opacity: 0.12 }));
      svg.append(h("path", { d, fill: "none", stroke: s.color, "stroke-width": 1.8, "stroke-linejoin": "round" }));
    });
    // hover crosshair + readout
    const cross = h("line", { y1: T, y2: H - B, class: "od-cross", visibility: "hidden" });
    const tip = h("div", { class: "od-tip", hidden: true });
    svg.append(cross);
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect(), px = ((e.clientX - r.left) / r.width) * W;
      const t = x0 + ((px - L) / (W - L - R)) * (x1 - x0);
      cross.setAttribute("x1", X(Math.min(x1, Math.max(x0, t)))); cross.setAttribute("x2", X(Math.min(x1, Math.max(x0, t))));
      cross.setAttribute("visibility", "visible");
      const rows = series.map((s) => {
        let best = s.points[0];
        for (const p of s.points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
        return h("div", {}, h("span", { class: "od-sw", style: { background: s.color } }), `${s.name}: `, h("b", {}, yf(best[1])));
      });
      tip.replaceChildren(h("div", { class: "od-muted" }, fmt.time(t)), rows); tip.hidden = false;
      tip.style.left = `${Math.min(r.width - 180, Math.max(0, e.clientX - r.left + 12))}px`;
    });
    svg.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
    box.append(h("div", { class: "od-chart-body" }, svg, tip));
    if (series.length > 1 || opts.legend) box.append(h("div", { class: "od-legend" }, series.map((s) =>
      h("span", {}, h("span", { class: "od-sw", style: { background: s.color } }), s.name))));
    return box;
  }

  /** items: [{label, value, status?}] -> horizontal bars */
  function barChart(items, opts = {}) {
    items = items || [];
    const max = Math.max(1e-12, ...items.map((i) => Number(i.value) || 0));
    const yf = opts.format || fmt.num;
    return h("div", { class: "od-bars" }, opts.title ? h("div", { class: "od-chart-title" }, opts.title) : null,
      items.length ? items.map((it, i) => h("div", { class: "od-bar-row" },
        h("div", { class: "od-bar-label", title: it.label }, it.label),
        h("div", { class: "od-bar-track" }, h("div", { class: `od-bar ${it.status || ""}`, style: {
          width: `${Math.max(1, ((Number(it.value) || 0) / max) * 100)}%`, background: it.status ? null : (it.color || PALETTE[i % PALETTE.length]) } })),
        h("div", { class: "od-bar-value" }, yf(it.value)))) : h("p", { class: "od-empty" }, "No data."));
  }

  function sparkline(values, opts = {}) {
    const vals = (values || []).map((v) => (Array.isArray(v) ? v[1] : v)).filter((v) => Number.isFinite(Number(v))).map(Number);
    const W = 120, H = 28;
    const svg = h("svg", { viewBox: `0 0 ${W} ${H}`, class: `od-spark ${opts.status || ""}`, preserveAspectRatio: "none" });
    if (vals.length < 2) return svg;
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const d = vals.map((v, i) => `${i ? "L" : "M"}${((i / (vals.length - 1)) * W).toFixed(1)},${(H - 2 - ((v - lo) / Math.max(1e-12, hi - lo)) * (H - 4)).toFixed(1)}`).join("");
    svg.append(h("path", { d, fill: "none", "stroke-width": 1.5 }));
    return svg;
  }

  function section(title, ...kids) { return h("div", { class: "od-section" }, title ? h("div", { class: "od-section-title" }, title) : null, kids); }
  function note(text, st = "") { return h("div", { class: `od-note ${st}` }, text); }
  function md(text) { // tiny, safe: paragraphs + **bold** + `code` via text nodes
    const root = h("div", { class: "od-md" });
    String(text || "").split(/\n{2,}/).forEach((para) => {
      const p = h("p", {});
      para.split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach((part) => {
        if (/^\*\*.+\*\*$/.test(part)) p.append(h("strong", {}, part.slice(2, -2)));
        else if (/^`.+`$/.test(part)) p.append(h("code", {}, part.slice(1, -1)));
        else p.append(document.createTextNode(part));
      });
      root.append(p);
    });
    return root;
  }

  // ------------------------------------------------------------ public sdk
  const sdk = {
    h, fmt, status, badge, stat, stats, tiles, table, lineChart, barChart, sparkline, section, note, md, PALETTE,
    /** Read-only operation declared in widget.json (sources or uses). */
    read: (integration, op, params = {}) => call("read", { integration, op, params }),
    /** Action declared in widget.json "uses": the user sees the confirm dialog; resolves with the result or null. */
    action: (integration, op, params = {}) => call("action", { integration, op, params }),
    /** Open the assistant with a question (widget context is attached). */
    ask: (text) => post({ type: "ask", text: String(text || "") }),
    refresh: () => post({ type: "refresh" }),
    /** Ask the host to re-run sources with new input values. */
    setInputs: (inputs) => post({ type: "inputs", inputs }),
    /** Convert prometheus.query_range output to lineChart series. */
    promSeries: (rows, label) => (rows || []).map((r) => ({
      name: label ? (r.labels?.[label] ?? r.series) : r.series, points: r.points || [] })),
  };
  window.sdk = sdk;

  function report(err) {
    post({ type: "error", message: String((err && err.message) || err), stack: String((err && err.stack) || "").slice(0, 2000) });
  }
  window.addEventListener("error", (e) => report(e.error || e.message));
  window.addEventListener("unhandledrejection", (e) => report(e.reason));

  async function doRender(msg) {
    last = msg;
    const root = document.getElementById("root");
    root.replaceChildren();
    if (typeof window.render !== "function") { report(new Error("view.js does not define render(ctx)")); return; }
    try {
      await window.render({ root, data: msg.data, inputs: msg.inputs || {}, errors: msg.errors || {},
        demo: msg.demo || [], widget: msg.widget || {}, sdk });
      post({ type: "rendered" });
    } catch (err) {
      root.replaceChildren(h("div", { class: "od-note crit" }, `Widget error: ${err.message || err}`));
      report(err);
    }
  }

  window.addEventListener("message", (e) => {
    if (e.source !== parent) return;
    const msg = e.data || {};
    if (msg.type === "render") doRender(msg);
    else if (msg.type === "result" && pending.has(msg.id)) {
      const p = pending.get(msg.id); pending.delete(msg.id);
      msg.ok ? p.resolve(msg.data) : p.reject(new Error(msg.error || "failed"));
    }
  });

  // Report the content's own height (not the viewport's) so the host frame can grow and shrink to fit.
  const ro = new ResizeObserver(() => {
    const root = document.getElementById("root");
    post({ type: "height", h: Math.ceil(root.getBoundingClientRect().height) + 2 });
  });
  window.addEventListener("DOMContentLoaded", () => { ro.observe(document.getElementById("root")); post({ type: "ready" }); });
  void last;
})();
