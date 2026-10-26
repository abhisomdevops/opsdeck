"""Vulnerability intelligence from public sources (no credentials needed):

* NVD CVE API 2.0       - description, CVSS, CWE, references (NVD_API_KEY raises rate limits)
* FIRST EPSS            - probability of exploitation in the next 30 days
* CISA KEV catalog      - known exploited in the wild, remediation due date
* OSV.dev               - affected packages per ecosystem with fixed versions

Enable with CVE_INTEL_ENABLED=true (off by default so air-gapped installs run on
demo data). Each source fails independently; the result says which ones did.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from ..config import env, env_bool
from ..registry import Column, Integration, OpError, Param, RowAction, Section, read_op
from ._common import ago, check, clip, http_client

_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
ECOSYSTEMS = ["PyPI", "npm", "Go", "Maven", "NuGet", "RubyGems", "crates.io", "Packagist", "Debian", "Ubuntu",
              "Alpine", "Red Hat"]


def _cve_id(cve: str) -> str:
    cve = (cve or "").strip().upper()
    if not _CVE.match(cve):
        raise OpError("cve must look like CVE-2024-12345")
    return cve


class CVEIntel(Integration):
    name = "cve"
    title = "CVE intel"
    category = "Security"
    icon = "shield"
    description = "NVD, EPSS, CISA KEV and OSV lookups for vulnerability triage."

    _kev: dict | None = None
    _kev_at: float = 0
    _lock = threading.Lock()

    def is_configured(self) -> bool:
        return env_bool("CVE_INTEL_ENABLED", False)

    def health(self):
        self._kev_catalog()
        return {"ok": True, "detail": f"KEV catalog: {len(CVEIntel._kev or {})} entries"}

    def _kev_catalog(self) -> dict:
        with self._lock:
            if CVEIntel._kev is None or time.time() - CVEIntel._kev_at > 6 * 3600:
                with http_client(self.settings, KEV_URL.rsplit("/", 1)[0]) as c:
                    data = check(c.get("/" + KEV_URL.rsplit("/", 1)[1])).json()
                CVEIntel._kev = {v["cveID"]: v for v in data.get("vulnerabilities", [])}
                CVEIntel._kev_at = time.time()
            return CVEIntel._kev

    def _nvd(self, cve: str) -> dict:
        headers = {"apiKey": env("NVD_API_KEY")} if env("NVD_API_KEY") else {}
        with http_client(self.settings, "https://services.nvd.nist.gov", headers=headers) as c:
            d = check(c.get("/rest/json/cves/2.0", params={"cveId": cve})).json()
        vulns = d.get("vulnerabilities") or []
        if not vulns:
            return {}
        v = vulns[0]["cve"]
        metrics = v.get("metrics", {})
        cvss = None
        for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if metrics.get(key):
                m = metrics[key][0]["cvssData"]
                cvss = {"version": m.get("version"), "score": m.get("baseScore"),
                        "severity": m.get("baseSeverity") or metrics[key][0].get("baseSeverity"),
                        "vector": m.get("vectorString")}
                break
        desc = next((x["value"] for x in v.get("descriptions", []) if x.get("lang") == "en"), "")
        cwes = sorted({d["value"] for w in v.get("weaknesses", []) for d in w.get("description", [])})
        return {"published": v.get("published"), "status": v.get("vulnStatus"), "description": clip(desc, 2000),
                "cvss": cvss, "cwe": cwes, "references": [r["url"] for r in v.get("references", [])[:8]]}

    def _epss(self, cve: str) -> dict:
        with http_client(self.settings, "https://api.first.org") as c:
            d = check(c.get("/data/v1/epss", params={"cve": cve})).json()
        row = (d.get("data") or [{}])[0]
        return {"score": float(row["epss"]), "percentile": float(row["percentile"]), "date": row.get("date")} if row else {}

    def _osv(self, cve: str) -> list[dict]:
        with http_client(self.settings, "https://api.osv.dev") as c:
            r = c.get(f"/v1/vulns/{cve}")
            if r.status_code == 404:
                return []
            d = check(r).json()
        return self._osv_affected(d)

    @staticmethod
    def _osv_affected(d: dict) -> list[dict]:
        out = []
        for a in d.get("affected", [])[:30]:
            pkg = a.get("package") or {}
            fixed = sorted({e["fixed"] for r in a.get("ranges", []) for e in r.get("events", []) if "fixed" in e})
            out.append({"ecosystem": pkg.get("ecosystem"), "package": pkg.get("name"), "fixed": fixed})
        return out

    # ------------------------------------------------------------ reads
    @read_op("Everything known about a CVE: CVSS/CWE (NVD), exploit probability (EPSS), "
             "known-exploited status (CISA KEV), and fixed versions per package (OSV).",
             (Param("cve", required=True, description="CVE-YYYY-NNNN"),))
    def lookup(self, cve):
        cve = _cve_id(cve)
        out: dict = {"cve": cve, "errors": []}
        for key, fn in (("nvd", self._nvd), ("epss", self._epss), ("osv_affected", self._osv)):
            try:
                out[key] = fn(cve)
            except (OpError, httpx.HTTPError, KeyError, ValueError) as exc:
                out[key] = None
                out["errors"].append(f"{key}: {str(exc)[:200]}")
        try:
            k = self._kev_catalog().get(cve)
            out["kev"] = ({"known_exploited": True, "added": k.get("dateAdded"), "due": k.get("dueDate"),
                           "ransomware": k.get("knownRansomwareCampaignUse"), "action": k.get("requiredAction")}
                          if k else {"known_exploited": False})
        except (OpError, httpx.HTTPError) as exc:
            out["kev"] = None
            out["errors"].append(f"kev: {str(exc)[:200]}")
        out["priority"] = priority(out)
        return out

    @read_op("Known vulnerabilities for one package version (OSV), with fixed versions.", (
        Param("ecosystem", required=True, enum=ECOSYSTEMS), Param("package", required=True),
        Param("version", required=True),
    ))
    def package(self, ecosystem, package, version):
        with http_client(self.settings, "https://api.osv.dev") as c:
            d = check(c.post("/v1/query", json={"version": version, "package": {"name": package, "ecosystem": ecosystem}})).json()
        rows = []
        for v in d.get("vulns", [])[:100]:
            aliases = [a for a in v.get("aliases", []) if a.startswith("CVE-")]
            fixed = sorted({f for a in self._osv_affected(v) if a["package"] == package for f in a["fixed"]})
            rows.append({"id": v["id"], "cve": ", ".join(aliases), "summary": (v.get("summary") or "")[:200],
                         "fixed": ", ".join(fixed)})
        return rows

    @read_op("CVEs added to CISA KEV in the last N days.", (Param("days", "integer", default=30),))
    def kev_recent(self, days=30):
        import datetime as dt

        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = [{"cve": k, "vendor": v.get("vendorProject"), "product": v.get("product"), "name": v.get("vulnerabilityName"),
                 "added": v.get("dateAdded"), "due": v.get("dueDate"), "ransomware": v.get("knownRansomwareCampaignUse")}
                for k, v in self._kev_catalog().items() if v.get("dateAdded", "") >= cutoff]
        return sorted(rows, key=lambda r: r["added"], reverse=True)

    def widget(self):
        return [
            Section("Look up a CVE", "lookup", inputs=["cve"], kind="kv", params={"cve": "CVE-2023-44487"}),
            Section("Recently known-exploited (CISA KEV)", "kev_recent", inputs=["days"], params={"days": 30},
                    columns=[Column("cve", fmt="code"), Column("vendor"), Column("product"), Column("name"),
                             Column("added", fmt="time"), Column("due"), Column("ransomware", fmt="status")],
                    row_actions=[RowAction("lookup", "Details", {"cve": "cve"})]),
        ]

    # ------------------------------------------------------------- demo
    _DEMO = {
        "CVE-2023-44487": dict(description="HTTP/2 Rapid Reset: request cancellation can reset many streams quickly, "
                                           "causing denial of service.",
                               cvss={"version": "3.1", "score": 7.5, "severity": "HIGH"}, cwe=["CWE-400"],
                               epss=0.94, kev=True,
                               osv=[{"ecosystem": "Go", "package": "golang.org/x/net", "fixed": ["0.17.0"]}]),
        "CVE-2024-35195": dict(description="Requests Session keeps verify=False for subsequent requests to the same host.",
                               cvss={"version": "3.1", "score": 5.6, "severity": "MEDIUM"}, cwe=["CWE-670"],
                               epss=0.001, kev=False, osv=[{"ecosystem": "PyPI", "package": "requests", "fixed": ["2.32.0"]}]),
        "CVE-2024-6387": dict(description="regreSSHion: signal handler race in OpenSSH sshd allows unauthenticated "
                                          "remote code execution as root on glibc-based Linux.",
                              cvss={"version": "3.1", "score": 8.1, "severity": "HIGH"}, cwe=["CWE-364"],
                              epss=0.4, kev=False,
                              osv=[{"ecosystem": "Debian", "package": "openssh", "fixed": ["1:9.2p1-2+deb12u3"]}]),
        "CVE-2023-4863": dict(description="Heap buffer overflow in libwebp allows out of bounds write via a crafted image.",
                              cvss={"version": "3.1", "score": 8.8, "severity": "HIGH"}, cwe=["CWE-787"],
                              epss=0.6, kev=True,
                              osv=[{"ecosystem": "Debian", "package": "libwebp", "fixed": ["1.2.4-0.2+deb12u1"]}]),
    }

    def demo_lookup(self, cve):
        cve = _cve_id(cve)
        d = self._DEMO.get(cve) or dict(description="(demo) no sample data for this CVE", cvss=None, cwe=[],
                                         epss=0.01, kev=False, osv=[])
        out = {"cve": cve, "errors": [], "nvd": {"published": "2023-10-10", "status": "Analyzed",
                                                  "description": d["description"], "cvss": d["cvss"], "cwe": d["cwe"],
                                                  "references": [f"https://nvd.nist.gov/vuln/detail/{cve}"]},
               "epss": {"score": d["epss"], "percentile": 0.99 if d["epss"] > 0.5 else 0.4, "date": "demo"},
               "osv_affected": d["osv"],
               "kev": {"known_exploited": d["kev"], "added": "2023-10-10" if d["kev"] else None}}
        out["priority"] = priority(out)
        return out

    def demo_package(self, ecosystem, package, version):
        hits = [(c, d) for c, d in self._DEMO.items() if any(a["package"] == package for a in d["osv"])]
        return [{"id": f"GHSA-demo-{i}", "cve": c, "summary": d["description"][:120],
                 "fixed": ", ".join(f for a in d["osv"] for f in a["fixed"])} for i, (c, d) in enumerate(hits)]

    def demo_kev_recent(self, days=30):
        return [{"cve": "CVE-2023-4863", "vendor": "Google", "product": "Chromium WebP", "name": "Heap buffer overflow",
                 "added": ago(days=3), "due": "in 3 weeks", "ransomware": "Unknown"},
                {"cve": "CVE-2023-44487", "vendor": "IETF", "product": "HTTP/2", "name": "Rapid Reset DDoS",
                 "added": ago(days=12), "due": "in 9 days", "ransomware": "Unknown"}]


def priority(info: dict) -> str:
    """P1 known-exploited or likely exploited; P2 high severity; P3 everything else."""
    kev = (info.get("kev") or {}).get("known_exploited")
    epss = (info.get("epss") or {}).get("score") or 0
    score = ((info.get("nvd") or {}).get("cvss") or {}).get("score") or 0
    if kev or epss >= 0.1:
        return "P1"
    if score >= 7:
        return "P2"
    return "P3"
