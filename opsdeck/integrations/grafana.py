"""Grafana: dashboards, Grafana-managed alerts and annotations (deploy markers).

GRAFANA_URL and GRAFANA_TOKEN (a service account token with Viewer role).
"""

from __future__ import annotations

import time

from ..config import env
from ..registry import Column, Integration, Param, Section, read_op
from ._common import ago, check, http_client, iso


class Grafana(Integration):
    name = "grafana"
    title = "Grafana"
    category = "Observability"
    icon = "chart"
    description = "Dashboards, Grafana-managed alerts and annotations."
    required_env = ("GRAFANA_URL", "GRAFANA_TOKEN")

    def _client(self):
        return http_client(self.settings, env("GRAFANA_URL"), token=env("GRAFANA_TOKEN"))

    def health(self):
        with self._client() as c:
            d = check(c.get("/api/health")).json()
        return {"ok": True, "detail": f"Grafana {d.get('version', '?')}"}

    def _url(self, path: str) -> str:
        return env("GRAFANA_URL", "").rstrip("/") + path

    @read_op("Search dashboards by title.", (Param("query", default=""), Param("limit", "integer", default=30)))
    def dashboards(self, query="", limit=30):
        with self._client() as c:
            items = check(c.get("/api/search", params={"type": "dash-db", "query": query, "limit": limit})).json()
        return [{"title": d["title"], "folder": d.get("folderTitle", "General"), "tags": ", ".join(d.get("tags", [])),
                 "uid": d.get("uid"), "url": self._url(d.get("url", ""))} for d in items]

    @read_op("Grafana-managed alert instances that are firing or pending.", (
        Param("state", enum=["firing", "pending", "all"], default="firing"),
    ))
    def alerts(self, state="firing"):
        with self._client() as c:
            d = check(c.get("/api/prometheus/grafana/api/v1/alerts")).json().get("data", {})
        rows = [{"alert": a["labels"].get("alertname"), "state": (a.get("state") or "").lower().replace("alerting", "firing"),
                 "severity": a["labels"].get("severity", ""), "folder": a["labels"].get("grafana_folder", ""),
                 "summary": (a.get("annotations") or {}).get("summary", ""), "since": iso(a.get("activeAt"))}
                for a in d.get("alerts", [])]
        return rows if state == "all" else [r for r in rows if r["state"] == state]

    @read_op("Annotations (deploys, incidents) in the last N hours.", (
        Param("hours", "integer", default=24), Param("tags", description="Comma-separated tags, e.g. deploy"),
    ))
    def annotations(self, hours=24, tags=None):
        now = int(time.time() * 1000)
        params = {"from": now - hours * 3600 * 1000, "to": now, "limit": 200}
        if tags:
            params["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        with self._client() as c:
            items = check(c.get("/api/annotations", params=params)).json()
        return [{"time": iso(a.get("time")), "text": a.get("text", "")[:300], "tags": ", ".join(a.get("tags") or []),
                 "dashboard": a.get("dashboardUID")} for a in items]

    def widget(self):
        return [
            Section("Grafana alerts", "alerts", inputs=["state"], params={"state": "firing"}, refresh_secs=60,
                    columns=[Column("alert"), Column("state", fmt="status"), Column("severity", fmt="status"),
                             Column("folder"), Column("summary"), Column("since", fmt="time")]),
            Section("Annotations", "annotations", inputs=["hours", "tags"], params={"hours": 24}, width="half",
                    columns=[Column("time", fmt="time"), Column("text"), Column("tags")]),
            Section("Dashboards", "dashboards", inputs=["query"], params={"query": ""}, width="half",
                    columns=[Column("title"), Column("folder"), Column("tags"), Column("url", fmt="link")]),
        ]

    def demo_dashboards(self, query="", limit=30):
        rows = [("Payments / Overview", "Payments", "payments, slo", "pay1"), ("Checkout SLOs", "Checkout", "slo", "chk1"),
                ("Kubernetes / Workloads", "Platform", "k8s", "k8s1"), ("Kafka consumers", "Platform", "kafka", "kaf1")]
        return [{"title": t, "folder": f, "tags": tg, "uid": u, "url": f"https://grafana.acme.com/d/{u}"}
                for t, f, tg, u in rows if query.lower() in t.lower()][:limit]

    def demo_alerts(self, state="firing"):
        rows = [{"alert": "Payments settlement lag", "state": "firing", "severity": "critical", "folder": "Payments",
                 "summary": "Lag 21m (threshold 15m)", "since": ago(minutes=40)},
                {"alert": "Checkout availability SLO burn (1h/5m)", "state": "firing", "severity": "warning",
                 "folder": "Checkout", "summary": "14.4x burn rate", "since": ago(minutes=35)}]
        return rows if state == "all" else [r for r in rows if r["state"] == state]

    def demo_annotations(self, hours=24, tags=None):
        return [{"time": ago(hours=2.1), "text": "Deploy payments-worker 1.42.0 (argo: payments-prod)",
                 "tags": "deploy, payments", "dashboard": "pay1"},
                {"time": ago(hours=20), "text": "Deploy checkout-web 3.8.1", "tags": "deploy, checkout", "dashboard": "chk1"}]
