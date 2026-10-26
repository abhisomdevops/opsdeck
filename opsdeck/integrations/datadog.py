"""Datadog: monitors, metrics, logs and APM spans.

DD_API_KEY + DD_APP_KEY (an application key scoped to monitors_read,
metrics_read, logs_read_data, apm_read). DD_SITE: datadoghq.com (default),
us3.datadoghq.com, us5.datadoghq.com, datadoghq.eu, ap1.datadoghq.com, ddog-gov.com.
"""

from __future__ import annotations

import datetime as dt
import time

from ..config import env
from ..registry import Column, Integration, Param, Section, read_op
from ._common import ago, check, http_client, iso
from .prometheus import demo_series


class Datadog(Integration):
    name = "datadog"
    title = "Datadog"
    category = "Observability"
    icon = "dog"
    description = "Monitors, metrics, logs and APM traces."
    required_env = ("DD_API_KEY", "DD_APP_KEY")

    def _client(self):
        return http_client(self.settings, f"https://api.{env('DD_SITE', 'datadoghq.com')}",
                           headers={"DD-API-KEY": env("DD_API_KEY"), "DD-APPLICATION-KEY": env("DD_APP_KEY")})

    def health(self):
        with self._client() as c:
            check(c.get("/api/v1/validate"))
        return {"ok": True, "detail": f"site {env('DD_SITE', 'datadoghq.com')}"}

    @read_op("Monitors, alerting first.", (
        Param("state", enum=["Alert", "Warn", "No Data", "OK"], description="Only this state"),
        Param("tags", description="Comma-separated monitor tags, e.g. team:payments"),
    ))
    def monitors(self, state=None, tags=None):
        params = {"page_size": 200}
        if tags:
            params["monitor_tags"] = tags
        with self._client() as c:
            items = check(c.get("/api/v1/monitor", params=params)).json()
        rows = [{"id": m["id"], "name": m.get("name"), "state": m.get("overall_state"), "type": m.get("type"),
                 "tags": ", ".join(m.get("tags") or [])[:200], "query": (m.get("query") or "")[:300],
                 "url": f"https://app.{env('DD_SITE', 'datadoghq.com')}/monitors/{m['id']}"} for m in items]
        if state:
            rows = [r for r in rows if r["state"] == state]
        order = {"Alert": 0, "Warn": 1, "No Data": 2}
        return sorted(rows, key=lambda r: order.get(r["state"], 3))

    @read_op("Metric timeseries query over the last N minutes.", (
        Param("query", required=True, description="e.g. avg:trace.http.request.duration{service:checkout-web} by {env}"),
        Param("minutes", "integer", default=60),
    ))
    def metrics(self, query, minutes=60):
        now = int(time.time())
        with self._client() as c:
            d = check(c.get("/api/v1/query", params={"from": now - minutes * 60, "to": now, "query": query})).json()
        out = []
        for s in d.get("series", [])[:20]:
            pts = [[int(t / 1000), v] for t, v in s.get("pointlist", []) if v is not None]
            vals = [v for _, v in pts]
            out.append({"series": s.get("expression") or s.get("scope"), "min": min(vals) if vals else None,
                        "max": max(vals) if vals else None, "last": vals[-1] if vals else None, "points": pts[-120:]})
        return out

    @read_op("Search logs.", (
        Param("query", required=True, description="Datadog log search, e.g. service:payments-worker status:error"),
        Param("minutes", "integer", default=60), Param("limit", "integer", default=100),
    ))
    def logs(self, query, minutes=60, limit=100):
        body = {"filter": {"query": query, "from": f"now-{minutes}m", "to": "now"},
                "sort": "-timestamp", "page": {"limit": min(limit, 1000)}}
        with self._client() as c:
            d = check(c.post("/api/v2/logs/events/search", json=body)).json()
        return [{"time": iso(e["attributes"].get("timestamp")), "service": e["attributes"].get("service"),
                 "status": e["attributes"].get("status"), "host": e["attributes"].get("host"),
                 "message": (e["attributes"].get("message") or "")[:2000]} for e in d.get("data", [])]

    @read_op("Search APM spans (e.g. errors or slow requests).", (
        Param("query", required=True, description="e.g. service:checkout-web status:error"),
        Param("minutes", "integer", default=60), Param("limit", "integer", default=50),
    ))
    def spans(self, query, minutes=60, limit=50):
        body = {"data": {"type": "search_request", "attributes": {
            "filter": {"query": query, "from": f"now-{minutes}m", "to": "now"}, "sort": "-timestamp",
            "page": {"limit": min(limit, 1000)}}}}
        with self._client() as c:
            d = check(c.post("/api/v2/spans/events/search", json=body)).json()
        rows = []
        for e in d.get("data", []):
            a = e.get("attributes", {})
            start, end = a.get("start_timestamp"), a.get("end_timestamp")
            dur = None
            if start and end:
                try:
                    dur = round((dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                                 - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000, 1)
                except ValueError:
                    dur = None
            rows.append({"time": iso(start), "service": a.get("service"), "resource": a.get("resource_name"),
                         "duration_ms": dur, "status": (a.get("custom") or {}).get("status") or a.get("status"),
                         "trace_id": a.get("trace_id"), "error": ((a.get("custom") or {}).get("error") or {}).get("message", "")})
        return rows

    def widget(self):
        return [
            Section("Monitors", "monitors", inputs=["state", "tags"], refresh_secs=60,
                    columns=[Column("name"), Column("state", fmt="status"), Column("type"), Column("tags"),
                             Column("url", fmt="link")]),
            Section("Error logs", "logs", inputs=["query", "minutes"], params={"query": "status:error", "minutes": 60},
                    columns=[Column("time", fmt="time"), Column("service"), Column("status", fmt="status"),
                             Column("message", fmt="code")]),
        ]

    def attention(self):
        alerting = self.invoke("monitors", {"state": "Alert", "tags": None})
        if not alerting:
            return []
        return [{"severity": "critical", "title": f"{len(alerting)} Datadog monitor(s) alerting",
                 "detail": ", ".join(m["name"] for m in alerting[:4])}]

    # ------------------------------------------------------------- demo
    def demo_monitors(self, state=None, tags=None):
        rows = [(101, "checkout-web p99 latency > 2s", "Alert", "query alert", "team:checkout, env:prod"),
                (102, "payments-api error rate", "Warn", "query alert", "team:payments, env:prod"),
                (103, "RDS ledger-db CPU", "OK", "metric alert", "team:payments"),
                (104, "search-api synthetic", "No Data", "synthetics alert", "team:search")]
        out = [{"id": i, "name": n, "state": s, "type": t, "tags": tg, "query": "",
                "url": f"https://app.datadoghq.com/monitors/{i}"} for i, n, s, t, tg in rows]
        return [r for r in out if not state or r["state"] == state]

    def demo_metrics(self, query, minutes=60):
        pts = demo_series(0.3 if "duration" in query else 120, minutes, max(15, minutes * 60 // 120), 40, 6.0)
        vals = [v for _, v in pts]
        return [{"series": query, "min": min(vals), "max": max(vals), "last": vals[-1], "points": pts}]

    def demo_logs(self, query, minutes=60, limit=100):
        return [{"time": ago(minutes=m), "service": s, "status": st, "host": "i-0abc", "message": msg}
                for m, s, st, msg in [
                    (2, "checkout-web", "error", "POST /api/pay 502: upstream payments-api timeout after 3000ms"),
                    (3, "payments-api", "warn", "kafka producer queue full, retrying (settlements)"),
                    (6, "payments-worker", "error", "missing required env PAYMENTS_DB_DSN")]][:limit]

    def demo_spans(self, query, minutes=60, limit=50):
        return [{"time": ago(minutes=12), "service": "checkout-web", "resource": "POST /api/pay", "duration_ms": 3012.4,
                 "status": "error", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "error": "upstream timeout"}][:limit]
