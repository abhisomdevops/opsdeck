"""Prisma Cloud Compute (Twistlock): image and host vulnerabilities (CVEs).

PRISMA_COMPUTE_URL: the Compute console path from Runtime Security > Manage >
System > Utilities, e.g. https://us-east1.cloud.twistlock.com/us-1-111111111.
Credentials: PRISMA_COMPUTE_ACCESS_KEY / PRISMA_COMPUTE_SECRET_KEY, falling back
to PRISMA_ACCESS_KEY / PRISMA_SECRET_KEY.
"""

from __future__ import annotations

import re
import threading
import time

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, read_op
from ._common import ago, check, http_client, iso

SEV = ["critical", "high", "medium", "low"]


def _fixed(status: str) -> str:
    m = re.search(r"fixed in (.+)", status or "", re.I)
    return m.group(1).strip() if m else ""


class PrismaCompute(Integration):
    name = "prisma_compute"
    title = "Prisma Compute"
    category = "Security"
    icon = "shield"
    description = "Container image vulnerabilities (CVEs) from Prisma Cloud Compute."
    required_env = ("PRISMA_COMPUTE_URL",)

    _token: str | None = None
    _token_at: float = 0
    _lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(env("PRISMA_COMPUTE_URL") and (env("PRISMA_COMPUTE_ACCESS_KEY") or env("PRISMA_ACCESS_KEY")))

    def _auth(self) -> str:
        with self._lock:
            if self._token and time.time() - self._token_at < 3000:
                return self._token
            with http_client(self.settings, env("PRISMA_COMPUTE_URL")) as c:
                res = check(c.post("/api/v1/authenticate", json={
                    "username": env("PRISMA_COMPUTE_ACCESS_KEY") or env("PRISMA_ACCESS_KEY"),
                    "password": env("PRISMA_COMPUTE_SECRET_KEY") or env("PRISMA_SECRET_KEY")})).json()
            PrismaCompute._token, PrismaCompute._token_at = res["token"], time.time()
            return PrismaCompute._token

    def _client(self):
        return http_client(self.settings, env("PRISMA_COMPUTE_URL"), token=self._auth(), timeout=60)

    def health(self):
        self._auth()
        return {"ok": True, "detail": "authenticated"}

    def _images(self, search=None, limit=50) -> list[dict]:
        params = {"limit": min(limit, 50), "offset": 0}
        if search:
            params["search"] = search
        with self._client() as c:
            return check(c.get("/api/v1/images", params=params)).json() or []

    @read_op("Scanned images with vulnerability counts.", (
        Param("search", description="Filter by image name"), Param("limit", "integer", default=50),
    ))
    def images(self, search=None, limit=50):
        rows = []
        for img in self._images(search, limit):
            tag = img.get("repoTag") or {}
            dist = img.get("vulnerabilityDistribution") or {}
            rows.append({"image": f"{tag.get('registry', '')}/{tag.get('repo', '')}:{tag.get('tag', '')}".lstrip("/"),
                         "critical": dist.get("critical", 0), "high": dist.get("high", 0),
                         "medium": dist.get("medium", 0), "running": len(img.get("instances") or []),
                         "os": img.get("distro"), "scanned": iso(img.get("scanTime"))})
        return sorted(rows, key=lambda r: (-r["critical"], -r["high"]))

    @read_op("CVE findings across images: package, installed and fixed version, where it runs.", (
        Param("severity", enum=SEV, description="Minimum severity"), Param("search", description="Image name filter"),
        Param("cve", description="Only this CVE"), Param("fixable_only", "boolean", default=True),
        Param("limit", "integer", default=200),
    ))
    def vulnerabilities(self, severity=None, search=None, cve=None, fixable_only=True, limit=200):
        floor = SEV.index(severity) if severity else len(SEV) - 1
        rows = []
        for img in self._images(search, 50):
            tag = img.get("repoTag") or {}
            image = f"{tag.get('repo', '')}:{tag.get('tag', '')}"
            hosts = sorted({i.get("host", "") for i in img.get("instances") or []})
            for v in img.get("vulnerabilities") or []:
                sev = (v.get("severity") or "low").lower()
                if sev not in SEV or SEV.index(sev) > floor:
                    continue
                if cve and v.get("cve") != cve.upper():
                    continue
                fixed = _fixed(v.get("status", ""))
                if fixable_only and not fixed:
                    continue
                rows.append({"cve": v.get("cve"), "severity": sev, "cvss": v.get("cvss"),
                             "package": v.get("packageName"), "installed": v.get("packageVersion"), "fixed": fixed,
                             "image": image, "running_on": ", ".join(hosts[:3]), "published": iso(v.get("published"))})
        rows.sort(key=lambda r: (SEV.index(r["severity"]), -(r["cvss"] or 0)))
        return rows[:limit]

    def widget(self):
        return [
            Section("Fixable vulnerabilities", "vulnerabilities", inputs=["severity", "search", "cve"],
                    params={"severity": "high", "fixable_only": True, "limit": 200}, refresh_secs=900,
                    columns=[Column("cve", fmt="code"), Column("severity", fmt="status"), Column("cvss", fmt="num"),
                             Column("package", fmt="code"), Column("installed", fmt="code"), Column("fixed", fmt="code"),
                             Column("image", fmt="code"), Column("running_on")],
                    row_actions=[RowAction("vulnerabilities", "Where else?", {"cve": "cve"}, params={"severity": "low"})]),
            Section("Images", "images", inputs=["search"],
                    columns=[Column("image", fmt="code"), Column("critical", fmt="num"), Column("high", fmt="num"),
                             Column("medium", fmt="num"), Column("running", fmt="num"), Column("os"),
                             Column("scanned", fmt="time")]),
        ]

    def attention(self):
        crit = self.invoke("vulnerabilities", {"severity": "critical", "search": None, "cve": None,
                                               "fixable_only": True, "limit": 500})
        if not crit:
            return []
        return [{"severity": "warning", "title": f"{len(crit)} fixable critical CVE finding(s) in images",
                 "detail": ", ".join(sorted({r['cve'] for r in crit})[:5])}]

    # ------------------------------------------------------------- demo
    _VULNS = [
        ("CVE-2023-44487", "high", 7.5, "golang.org/x/net", "v0.15.0", "0.17.0", "acme/checkout-web:3.8.1", "ip-10-0-1-12"),
        ("CVE-2023-4863", "critical", 8.8, "libwebp7", "1.2.4-0.2", "1.2.4-0.2+deb12u1", "acme/payments-worker:1.42.0",
         "ip-10-0-2-40"),
        ("CVE-2024-35195", "medium", 5.6, "requests", "2.31.0", "2.32.0", "acme/payments-worker:1.42.0", "ip-10-0-2-40"),
        ("CVE-2024-6387", "high", 8.1, "openssh-client", "1:9.2p1-2", "1:9.2p1-2+deb12u3", "acme/payments-worker:1.42.0",
         "ip-10-0-2-40"),
    ]

    def demo_vulnerabilities(self, severity=None, search=None, cve=None, fixable_only=True, limit=200):
        floor = SEV.index(severity) if severity else len(SEV) - 1
        return [{"cve": c, "severity": s, "cvss": cv, "package": p, "installed": i, "fixed": f, "image": im,
                 "running_on": h, "published": ago(days=200)} for c, s, cv, p, i, f, im, h in self._VULNS
                if SEV.index(s) <= floor and (not cve or c == cve.upper()) and (not search or search in im)][:limit]

    def demo_images(self, search=None, limit=50):
        return [{"image": "registry.example.com/acme/payments-worker:1.42.0", "critical": 1, "high": 1, "medium": 1,
                 "running": 1, "os": "debian 12", "scanned": ago(hours=3)},
                {"image": "registry.example.com/acme/checkout-web:3.8.1", "critical": 0, "high": 1, "medium": 0,
                 "running": 2, "os": "distroless", "scanned": ago(hours=3)}]
