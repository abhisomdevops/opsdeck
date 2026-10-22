"""Argo CD: application sync/health status and sync action.

Auth: an Argo CD API token (create a local account with apiKey capability,
e.g. `argocd account generate-token --account opsdeck`), scoped with Argo CD
RBAC to the projects this console should see.
"""

from __future__ import annotations

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, action, read_op
from ._common import ago, check, http_client


class ArgoCD(Integration):
    name = "argocd"
    title = "Argo CD"
    category = "Delivery"
    icon = "argo"
    description = "Application sync and health across Argo CD projects."
    required_env = ("ARGOCD_URL", "ARGOCD_TOKEN")

    def _client(self):
        return http_client(self.settings, env("ARGOCD_URL"), token=env("ARGOCD_TOKEN"))

    def health(self):
        with self._client() as c:
            v = check(c.get("/api/version")).json()
        return {"ok": True, "detail": f"Argo CD {v.get('Version', '?')}"}

    @read_op("List Argo CD applications with sync and health status.", (
        Param("project", description="Filter by project"),
        Param("problems_only", "boolean", "Only OutOfSync or not Healthy", default=False),
    ))
    def applications(self, project=None, problems_only=False):
        params = {"projects": project} if project else {}
        with self._client() as c:
            items = check(c.get("/api/v1/applications", params=params)).json().get("items") or []
        rows = []
        for a in items:
            st = a.get("status", {})
            spec = a.get("spec", {})
            src = spec.get("source") or (spec.get("sources") or [{}])[0]
            op = st.get("operationState", {})
            rows.append({
                "name": a["metadata"]["name"], "project": spec.get("project"),
                "sync": st.get("sync", {}).get("status", "Unknown"),
                "health": st.get("health", {}).get("status", "Unknown"),
                "revision": (st.get("sync", {}).get("revision") or "")[:12],
                "destination": f"{spec.get('destination', {}).get('name') or spec.get('destination', {}).get('server', '')}"
                               f"/{spec.get('destination', {}).get('namespace', '')}",
                "repo": src.get("repoURL", ""), "path": src.get("path") or src.get("chart", ""),
                "last_sync": op.get("finishedAt"), "last_op": op.get("phase"),
            })
        if problems_only:
            rows = [r for r in rows if r["sync"] != "Synced" or r["health"] != "Healthy"]
        return sorted(rows, key=lambda r: (r["sync"] == "Synced" and r["health"] == "Healthy", r["name"]))

    @read_op("Resources of one application that are out of sync or unhealthy.", (
        Param("name", required=True),
    ))
    def app_resources(self, name):
        with self._client() as c:
            a = check(c.get(f"/api/v1/applications/{name}")).json()
        out = []
        for r in a.get("status", {}).get("resources", []) or []:
            health = (r.get("health") or {}).get("status", "")
            if r.get("status") != "Synced" or health not in ("", "Healthy"):
                out.append({"kind": r.get("kind"), "namespace": r.get("namespace"), "name": r.get("name"),
                            "sync": r.get("status"), "health": health or "-",
                            "message": (r.get("health") or {}).get("message", "")})
        return out

    @action("Trigger a sync of an Argo CD application.", (
        Param("name", required=True),
        Param("prune", "boolean", "Delete resources no longer in git", default=False),
    ), risk="medium")
    def sync(self, name, prune=False):
        with self._client() as c:
            res = check(c.post(f"/api/v1/applications/{name}/sync", json={"prune": prune})).json()
        return {"sync_started": name, "phase": res.get("status", {}).get("operationState", {}).get("phase")}

    def describe_action(self, op, params):
        if op == "sync":
            return f"Argo CD sync of application '{params['name']}'" + (" WITH PRUNE" if params.get("prune") else "")
        return super().describe_action(op, params)

    def widget(self):
        return [Section(
            "Applications", "applications", inputs=["project", "problems_only"],
            columns=[Column("name", fmt="code"), Column("project"), Column("sync", fmt="status"),
                     Column("health", fmt="status"), Column("revision", fmt="code"), Column("destination"),
                     Column("last_sync", fmt="time"), Column("last_op", fmt="status")],
            row_actions=[RowAction("app_resources", "Diff", {"name": "name"}),
                         RowAction("sync", "Sync", {"name": "name"}),
                         RowAction("sync", "Sync+prune", {"name": "name"}, params={"prune": True})],
            refresh_secs=60)]

    def attention(self):
        bad = self.invoke("applications", {"project": None, "problems_only": True})
        if not bad:
            return []
        degraded = [a for a in bad if a["health"] in ("Degraded", "Missing")]
        return [{"severity": "critical" if degraded else "warning",
                 "title": f"{len(bad)} Argo CD app(s) out of sync or unhealthy",
                 "detail": ", ".join(f"{a['name']} ({a['sync']}/{a['health']})" for a in bad[:6])}]

    # -------------------------------------------------------------- demo
    _APPS = [
        ("payments-prod", "payments", "Synced", "Healthy", "prod/payments"),
        ("checkout-prod", "checkout", "OutOfSync", "Healthy", "prod/checkout"),
        ("search-prod", "search", "Synced", "Degraded", "prod/search"),
        ("observability", "platform", "Synced", "Progressing", "prod/monitoring"),
        ("payments-staging", "payments", "Synced", "Healthy", "staging/payments"),
    ]

    def demo_applications(self, project=None, problems_only=False):
        rows = [{"name": n, "project": p, "sync": s, "health": h, "revision": "7d1e0f3a9b21",
                 "destination": f"in-cluster/{d.split('/')[1]}", "repo": "https://github.com/acme/deploy",
                 "path": d, "last_sync": ago(minutes=15 * (i + 1)),
                 "last_op": "Failed" if h == "Degraded" else "Succeeded"}
                for i, (n, p, s, h, d) in enumerate(self._APPS) if not project or p == project]
        if problems_only:
            rows = [r for r in rows if r["sync"] != "Synced" or r["health"] != "Healthy"]
        return sorted(rows, key=lambda r: (r["sync"] == "Synced" and r["health"] == "Healthy", r["name"]))

    def demo_app_resources(self, name):
        if name == "checkout-prod":
            return [{"kind": "ConfigMap", "namespace": "checkout", "name": "checkout-web-config",
                     "sync": "OutOfSync", "health": "-", "message": ""}]
        if name == "search-prod":
            return [{"kind": "Deployment", "namespace": "search", "name": "search-api", "sync": "Synced",
                     "health": "Degraded", "message": "Deployment exceeded its progress deadline"}]
        return []

    def demo_sync(self, name, prune=False):
        return {"sync_started": name, "phase": "Running", "demo": True}
