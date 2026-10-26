"""Loki logs (LogQL).

LOKI_URL: base URL serving /loki/api/v1/*. Auth: LOKI_TOKEN (bearer) or
LOKI_USER + LOKI_PASSWORD (basic, e.g. Grafana Cloud). LOKI_ORG_ID sets
X-Scope-OrgID for multi-tenant Loki.
"""

from __future__ import annotations

import datetime as dt
import time

from ..config import env
from ..registry import Column, Integration, Param, Section, read_op
from ._common import ago, check, http_client


class Loki(Integration):
    name = "loki"
    title = "Loki"
    category = "Observability"
    icon = "logs"
    description = "Search logs with LogQL."
    required_env = ("LOKI_URL",)

    def _client(self):
        headers = {"X-Scope-OrgID": env("LOKI_ORG_ID")} if env("LOKI_ORG_ID") else {}
        if env("LOKI_USER"):
            return http_client(self.settings, env("LOKI_URL"), user=env("LOKI_USER"), token=env("LOKI_PASSWORD"),
                               headers=headers)
        return http_client(self.settings, env("LOKI_URL"), token=env("LOKI_TOKEN"), headers=headers)

    def health(self):
        with self._client() as c:
            check(c.get("/loki/api/v1/labels"))
        return {"ok": True, "detail": "connected"}

    @read_op("Log lines matching a LogQL query, newest first.", (
        Param("query", required=True, description='LogQL, e.g. {namespace="payments"} |= "error"'),
        Param("minutes", "integer", "Look-back window", default=60),
        Param("limit", "integer", default=200),
    ))
    def query(self, query, minutes=60, limit=200):
        end = time.time_ns()
        start = end - minutes * 60 * 10**9
        with self._client() as c:
            d = check(c.get("/loki/api/v1/query_range", params={
                "query": query, "start": start, "end": end, "limit": min(limit, 2000), "direction": "backward",
            })).json().get("data", {})
        rows = []
        for stream in d.get("result", []):
            labels = stream.get("stream", {})
            short = ",".join(f"{k}={v}" for k, v in labels.items() if k in {"namespace", "pod", "app", "container", "level"})
            for ts, line in stream.get("values", []):
                rows.append({"time": dt.datetime.fromtimestamp(int(ts) / 1e9, dt.UTC).isoformat(),
                             "labels": short, "line": line[:2000]})
        rows.sort(key=lambda r: r["time"], reverse=True)
        return rows[:limit]

    @read_op("Values of a label (e.g. namespace, app) to build queries.", (Param("label", required=True),))
    def label_values(self, label):
        with self._client() as c:
            vals = check(c.get(f"/loki/api/v1/label/{label}/values")).json().get("data", [])
        return [{"value": v} for v in vals[:500]]

    def widget(self):
        return [Section("Logs", "query", inputs=["query", "minutes"],
                        params={"query": env("LOKI_DEFAULT_QUERY", '{namespace="payments"} |~ "(?i)error|fatal"'),
                                "minutes": 60, "limit": 200},
                        columns=[Column("time", fmt="time"), Column("labels", fmt="code"), Column("line", fmt="code")])]

    def demo_query(self, query, minutes=60, limit=200):
        lines = [
            (1, "namespace=payments,pod=payments-worker-5d4b9c7f8-hq2nn", "FATAL configuration invalid, exiting (code 2)"),
            (1, "namespace=payments,pod=payments-worker-5d4b9c7f8-hq2nn", "ERROR missing required env PAYMENTS_DB_DSN"),
            (6, "namespace=payments,pod=payments-worker-5d4b9c7f8-hq2nn", "ERROR missing required env PAYMENTS_DB_DSN"),
            (9, "namespace=payments,pod=payments-api-7c9d8f6b5-2xkqp",
             "WARN settlement queue depth 18234 exceeds threshold 5000"),
            (12, "namespace=checkout,pod=checkout-web-6f7b8c9d4-ab12c",
             "ERROR POST /api/pay 502 upstream payments-api timeout after 3000ms trace_id=4bf92f3577b34da6a3ce929d0e0e4736"),
            (44, "namespace=payments,pod=payments-worker-7f9c6d5b4-k2l9m", "INFO starting payments-worker v1.41.3"),
        ]
        q = query.lower()
        rows = [{"time": ago(minutes=m), "labels": lb, "line": ln} for m, lb, ln in lines if m <= minutes]
        if "checkout" in q:
            rows = [r for r in rows if "checkout" in r["labels"]] or rows
        return rows[:limit]

    def demo_label_values(self, label):
        return [{"value": v} for v in ["checkout", "payments", "search", "platform"]]
