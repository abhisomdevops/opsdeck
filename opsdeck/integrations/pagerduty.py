"""PagerDuty: open incidents, timeline, on-call; acknowledge / note / resolve.

PAGERDUTY_TOKEN: REST API key (read-only is enough for viewing). Writes need a
full-access key and PAGERDUTY_FROM (the email of a valid PagerDuty user that
actions are attributed to). PAGERDUTY_API_URL for the EU service region
(https://api.eu.pagerduty.com).
"""

from __future__ import annotations

from ..config import env
from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago, check, http_client, iso


class PagerDuty(Integration):
    name = "pagerduty"
    title = "PagerDuty"
    category = "Incidents"
    icon = "bell"
    description = "Open incidents, timelines and who is on call."
    required_env = ("PAGERDUTY_TOKEN",)

    def _client(self, write=False):
        headers = {"Authorization": f"Token token={env('PAGERDUTY_TOKEN')}",
                   "Accept": "application/vnd.pagerduty+json;version=2"}
        if write:
            if not env("PAGERDUTY_FROM"):
                raise OpError("set PAGERDUTY_FROM to the email of the PagerDuty user actions are made as", 409)
            headers["From"] = env("PAGERDUTY_FROM")
        return http_client(self.settings, env("PAGERDUTY_API_URL", "https://api.pagerduty.com"), headers=headers)

    def health(self):
        with self._client() as c:
            check(c.get("/abilities"))
        return {"ok": True, "detail": "connected"}

    @staticmethod
    def _row(i: dict) -> dict:
        return {"id": i["id"], "number": i.get("incident_number"), "title": i.get("title"),
                "status": i.get("status"), "urgency": i.get("urgency"),
                "priority": (i.get("priority") or {}).get("summary", ""),
                "service": (i.get("service") or {}).get("summary"),
                "assignee": ", ".join(a["assignee"]["summary"] for a in i.get("assignments", [])),
                "created": iso(i.get("created_at")), "url": i.get("html_url")}

    @read_op("Open (triggered/acknowledged) incidents.", (
        Param("urgency", enum=["high", "low"]), Param("limit", "integer", default=50),
    ))
    def incidents(self, urgency=None, limit=50):
        params = {"statuses[]": ["triggered", "acknowledged"], "limit": min(limit, 100), "sort_by": "created_at:desc"}
        if urgency:
            params["urgencies[]"] = urgency
        with self._client() as c:
            data = check(c.get("/incidents", params=params)).json()
        return [self._row(i) for i in data.get("incidents", [])]

    @read_op("One incident with its timeline (log entries) and alert details.", (Param("id", required=True),))
    def incident(self, id):
        with self._client() as c:
            inc = check(c.get(f"/incidents/{id}")).json()["incident"]
            log = check(c.get(f"/incidents/{id}/log_entries", params={"limit": 50, "is_overview": "true"})).json()
            alerts = check(c.get(f"/incidents/{id}/alerts", params={"limit": 10})).json()
        out = self._row(inc)
        out["timeline"] = "\n".join(f"{e.get('created_at', '')[:19]} {e.get('summary', '')}"
                                    for e in reversed(log.get("log_entries", [])))
        out["alerts"] = "\n".join(f"- {a.get('summary')}: {str((a.get('body') or {}).get('details', ''))[:500]}"
                                  for a in alerts.get("alerts", []))
        return out

    @read_op("Who is on call now, per escalation policy.")
    def oncall(self):
        with self._client() as c:
            data = check(c.get("/oncalls", params={"limit": 100, "earliest": "true"})).json()
        return [{"policy": (o.get("escalation_policy") or {}).get("summary"), "level": o.get("escalation_level"),
                 "user": (o.get("user") or {}).get("summary"), "until": iso(o.get("end"))}
                for o in data.get("oncalls", [])]

    def _update(self, id, status):
        with self._client(write=True) as c:
            check(c.put(f"/incidents/{id}", json={"incident": {"type": "incident_reference", "status": status}}))
        return {"incident": id, "status": status}

    @action("Acknowledge an incident.", (Param("id", required=True),), risk="low")
    def acknowledge(self, id):
        return self._update(id, "acknowledged")

    @action("Resolve an incident.", (Param("id", required=True),), risk="medium")
    def resolve(self, id):
        return self._update(id, "resolved")

    @action("Add a note to an incident timeline.", (Param("id", required=True), Param("body", required=True)),
            risk="low")
    def add_note(self, id, body):
        with self._client(write=True) as c:
            check(c.post(f"/incidents/{id}/notes", json={"note": {"content": body}}))
        return {"incident": id, "noted": True}

    def widget(self):
        cols = [Column("number", fmt="num"), Column("title"), Column("status", fmt="status"),
                Column("urgency", fmt="status"), Column("service"), Column("assignee"), Column("created", fmt="time"),
                Column("url", fmt="link")]
        return [
            Section("Open incidents", "incidents", inputs=["urgency"], refresh_secs=30, columns=cols, row_actions=[
                RowAction("incident", "Timeline", {"id": "id"}), RowAction("acknowledge", "Ack", {"id": "id"}),
                RowAction("add_note", "Note", {"id": "id"}, inputs=["body"]), RowAction("resolve", "Resolve", {"id": "id"})]),
            Section("On call", "oncall", width="half",
                    columns=[Column("policy"), Column("level", fmt="num"), Column("user"), Column("until", fmt="time")]),
        ]

    def attention(self):
        rows = self.invoke("incidents", {"urgency": "high", "limit": 50})
        trig = [r for r in rows if r["status"] == "triggered"]
        if not rows:
            return []
        return [{"severity": "critical" if trig else "warning",
                 "title": f"{len(rows)} high-urgency PagerDuty incident(s), {len(trig)} unacknowledged",
                 "detail": "; ".join(r["title"] for r in rows[:3])}]

    # ------------------------------------------------------------- demo
    def demo_incidents(self, urgency=None, limit=50):
        rows = [
            {"id": "Q1PAY", "number": 8812, "title": "[P1] payments-worker CrashLoopBackOff / settlement lag",
             "status": "acknowledged", "urgency": "high", "priority": "P1", "service": "payments",
             "assignee": "Dana Ruiz", "created": ago(minutes=47), "url": "https://acme.pagerduty.com/incidents/Q1PAY"},
            {"id": "Q2CHK", "number": 8813, "title": "[P2] checkout-web 5xx SLO burn", "status": "triggered",
             "urgency": "high", "priority": "P2", "service": "checkout", "assignee": "Sam Patel",
             "created": ago(minutes=36), "url": "https://acme.pagerduty.com/incidents/Q2CHK"},
            {"id": "Q3SRC", "number": 8809, "title": "search-api no ready replicas", "status": "triggered",
             "urgency": "low", "priority": "P3", "service": "search", "assignee": "Lee Chen",
             "created": ago(minutes=8), "url": "https://acme.pagerduty.com/incidents/Q3SRC"},
        ]
        return [r for r in rows if not urgency or r["urgency"] == urgency][:limit]

    def demo_incident(self, id):
        row = next((r for r in self.demo_incidents() if r["id"] == id), self.demo_incidents()[0])
        return dict(row, timeline="T-47m Triggered by Prometheus: KubePodCrashLooping payments-worker\n"
                                  "T-45m Escalated to Dana Ruiz (Payments primary)\nT-41m Acknowledged by Dana Ruiz\n"
                                  "T-40m Alert grouped: SettlementLagHigh",
                    alerts="- KubePodCrashLooping: pod payments-worker-5d4b9c7f8-hq2nn restarts 14\n"
                           "- SettlementLagHigh: settlement_lag_seconds 1260")

    def demo_oncall(self):
        return [{"policy": "Payments", "level": 1, "user": "Dana Ruiz", "until": ago(hours=-30)},
                {"policy": "Payments", "level": 2, "user": "Jo Park", "until": ago(hours=-30)},
                {"policy": "Checkout", "level": 1, "user": "Sam Patel", "until": ago(hours=-6)},
                {"policy": "Platform SRE", "level": 1, "user": "Lee Chen", "until": ago(hours=-54)}]

    def demo_acknowledge(self, id):
        return {"incident": id, "status": "acknowledged", "demo": True}

    def demo_resolve(self, id):
        return {"incident": id, "status": "resolved", "demo": True}

    def demo_add_note(self, id, body):
        return {"incident": id, "noted": True, "demo": True}
