// Prometheus returns one series per label set; pick the one for this service (or the only one).
function pick(rows, service) {
  rows = rows || [];
  return rows.find((r) => (r.labels || {}).service === service) || rows[0] || null;
}

function render({ root, data, inputs, sdk }) {
  const svc = inputs.service;
  const err = pick(data.errors, svc), lat = pick(data.latency, svc), rps = pick(data.traffic, svc);
  const SLO_ERR = 0.005, SLO_P99 = 1.0;
  const st = (v, limit) => (v == null ? "" : v > limit * 2 ? "crit" : v > limit ? "warn" : "ok");
  root.append(sdk.stats([
    { label: "5xx ratio", value: err ? sdk.fmt.pct(err.last, 2) : "–", status: st(err?.last, SLO_ERR),
      sub: `SLO ${sdk.fmt.pct(SLO_ERR, 1)}`, spark: err?.points },
    { label: "p99 latency", value: lat ? sdk.fmt.ms(lat.last * 1000) : "–", status: st(lat?.last, SLO_P99),
      sub: `target ${SLO_P99}s`, spark: lat?.points },
    { label: "Traffic", value: rps ? sdk.fmt.num(rps.last) : "–", unit: "req/s", spark: rps?.points },
  ]));
  if (err) root.append(sdk.lineChart([{ name: "5xx ratio", points: err.points }], {
    title: "Error ratio", height: 150, yFormat: (v) => sdk.fmt.pct(v, 1), area: true,
    thresholds: [{ value: SLO_ERR, label: "SLO", status: "warn" }] }));
  if (lat) root.append(sdk.lineChart([{ name: "p99", points: lat.points }], {
    title: "p99 latency (s)", height: 150, thresholds: [{ value: SLO_P99, label: "target", status: "warn" }] }));
  if (!err && !lat && !rps) root.append(sdk.note(`No metrics for service "${svc}".`, "warn"));
  else if ((err && err.last > SLO_ERR) || (lat && lat.last > SLO_P99)) {
    root.append(sdk.h("p", {}, sdk.h("button", { class: "od-btn", onclick: () => sdk.ask(
      `${svc} is outside its SLO (5xx ${sdk.fmt.pct(err?.last, 2)}, p99 ${lat ? lat.last.toFixed(2) + "s" : "n/a"}). ` +
      "Find what changed and the root cause.") }, "Ask the assistant why")));
  }
}
