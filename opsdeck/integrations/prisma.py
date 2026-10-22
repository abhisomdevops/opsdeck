"""Prisma Cloud (CSPM): open alerts by severity and policy.

PRISMA_URL is your tenant API URL (e.g. https://api.prismacloud.io or
https://api2.prismacloud.io), PRISMA_ACCESS_KEY / PRISMA_SECRET_KEY an access
key for a read-only (Account Group Read Only) role.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

from ..config import env
from ..registry import Column, Integration, Param, Section, read_op
from ._common import ago, check, http_client, iso

SEVERITIES = ["critical", "high", "medium", "low", "informational"]


class Prisma(Integration):
    name = "prisma"
    title = "Prisma Cloud"
    category = "Security"
    icon = "shield"
    description = "Cloud security posture alerts (CSPM)."
    required_env = ("PRISMA_URL", "PRISMA_ACCESS_KEY", "PRISMA_SECRET_KEY")

    _token: str | None = None
    _token_at: float = 0
    _lock = threading.Lock()

    def _auth_token(self) -> str:
        with self._lock:
            # Prisma tokens are valid for 10 minutes.
            if self._token and time.time() - self._token_at < 540:
                return self._token
            with http_client(self.settings, env("PRISMA_URL")) as c:
                res = check(c.post("/login", json={"username": env("PRISMA_ACCESS_KEY"),
                                                    "password": env("PRISMA_SECRET_KEY")})).json()
            Prisma._token, Prisma._token_at = res["token"], time.time()
            return Prisma._token

    def _client(self):
        return http_client(self.settings, env("PRISMA_URL"), headers={"x-redlock-auth": self._auth_token()})

    def health(self):
        self._auth_token()
        return {"ok": True, "detail": "authenticated"}

    @read_op("Open Prisma Cloud alerts, highest severity first.", (
        Param("severity", enum=SEVERITIES, description="Only this severity"),
        Param("days", "integer", "Look-back window in days", default=7),
        Param("limit", "integer", default=100),
    ))
    def alerts(self, severity=None, days=7, limit=100):
        params = {"timeType": "relative", "timeAmount": max(1, min(days, 90)), "timeUnit": "day",
                  "detailed": "true", "alert.status": "open", "limit": min(limit, 500)}
        if severity:
            params["policy.severity"] = severity
        with self._client() as c:
            data = check(c.get("/v2/alert", params=params)).json()
        rows = []
        for a in data.get("items", []):
            pol, res = a.get("policy") or {}, a.get("resource") or {}
            rows.append({
                "id": a.get("id"), "severity": pol.get("severity"), "policy": pol.get("name"),
                "type": pol.get("policyType"), "resource": res.get("name"),
                "resource_type": res.get("resourceType"), "cloud": res.get("cloudType"),
                "account": res.get("account") or res.get("accountId"), "region": res.get("region"),
                "since": iso(a.get("alertTime")),
            })
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(rows, key=lambda r: order.get((r["severity"] or "").lower(), 9))

    @read_op("Count of open alerts by severity.", (Param("days", "integer", default=7),))
    def summary(self, days=7):
        counts = Counter((a["severity"] or "unknown").lower() for a in self.invoke(
            "alerts", {"severity": None, "days": days, "limit": 500}))
        return {s: counts.get(s, 0) for s in SEVERITIES}

    def widget(self):
        return [
            Section("Open alerts by severity", "summary", kind="kv", params={"days": 7}, width="half"),
            Section("Open alerts", "alerts", inputs=["severity", "days"], params={"days": 7, "limit": 100},
                    columns=[Column("severity", fmt="status"), Column("policy"), Column("type"),
                             Column("resource", fmt="code"), Column("resource_type"), Column("cloud"),
                             Column("account"), Column("region"), Column("since", fmt="time")],
                    refresh_secs=600),
        ]

    def attention(self):
        s = self.invoke("summary", {"days": 7})
        if not (s["critical"] or s["high"]):
            return []
        return [{"severity": "critical" if s["critical"] else "warning",
                 "title": f"{s['critical']} critical / {s['high']} high Prisma alerts open",
                 "detail": "Cloud posture alerts from the last 7 days"}]

    # -------------------------------------------------------------- demo
    _ALERTS = [
        ("critical", "AWS S3 bucket publicly readable", "config", "acme-tmp-exports", "AWS::S3::Bucket", 30),
        ("high", "AWS Security Group allows 0.0.0.0/0 on port 22", "network", "sg-0a1b2c3d", "AWS::EC2::SecurityGroup", 4),
        ("high", "EKS cluster endpoint publicly accessible", "config", "prod-eks", "AWS::EKS::Cluster", 90),
        ("medium", "IAM user access key older than 90 days", "iam", "svc-legacy-reports", "AWS::IAM::User", 300),
        ("medium", "RDS instance without deletion protection", "config", "ledger-db-replica", "AWS::RDS::DBInstance", 50),
        ("low", "CloudTrail log file validation disabled", "config", "org-trail", "AWS::CloudTrail::Trail", 700),
    ]

    def demo_alerts(self, severity=None, days=7, limit=100):
        return [{"id": f"P-{1000 + i}", "severity": s, "policy": p, "type": t, "resource": r,
                 "resource_type": rt, "cloud": "aws", "account": "prod (123456789012)", "region": "us-east-1",
                 "since": ago(hours=h)} for i, (s, p, t, r, rt, h) in enumerate(self._ALERTS)
                if not severity or s == severity][:limit]

    def demo_summary(self, days=7):
        counts = Counter(a["severity"] for a in self.demo_alerts())
        return {s: counts.get(s, 0) for s in SEVERITIES}
