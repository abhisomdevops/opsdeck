"""Rancher: every downstream cluster's workloads through one API, plus restart.

RANCHER_URL (https://rancher.acme.com) and RANCHER_TOKEN (an API key,
"token-xxxxx:yyyy", scoped to the clusters this console should see). The
Kubernetes API of each managed cluster is reached via Rancher's proxy at
/k8s/clusters/<id>, so Rancher RBAC (project/cluster roles) applies.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from ..config import env
from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago, check, http_client, iso

KINDS = {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet"}


class Rancher(Integration):
    name = "rancher"
    title = "Rancher"
    category = "Runtime"
    icon = "k8s"
    description = "Clusters and workloads across every Rancher-managed Kubernetes cluster."
    required_env = ("RANCHER_URL", "RANCHER_TOKEN")

    def _client(self):
        return http_client(self.settings, env("RANCHER_URL"), token=env("RANCHER_TOKEN"))

    def health(self):
        with self._client() as c:
            n = len(check(c.get("/v3/clusters")).json().get("data", []))
        return {"ok": True, "detail": f"{n} cluster(s)"}

    def _cluster_ids(self, cluster: str | None) -> list[tuple[str, str]]:
        clusters = [(c["id"], c["name"]) for c in self.invoke("clusters", {})]
        if not cluster:
            return clusters
        hit = [x for x in clusters if cluster in x]
        if not hit:
            raise OpError(f"unknown Rancher cluster '{cluster}'", 404)
        return hit

    @read_op("Managed clusters with state, Kubernetes version, nodes and capacity.")
    def clusters(self):
        with self._client() as c:
            data = check(c.get("/v3/clusters")).json().get("data", [])
        rows = []
        for cl in data:
            alloc, req = cl.get("allocatable") or {}, cl.get("requested") or {}
            rows.append({"id": cl["id"], "name": cl.get("name"), "state": cl.get("state"),
                         "provider": cl.get("provider") or cl.get("driver"),
                         "version": (cl.get("version") or {}).get("gitVersion"), "nodes": cl.get("nodeCount"),
                         "cpu": f"{req.get('cpu', '?')}/{alloc.get('cpu', '?')}",
                         "memory": f"{req.get('memory', '?')}/{alloc.get('memory', '?')}",
                         "created": iso(cl.get("created"))})
        return rows

    def _workloads_one(self, cid: str, cname: str, namespace: str | None, kinds: list[str]) -> list[dict]:
        rows = []
        with self._client() as c:
            for kind in kinds:
                path = (f"/k8s/clusters/{cid}/apis/apps/v1/namespaces/{namespace}/{kind}" if namespace
                        else f"/k8s/clusters/{cid}/apis/apps/v1/{kind}")
                for w in check(c.get(path)).json().get("items", []):
                    st, sp = w.get("status") or {}, w.get("spec") or {}
                    if kind == "daemonsets":
                        desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
                    else:
                        desired, ready = sp.get("replicas", 1), st.get("readyReplicas", 0)
                    images = ", ".join(x["image"] for x in sp.get("template", {}).get("spec", {}).get("containers", []))
                    rows.append({"cluster": cname, "cluster_id": cid, "namespace": w["metadata"]["namespace"],
                                 "kind": KINDS[kind], "name": w["metadata"]["name"],
                                 "status": "Ready" if ready >= desired else ("ScaledDown" if desired == 0 else "Degraded"),
                                 "ready": f"{ready}/{desired}", "image": images})
        return rows

    @read_op("Workloads (deployments, statefulsets, daemonsets) across clusters.", (
        Param("cluster", description="Cluster id or name; empty for all"), Param("namespace"),
        Param("problems_only", "boolean", default=False),
    ))
    def workloads(self, cluster=None, namespace=None, problems_only=False):
        targets = self._cluster_ids(cluster)
        with ThreadPoolExecutor(max_workers=8) as pool:
            parts = list(pool.map(lambda x: self._workloads_one(x[0], x[1], namespace, list(KINDS)), targets[:30]))
        rows = [r for p in parts for r in p]
        if problems_only:
            rows = [r for r in rows if r["status"] == "Degraded"]
        return sorted(rows, key=lambda r: (r["status"] == "Ready", r["cluster"], r["namespace"], r["name"]))

    @read_op("Pods in one cluster/namespace with status and restarts.", (
        Param("cluster", required=True), Param("namespace", required=True),
    ))
    def pods(self, cluster, namespace):
        cid, cname = self._cluster_ids(cluster)[0]
        with self._client() as c:
            items = check(c.get(f"/k8s/clusters/{cid}/api/v1/namespaces/{namespace}/pods")).json().get("items", [])
        rows = []
        for p in items:
            cs = (p.get("status") or {}).get("containerStatuses") or []
            waiting = next((s["state"]["waiting"].get("reason") for s in cs if (s.get("state") or {}).get("waiting")), None)
            rows.append({"cluster": cname, "namespace": namespace, "name": p["metadata"]["name"],
                         "status": waiting or (p.get("status") or {}).get("phase"),
                         "ready": f"{sum(1 for s in cs if s.get('ready'))}/{len(cs)}",
                         "restarts": sum(s.get("restartCount", 0) for s in cs),
                         "node": (p.get("spec") or {}).get("nodeName")})
        return rows

    @action("Rolling restart of a workload in a Rancher-managed cluster.", (
        Param("cluster", required=True), Param("namespace", required=True), Param("name", required=True),
        Param("kind", enum=list(KINDS.values()), default="Deployment"),
    ), risk="medium")
    def restart(self, cluster, namespace, name, kind="Deployment"):
        cid, cname = self._cluster_ids(cluster)[0]
        plural = next(k for k, v in KINDS.items() if v == kind)
        now = dt.datetime.now(dt.UTC).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        with self._client() as c:
            check(c.patch(f"/k8s/clusters/{cid}/apis/apps/v1/namespaces/{namespace}/{plural}/{name}", json=body,
                          headers={"Content-Type": "application/strategic-merge-patch+json"}))
        return {"restarted": f"{cname}/{namespace}/{kind}/{name}", "at": now}

    def describe_action(self, op, params):
        if op == "restart":
            return (f"Rolling restart of {params.get('kind') or 'Deployment'} {params['namespace']}/{params['name']} "
                    f"in Rancher cluster {params['cluster']}")
        return super().describe_action(op, params)

    def widget(self):
        return [
            Section("Clusters", "clusters", refresh_secs=120,
                    columns=[Column("name", fmt="code"), Column("state", fmt="status"), Column("provider"),
                             Column("version"), Column("nodes", fmt="num"), Column("cpu"), Column("memory")]),
            Section("Workloads", "workloads", inputs=["cluster", "namespace", "problems_only"], refresh_secs=60,
                    columns=[Column("cluster"), Column("namespace"), Column("kind"), Column("name", fmt="code"),
                             Column("status", fmt="status"), Column("ready"), Column("image", fmt="code")],
                    row_actions=[RowAction("pods", "Pods", {"cluster": "cluster_id", "namespace": "namespace"}),
                                 RowAction("restart", "Restart", {"cluster": "cluster_id", "namespace": "namespace",
                                                                  "name": "name", "kind": "kind"})]),
        ]

    def attention(self):
        bad = self.invoke("workloads", {"cluster": None, "namespace": None, "problems_only": True})
        down = [c for c in self.invoke("clusters", {}) if c["state"] != "active"]
        out = []
        if down:
            out.append({"severity": "critical", "title": f"{len(down)} Rancher cluster(s) not active",
                        "detail": ", ".join(f"{c['name']} ({c['state']})" for c in down)})
        if bad:
            out.append({"severity": "warning", "title": f"{len(bad)} degraded workload(s) across Rancher clusters",
                        "detail": ", ".join(f"{r['cluster']}/{r['namespace']}/{r['name']}" for r in bad[:5])})
        return out

    # ------------------------------------------------------------- demo
    _CLUSTERS = [("c-m-prod1", "prod-us-east-1", "active", "eks", "v1.30.4", 12),
                 ("c-m-prod2", "prod-eu-west-1", "active", "eks", "v1.30.4", 8),
                 ("c-m-stg", "staging", "updating", "rke2", "v1.31.1+rke2r1", 4)]

    def demo_clusters(self):
        return [{"id": i, "name": n, "state": s, "provider": p, "version": v, "nodes": nd,
                 "cpu": f"{nd * 3}/{nd * 8}", "memory": f"{nd * 11}Gi/{nd * 32}Gi", "created": ago(days=300)}
                for i, n, s, p, v, nd in self._CLUSTERS]

    def demo_workloads(self, cluster=None, namespace=None, problems_only=False):
        rows = [("c-m-prod1", "prod-us-east-1", "payments", "Deployment", "payments-worker", "Degraded", "0/1",
                 "registry.example.com/payments-worker:1.42.0"),
                ("c-m-prod1", "prod-us-east-1", "payments", "Deployment", "payments-api", "Ready", "2/2",
                 "registry.example.com/payments-api:1.42.0"),
                ("c-m-prod2", "prod-eu-west-1", "payments", "Deployment", "payments-worker", "Ready", "1/1",
                 "registry.example.com/payments-worker:1.41.3"),
                ("c-m-prod2", "prod-eu-west-1", "checkout", "Deployment", "checkout-web", "Ready", "3/3",
                 "registry.example.com/checkout-web:3.8.1"),
                ("c-m-stg", "staging", "search", "StatefulSet", "search-indexer", "Degraded", "1/2",
                 "registry.example.com/search-indexer:2.0.0-rc1"),
                ("c-m-prod1", "prod-us-east-1", "kube-system", "DaemonSet", "aws-node", "Ready", "12/12",
                 "amazon-k8s-cni:v1.18.3")]
        out = [{"cluster": cn, "cluster_id": ci, "namespace": ns, "kind": k, "name": n, "status": s, "ready": r, "image": i}
               for ci, cn, ns, k, n, s, r, i in rows
               if (not cluster or cluster in (ci, cn)) and (not namespace or ns == namespace)]
        if problems_only:
            out = [r for r in out if r["status"] == "Degraded"]
        return sorted(out, key=lambda r: (r["status"] == "Ready", r["cluster"], r["namespace"], r["name"]))

    def demo_pods(self, cluster, namespace):
        return [{"cluster": cluster, "namespace": namespace, "name": f"{namespace}-worker-5d4b9c7f8-hq2nn",
                 "status": "CrashLoopBackOff", "ready": "0/1", "restarts": 14, "node": "ip-10-0-2-40"}]

    def demo_restart(self, cluster, namespace, name, kind="Deployment"):
        return {"restarted": f"{cluster}/{namespace}/{kind}/{name}", "at": ago(seconds=0), "demo": True}
