"""Tempo traces: search with TraceQL and read a trace.

TEMPO_URL: base URL serving /api/search and /api/traces. Auth: TEMPO_TOKEN or
TEMPO_USER + TEMPO_PASSWORD; TEMPO_ORG_ID sets X-Scope-OrgID.
"""

from __future__ import annotations

import time

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, read_op
from ._common import ago, check, http_client, iso


def _attr(attrs: list, key: str):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value") or {}
            return v.get("stringValue") or v.get("intValue") or v.get("boolValue")
    return None


def summarize_trace(data: dict) -> list[dict]:
    """OTLP JSON (Tempo /api/traces/<id>) -> flat span rows, slowest first."""
    rows = []
    for rs in data.get("batches") or data.get("resourceSpans") or []:
        svc = _attr((rs.get("resource") or {}).get("attributes"), "service.name")
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans", []):
                start, end = int(sp.get("startTimeUnixNano", 0)), int(sp.get("endTimeUnixNano", 0))
                status = (sp.get("status") or {}).get("code")
                rows.append({"service": svc, "span": sp.get("name"), "duration_ms": round((end - start) / 1e6, 1),
                             "status": "error" if status in (2, "STATUS_CODE_ERROR") else "ok",
                             "http_status": _attr(sp.get("attributes"), "http.status_code")
                             or _attr(sp.get("attributes"), "http.response.status_code"),
                             "span_id": sp.get("spanId"), "parent": sp.get("parentSpanId")})
    return sorted(rows, key=lambda r: -r["duration_ms"])[:300]


class Tempo(Integration):
    name = "tempo"
    title = "Tempo"
    category = "Observability"
    icon = "trace"
    description = "Distributed traces (TraceQL search, trace breakdown)."
    required_env = ("TEMPO_URL",)

    def _client(self):
        headers = {"X-Scope-OrgID": env("TEMPO_ORG_ID")} if env("TEMPO_ORG_ID") else {}
        if env("TEMPO_USER"):
            return http_client(self.settings, env("TEMPO_URL"), user=env("TEMPO_USER"), token=env("TEMPO_PASSWORD"),
                               headers=headers)
        return http_client(self.settings, env("TEMPO_URL"), token=env("TEMPO_TOKEN"), headers=headers)

    def health(self):
        with self._client() as c:
            check(c.get("/api/echo"))
        return {"ok": True, "detail": "connected"}

    @read_op("Search traces with TraceQL.", (
        Param("traceql", description='e.g. { resource.service.name = "checkout-web" && status = error }',
              default="{ status = error }"),
        Param("minutes", "integer", default=60), Param("limit", "integer", default=20),
    ))
    def search(self, traceql="{ status = error }", minutes=60, limit=20):
        end = int(time.time())
        with self._client() as c:
            d = check(c.get("/api/search", params={"q": traceql, "start": end - minutes * 60, "end": end,
                                                    "limit": min(limit, 100)})).json()
        return [{"trace_id": t.get("traceID"), "root_service": t.get("rootServiceName"),
                 "root_span": t.get("rootTraceName"), "duration_ms": t.get("durationMs"),
                 "start": iso(int(t.get("startTimeUnixNano", 0)) / 1e9 if t.get("startTimeUnixNano") else None)}
                for t in d.get("traces", [])]

    @read_op("Spans of one trace, slowest first, with error status.", (Param("trace_id", required=True),))
    def trace(self, trace_id):
        with self._client() as c:
            d = check(c.get(f"/api/traces/{trace_id}")).json()
        return summarize_trace(d)

    def widget(self):
        return [Section("Error traces", "search", inputs=["traceql", "minutes"],
                        params={"traceql": "{ status = error }", "minutes": 60, "limit": 20},
                        columns=[Column("trace_id", fmt="code"), Column("root_service"), Column("root_span"),
                                 Column("duration_ms", fmt="num"), Column("start", fmt="time")],
                        row_actions=[RowAction("trace", "Spans", {"trace_id": "trace_id"})])]

    def demo_search(self, traceql="{ status = error }", minutes=60, limit=20):
        return [{"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "root_service": "checkout-web",
                 "root_span": "POST /api/pay", "duration_ms": 3012, "start": ago(minutes=12)},
                {"trace_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "root_service": "checkout-web",
                 "root_span": "POST /api/pay", "duration_ms": 3004, "start": ago(minutes=17)}][:limit]

    def demo_trace(self, trace_id):
        return [
            {"service": "checkout-web", "span": "POST /api/pay", "duration_ms": 3012.4, "status": "error",
             "http_status": 502, "span_id": "a1", "parent": None},
            {"service": "checkout-web", "span": "HTTP POST payments-api/charge", "duration_ms": 3001.0,
             "status": "error", "http_status": None, "span_id": "a2", "parent": "a1"},
            {"service": "payments-api", "span": "enqueue settlement", "duration_ms": 2950.2, "status": "ok",
             "http_status": None, "span_id": "b1", "parent": "a2"},
            {"service": "payments-api", "span": "kafka produce settlements (queue full, retrying)",
             "duration_ms": 2940.8, "status": "ok", "http_status": None, "span_id": "b2", "parent": "b1"},
            {"service": "payments-api", "span": "SELECT ledger_accounts", "duration_ms": 4.1, "status": "ok",
             "http_status": None, "span_id": "b3", "parent": "b1"},
        ]
