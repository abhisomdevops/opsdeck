function render({ root, data, sdk }) {
  const { h, tiles, table, section } = sdk;
  const clusters = data.clusters || [];
  root.append(section("Clusters", tiles(clusters.map((c) => ({
    label: c.name, value: c.state, status: c.state === "active" ? "ok" : "warn",
    sub: `${c.version || ""} · ${c.nodes ?? "?"} nodes · cpu ${c.cpu}`,
  })))));
  root.append(section("Degraded workloads", table(data.workloads || [], {
    empty: "All workloads at desired replicas.",
    columns: [{ key: "cluster" }, { key: "namespace" }, { key: "name", fmt: "code" }, { key: "ready" }, { key: "status", fmt: "status" }],
    actions: [{ label: "Restart", kind: "act", onClick: async (r) => {
      const res = await sdk.action("rancher", "restart", { cluster: r.cluster_id, namespace: r.namespace, name: r.name, kind: r.kind });
      if (res) sdk.refresh();
    } }],
  })));
  const logBox = h("div");
  root.append(section("Unhealthy pods (local cluster)", table(data.pods || [], {
    empty: "No unhealthy pods.",
    columns: [{ key: "namespace" }, { key: "name", fmt: "code" }, { key: "status", fmt: "status" }, { key: "restarts", fmt: "num" }],
    actions: [{ label: "Logs", onClick: async (p) => {
      logBox.replaceChildren(sdk.note("Loading previous container logs…"));
      try {
        const res = await sdk.read("kubernetes", "pod_logs", { namespace: p.namespace, pod: p.name, previous: true, tail: 40 });
        logBox.replaceChildren(h("pre", { class: "od-code", style: { whiteSpace: "pre-wrap", maxHeight: "220px", overflow: "auto" } }, res.log || "(empty)"));
      } catch (e) { logBox.replaceChildren(sdk.note(e.message, "crit")); }
    } }],
  })), logBox);
}
