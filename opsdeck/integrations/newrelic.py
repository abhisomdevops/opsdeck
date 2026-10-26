"""New Relic (NerdGraph): NRQL, APM application health and active issues.

NEW_RELIC_API_KEY: a User key. NEW_RELIC_ACCOUNT_ID: account to query.
NEW_RELIC_REGION: US (default) or EU.
"""

from __future__ import annotations

import json

from ..config import env
from ..registry import Column, Integration, OpError, Param, Section, read_op
from ._common import ago, check, http_client, iso


class NewRelic(Integration):
    name = "newrelic"
    title = "New Relic"
    category = "Observability"
    icon = "chart"
    description = "NRQL queries, APM application health and active issues."
    required_env = ("NEW_RELIC_API_KEY", "NEW_RELIC_ACCOUNT_ID")

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        base = "https://api.eu.newrelic.com" if (env("NEW_RELIC_REGION") or "").upper() == "EU" else "https://api.newrelic.com"
        with http_client(self.settings, base, headers={"API-Key": env("NEW_RELIC_API_KEY")}) as c:
            d = check(c.post("/graphql", json={"query": query, "variables": variables or {}})).json()
        if d.get("errors"):
            raise OpError(f"NerdGraph: {d['errors'][0].get('message')}", 502)
        return d.get("data") or {}

    @property
    def account(self) -> int:
        return int(env("NEW_RELIC_ACCOUNT_ID") or 0)

    def health(self):
        d = self._gql("{ actor { user { email } } }")
        return {"ok": True, "detail": f"as {d['actor']['user']['email']}"}

    @read_op("Run an NRQL query (e.g. SELECT percentile(duration, 99) FROM Transaction FACET appName SINCE 1 hour ago).",
             (Param("nrql", required=True),))
    def nrql(self, nrql):
        q = "query($id: Int!, $nrql: Nrql!) { actor { account(id: $id) { nrql(query: $nrql) { results } } } }"
        d = self._gql(q, {"id": self.account, "nrql": nrql})
        results = d["actor"]["account"]["nrql"]["results"] or []
        return [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in results[:500]]

    @read_op("APM applications with alert severity, error rate, response time and throughput.")
    def applications(self):
        q = ("{ actor { entitySearch(query: \"domain = 'APM' AND type = 'APPLICATION'\") { results { entities { "
             "name guid alertSeverity reporting ... on ApmApplicationEntityOutline { apmSummary { errorRate "
             "responseTimeAverage throughput apdexScore } } } } } } }")
        ents = self._gql(q)["actor"]["entitySearch"]["results"]["entities"]
        rows = []
        for e in ents:
            s = e.get("apmSummary") or {}
            rows.append({"app": e["name"], "alert": e.get("alertSeverity"), "reporting": e.get("reporting"),
                         "error_rate": s.get("errorRate"), "response_ms": round((s.get("responseTimeAverage") or 0) * 1000, 1),
                         "rpm": s.get("throughput"), "apdex": s.get("apdexScore")})
        order = {"CRITICAL": 0, "WARNING": 1}
        return sorted(rows, key=lambda r: order.get(r["alert"], 2))

    @read_op("Active alert issues.")
    def issues(self):
        q = ("query($id: Int!) { actor { account(id: $id) { aiIssues { issues(filter: {states: [ACTIVATED, CREATED]}) "
             "{ issues { issueId title priority state activatedAt entityNames } } } } } }")
        items = self._gql(q, {"id": self.account})["actor"]["account"]["aiIssues"]["issues"]["issues"]
        return [{"id": i["issueId"], "title": " / ".join(i.get("title") or []), "priority": i.get("priority"),
                 "state": i.get("state"), "entities": ", ".join(i.get("entityNames") or []),
                 "since": iso(i.get("activatedAt"))} for i in items]

    def widget(self):
        return [
            Section("APM applications", "applications", refresh_secs=120,
                    columns=[Column("app"), Column("alert", fmt="status"), Column("error_rate", fmt="num"),
                             Column("response_ms", fmt="num"), Column("rpm", fmt="num"), Column("apdex", fmt="num")]),
            Section("Active issues", "issues", width="half",
                    columns=[Column("title"), Column("priority", fmt="status"), Column("entities"), Column("since", fmt="time")]),
            Section("NRQL", "nrql", inputs=["nrql"], width="half",
                    params={"nrql": "SELECT count(*) FROM TransactionError FACET appName SINCE 1 hour ago"}),
        ]

    def attention(self):
        crit = [a for a in self.invoke("applications", {}) if a["alert"] == "CRITICAL"]
        return [{"severity": "critical", "title": f"{len(crit)} New Relic app(s) critical",
                 "detail": ", ".join(a["app"] for a in crit)}] if crit else []

    def demo_nrql(self, nrql):
        return [{"appName": "checkout-web", "count": 1843}, {"appName": "payments-api", "count": 212}]

    def demo_applications(self):
        return [{"app": "checkout-web", "alert": "CRITICAL", "reporting": True, "error_rate": 0.036, "response_ms": 1840.2,
                 "rpm": 5200, "apdex": 0.61},
                {"app": "payments-api", "alert": "WARNING", "reporting": True, "error_rate": 0.004, "response_ms": 310.5,
                 "rpm": 2100, "apdex": 0.88},
                {"app": "search-api", "alert": "NOT_ALERTING", "reporting": True, "error_rate": 0.0, "response_ms": 45.0,
                 "rpm": 900, "apdex": 0.99}]

    def demo_issues(self):
        return [{"id": "nr-1", "title": "checkout-web Apdex < 0.7", "priority": "CRITICAL", "state": "ACTIVATED",
                 "entities": "checkout-web", "since": ago(minutes=37)}]
