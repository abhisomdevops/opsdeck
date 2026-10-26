function render({ root, data, sdk, errors }) {
  const { h, stats, table } = sdk;
  const c = data.counts || {};
  root.append(stats([
    { label: "Open incidents", value: c.incidents ?? 0, status: c.incidents ? "warn" : "ok" },
    { label: "Unacknowledged", value: c.unacked ?? 0, status: c.unacked ? "crit" : "ok" },
    { label: "Critical signals", value: c.critical ?? 0, status: c.critical ? "crit" : "ok" },
    { label: "Firing alerts", value: c.alerts ?? 0, status: c.alerts ? "warn" : "ok" },
  ]));
  const failed = Object.entries(errors || {});
  if (failed.length) root.append(sdk.note(`Some sources failed: ${failed.map(([k, v]) => `${k}: ${v}`).join("; ")}`, "warn"));
  root.append(sdk.section("Everything that is on fire",
    table(data.items || [], {
      empty: "All quiet.",
      columns: [
        { key: "severity", fmt: "status" }, { key: "source" }, { key: "title" }, { key: "service" },
        { key: "state", fmt: "status" }, { key: "owner" }, { key: "since", fmt: "time" },
      ],
      actions: [
        { label: "Investigate", onClick: (r) => sdk.ask(`Investigate: ${r.title} (${r.source} ${r.ref}). Find the root cause.`) },
        { label: "Ack", kind: "act", when: (r) => r.source === "PagerDuty" && r.state === "triggered",
          onClick: async (r) => { if (await sdk.action("pagerduty", "acknowledge", { id: r.ref })) sdk.refresh(); } },
      ],
    })));
}
