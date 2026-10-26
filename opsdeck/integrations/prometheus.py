"""Prometheus-compatible metrics (Prometheus, Mimir, Thanos, VictoriaMetrics,
Grafana Cloud, Amazon Managed Prometheus via a SigV4 proxy) and Alertmanager.

PROMETHEUS_URL: base URL that serves /api/v1/query (for Mimir include the
/prometheus prefix). Auth: PROMETHEUS_TOKEN (bearer) or PROMETHEUS_USER +
PROMETHEUS_PASSWORD (basic). PROMETHEUS_ORG_ID sets X-Scope-OrgID for
multi-tenant Mimir/Cortex. ALERTMANAGER_URL (optional) adds silences-aware
alert listing from Alertmanager.
"""

from __future__ import annotations

import math
import time

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, read_op
from ._common import ago, check, http_client, iso

MAX_SERIES = 20
MAX_POINTS = 120


def _label_str(metric: dict) -> str:
    name = metric.get("__name__", "")
    labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{labels}}}" if labels else name or "{}"


def _summary(a: dict) -> str:
    ann = a.get("annotations") or {}
    return ann.get("summary") or ann.get("description", "")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def summarize_matrix(result: list, max_series: int = MAX_SERIES, max_points: int = MAX_POINTS) -> list[dict]:
    out = []
    for s in result[:max_series]:
        pts = [(int(float(t)), _num(v)) for t, v in s.get("values", [])]
        pts = [(t, v) for t, v in pts if v is not None]
        if len(pts) > max_points:  # downsample evenly, always keep the last point
            stride = len(pts) / max_points
            pts = [pts[int(i * stride)] for i in range(max_points - 1)] + [pts[-1]]
        vals = [v for _, v in pts]
        out.append({"series": _label_str(s.get("metric", {})), "labels": s.get("metric", {}),
                    "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                    "last": vals[-1] if vals else None, "points": [[t, round(v, 6)] for t, v in pts]})
    return out


def demo_series(base: float, minutes: int, step_s: int, change_at_min: float | None = None, factor: float = 1.0,
                noise: float = 0.05, seed: int = 1) -> list[list]:
    """Deterministic synthetic series ending now; optional step change N minutes ago."""
    now = int(time.time()) // step_s * step_s
    pts = []
    n = max(2, minutes * 60 // step_s)
    for i in range(n):
        t = now - (n - 1 - i) * step_s
        mins_ago = (now - t) / 60
        v = base * (factor if change_at_min is not None and mins_ago <= change_at_min else 1.0)
        v *= 1 + noise * math.sin(i * 0.7 + seed) + noise * 0.5 * math.cos(i * 1.9 + seed * 2)
        pts.append([t, round(max(v, 0), 6)])
    return pts


class Prometheus(Integration):
    name = "prometheus"
    title = "Prometheus"
    category = "Observability"
    icon = "chart"
    description = "PromQL queries, firing alerts and scrape targets (Prometheus, Mimir, Thanos, Alertmanager)."
    required_env = ("PROMETHEUS_URL",)

    def _client(self, base=None):
        headers = {}
        if env("PROMETHEUS_ORG_ID"):
            headers["X-Scope-OrgID"] = env("PROMETHEUS_ORG_ID")
        if env("PROMETHEUS_USER"):
            return http_client(self.settings, base or env("PROMETHEUS_URL"), user=env("PROMETHEUS_USER"),
                               token=env("PROMETHEUS_PASSWORD"), headers=headers)
        return http_client(self.settings, base or env("PROMETHEUS_URL"), token=env("PROMETHEUS_TOKEN"), headers=headers)

    def health(self):
        with self._client() as c:
            d = check(c.get("/api/v1/status/buildinfo")).json().get("data", {})
        return {"ok": True, "detail": f"version {d.get('version', '?')}"}

    # ------------------------------------------------------------ reads
    @read_op("Instant PromQL query. Returns one row per series.", (
        Param("query", required=True, description="PromQL"),
        Param("time", description="RFC3339 or unix time (default now)"),
    ))
    def query(self, query, time=None):
        params = {"query": query}
        if time:
            params["time"] = time
        with self._client() as c:
            d = check(c.get("/api/v1/query", params=params)).json().get("data", {})
        if d.get("resultType") == "scalar":
            return [{"series": "scalar", "labels": {}, "value": _num(d["result"][1])}]
        return [{"series": _label_str(r.get("metric", {})), "labels": r.get("metric", {}),
                 "value": _num((r.get("value") or [0, None])[1])} for r in d.get("result", [])[:200]]

    @read_op("Range PromQL query over the last N minutes. Returns downsampled series with min/max/last.", (
        Param("query", required=True, description="PromQL"),
        Param("minutes", "integer", "Window", default=60),
        Param("step", "integer", "Step seconds (default: window/120)"),
    ))
    def query_range(self, query, minutes=60, step=None):
        end = int(time.time())
        start = end - minutes * 60
        step = step or max(15, minutes * 60 // MAX_POINTS)
        with self._client() as c:
            d = check(c.get("/api/v1/query_range", params={"query": query, "start": start, "end": end,
                                                            "step": step})).json().get("data", {})
        return summarize_matrix(d.get("result", []))

    @read_op("Firing and pending alerts (from Alertmanager when ALERTMANAGER_URL is set, else Prometheus rules).", (
        Param("state", enum=["firing", "pending", "all"], default="firing"),
    ))
    def alerts(self, state="firing"):
        if env("ALERTMANAGER_URL"):
            with self._client(env("ALERTMANAGER_URL")) as c:
                data = check(c.get("/api/v2/alerts", params={"active": "true", "silenced": "false"})).json()
            rows = [{"alert": a["labels"].get("alertname"), "severity": a["labels"].get("severity", ""),
                     "state": "firing" if a.get("status", {}).get("state") == "active" else a.get("status", {}).get("state"),
                     "labels": {k: v for k, v in a["labels"].items() if k not in {"alertname", "severity"}},
                     "summary": _summary(a),
                     "since": iso(a.get("startsAt"))} for a in data]
        else:
            with self._client() as c:
                data = check(c.get("/api/v1/alerts")).json().get("data", {}).get("alerts", [])
            rows = [{"alert": a["labels"].get("alertname"), "severity": a["labels"].get("severity", ""),
                     "state": a.get("state"),
                     "labels": {k: v for k, v in a["labels"].items() if k not in {"alertname", "severity"}},
                     "summary": _summary(a),
                     "since": iso(a.get("activeAt"))} for a in data]
        if state != "all":
            rows = [r for r in rows if r["state"] == state]
        order = {"critical": 0, "page": 0, "high": 1, "warning": 2}
        return sorted(rows, key=lambda r: order.get(r["severity"], 3))

    @read_op("Scrape targets that are down.")
    def targets_down(self):
        with self._client() as c:
            d = check(c.get("/api/v1/targets", params={"state": "active"})).json().get("data", {})
        return [{"job": t["labels"].get("job"), "instance": t["labels"].get("instance"), "health": t.get("health"),
                 "error": t.get("lastError"), "last_scrape": iso(t.get("lastScrape"))}
                for t in d.get("activeTargets", []) if t.get("health") != "up"]

    @read_op("Metric names matching a substring (to discover what to query).", (
        Param("match", required=True, description="Substring, e.g. http_requests"),
    ))
    def metrics(self, match):
        with self._client() as c:
            names = check(c.get("/api/v1/label/__name__/values")).json().get("data", [])
        return [{"metric": n} for n in names if match.lower() in n.lower()][:200]

    def widget(self):
        return [
            Section("Alerts", "alerts", inputs=["state"], params={"state": "firing"}, refresh_secs=30,
                    columns=[Column("alert", fmt="code"), Column("severity", fmt="status"), Column("state", fmt="status"),
                             Column("summary"), Column("labels"), Column("since", fmt="time")]),
            Section("Query", "query", inputs=["query"],
                    params={"query": env("PROMETHEUS_DEFAULT_QUERY", "sum by (job) (up)")},
                    columns=[Column("series", fmt="code"), Column("value", fmt="num")], width="half"),
            Section("Targets down", "targets_down", width="half",
                    columns=[Column("job"), Column("instance", fmt="code"), Column("error"), Column("last_scrape", fmt="time")],
                    row_actions=[RowAction("query", "Up?", {}, params={"query": "up == 0"})]),
        ]

    def attention(self):
        firing = self.invoke("alerts", {"state": "firing"})
        if not firing:
            return []
        crit = [a for a in firing if a["severity"] in {"critical", "page"}]
        return [{"severity": "critical" if crit else "warning", "title": f"{len(firing)} Prometheus alert(s) firing",
                 "detail": ", ".join(a["alert"] for a in firing[:5])}]

    # ------------------------------------------------------------- demo
    def demo_query(self, query, time=None):
        q = query.lower()
        if "up" in q and "==" not in q:
            return [{"series": f'{{job="{j}"}}', "labels": {"job": j}, "value": v}
                    for j, v in [("payments-api", 2), ("payments-worker", 0), ("checkout-web", 2), ("node", 6)]]
        rows = self.demo_query_range(query, minutes=5)
        return [{"series": r["series"], "labels": r["labels"], "value": r["last"]} for r in rows]

    def demo_query_range(self, query, minutes=60, step=None):
        q = query.lower()
        step = step or max(15, minutes * 60 // MAX_POINTS)
        if "latency" in q or "duration" in q or "histogram_quantile" in q:
            specs = [("checkout-web", 0.22, 40, 3.1), ("payments-api", 0.12, 40, 1.4)]
        elif "error" in q or "5.." in q or "status" in q:
            specs = [("checkout-web", 0.004, 45, 9.0), ("payments-api", 0.002, 45, 4.0)]
        elif "lag" in q or "settlement" in q:
            specs = [("payments-worker", 30, 50, 40.0)]
        elif "cpu" in q:
            specs = [("payments-api", 0.35, None, 1), ("checkout-web", 0.6, None, 1)]
        elif "memory" in q:
            specs = [("payments-api", 4.1e8, None, 1), ("checkout-web", 6.3e8, None, 1)]
        else:
            specs = [("checkout-web", 120, 45, 0.7), ("payments-api", 80, None, 1)]
        out = []
        for i, (svc, base, at, factor) in enumerate(specs):
            pts = demo_series(base, minutes, step, at if at and at < minutes else None, factor, seed=i + 1)
            vals = [v for _, v in pts]
            out.append({"series": f'{{service="{svc}"}}', "labels": {"service": svc}, "min": min(vals),
                        "max": max(vals), "last": vals[-1], "points": pts})
        return out

    def demo_alerts(self, state="firing"):
        rows = [
            {"alert": "KubePodCrashLooping", "severity": "critical", "state": "firing",
             "labels": {"namespace": "payments", "pod": "payments-worker-5d4b9c7f8-hq2nn"},
             "summary": "Pod payments/payments-worker is crash looping", "since": ago(minutes=48)},
            {"alert": "SettlementLagHigh", "severity": "critical", "state": "firing",
             "labels": {"service": "payments-worker"}, "summary": "Settlement lag > 15m (now 21m)", "since": ago(minutes=40)},
            {"alert": "CheckoutHighErrorRate", "severity": "warning", "state": "firing",
             "labels": {"service": "checkout-web", "region": "us-east-1"},
             "summary": "5xx ratio 3.6% over 10m (SLO 99.5%)", "since": ago(minutes=38)},
            {"alert": "SearchApiImagePull", "severity": "warning", "state": "pending",
             "labels": {"namespace": "search"}, "summary": "search-api has no ready replicas", "since": ago(minutes=9)},
        ]
        return rows if state == "all" else [r for r in rows if r["state"] == state]

    def demo_targets_down(self):
        return [{"job": "payments-worker", "instance": "10.0.2.91:9100", "health": "down",
                 "error": "connect: connection refused", "last_scrape": ago(seconds=20)}]

    def demo_metrics(self, match):
        names = ["http_requests_total", "http_request_duration_seconds_bucket", "settlement_lag_seconds",
                 "container_cpu_usage_seconds_total", "container_memory_working_set_bytes", "kafka_consumergroup_lag", "up"]
        return [{"metric": n} for n in names if match.lower() in n]
