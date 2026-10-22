"""Jenkins: job status, build history, console output, trigger builds.

Auth: JENKINS_USER + JENKINS_TOKEN (a user API token; API-token requests do
not need a CSRF crumb). Job names may contain folders: "team/service/main".
"""

from __future__ import annotations

from urllib.parse import quote

from ..config import env
from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago, check, http_client, iso, tail

COLOR = {
    "blue": "Success", "green": "Success", "red": "Failure", "yellow": "Unstable", "aborted": "Aborted",
    "notbuilt": "NotBuilt", "disabled": "Disabled", "grey": "Pending",
}


def job_path(job: str) -> str:
    parts = [p for p in job.strip("/").split("/") if p]
    if not parts:
        raise OpError("job name required")
    return "".join(f"/job/{quote(p, safe='')}" for p in parts)


def _status(color: str | None) -> str:
    if not color:
        return "Folder"
    if color.endswith("_anime"):
        return "Building"
    return COLOR.get(color, color.title())


class Jenkins(Integration):
    name = "jenkins"
    title = "Jenkins"
    category = "Delivery"
    icon = "jenkins"
    description = "CI/CD job health, build history and console logs."
    required_env = ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN")

    def _client(self):
        return http_client(self.settings, env("JENKINS_URL"), user=env("JENKINS_USER"), token=env("JENKINS_TOKEN"))

    def health(self):
        with self._client() as c:
            r = check(c.get("/api/json", params={"tree": "mode"}))
        return {"ok": True, "detail": f"Jenkins {r.headers.get('x-jenkins', '?')}"}

    @read_op("List jobs (optionally inside a folder) with last build result.", (
        Param("folder", description="Folder path, e.g. team/service; empty for root"),
        Param("failed_only", "boolean", default=False),
    ))
    def jobs(self, folder=None, failed_only=False):
        base = job_path(folder) if folder else ""
        tree = "jobs[name,fullName,url,color,lastBuild[number,result,timestamp,duration,building]]"
        with self._client() as c:
            items = check(c.get(f"{base}/api/json", params={"tree": tree})).json().get("jobs", [])
        rows = []
        for j in items:
            lb = j.get("lastBuild") or {}
            rows.append({
                "job": j.get("fullName") or j["name"], "status": _status(j.get("color")),
                "last_build": lb.get("number"), "result": "Building" if lb.get("building") else lb.get("result"),
                "when": iso(lb.get("timestamp")), "duration_s": int((lb.get("duration") or 0) / 1000),
                "url": j.get("url"),
            })
        if failed_only:
            rows = [r for r in rows if r["status"] in ("Failure", "Unstable")]
        return rows

    @read_op("Recent builds of a job.", (
        Param("job", required=True), Param("limit", "integer", default=15),
    ))
    def builds(self, job, limit=15):
        tree = f"builds[number,result,timestamp,duration,building,url]{{0,{min(limit, 100)}}}"
        with self._client() as c:
            items = check(c.get(f"{job_path(job)}/api/json", params={"tree": tree})).json().get("builds", [])
        return [{"job": job, "number": b["number"], "result": "Building" if b.get("building") else b.get("result"),
                 "when": iso(b.get("timestamp")), "duration_s": int((b.get("duration") or 0) / 1000),
                 "url": b.get("url")} for b in items]

    @read_op("Console output (tail) of a build.", (
        Param("job", required=True),
        Param("number", description="Build number; empty for last build"),
        Param("lines", "integer", default=300),
    ))
    def console(self, job, number=None, lines=300):
        build = number or "lastBuild"
        with self._client() as c:
            text = check(c.get(f"{job_path(job)}/{quote(str(build))}/consoleText")).text
        return {"job": job, "build": build, "log": tail(text, min(lines, 5000))}

    @action("Trigger a build of a job. Parameters as KEY=VALUE lines.", (
        Param("job", required=True),
        Param("parameters", description="Optional KEY=VALUE per line for parameterized jobs"),
    ), risk="medium")
    def build(self, job, parameters=None):
        data = {}
        for line in (parameters or "").splitlines():
            if line.strip():
                if "=" not in line:
                    raise OpError(f"bad parameter line '{line}', expected KEY=VALUE")
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        endpoint = "buildWithParameters" if data else "build"
        with self._client() as c:
            r = check(c.post(f"{job_path(job)}/{endpoint}", data=data or None))
        return {"queued": job, "queue_url": r.headers.get("location")}

    def describe_action(self, op, params):
        if op == "build":
            extra = f" with parameters:\n{params['parameters']}" if params.get("parameters") else ""
            return f"Trigger Jenkins build of '{params['job']}'{extra}"
        return super().describe_action(op, params)

    def widget(self):
        return [
            Section("Jobs", "jobs", inputs=["folder", "failed_only"],
                    params={"folder": env("JENKINS_DEFAULT_FOLDER", "")},
                    columns=[Column("job", fmt="code"), Column("status", fmt="status"),
                             Column("last_build", fmt="num"), Column("result", fmt="status"),
                             Column("when", fmt="time"), Column("duration_s", "duration", fmt="duration"),
                             Column("url", "link", fmt="link")],
                    row_actions=[RowAction("builds", "History", {"job": "job"}),
                                 RowAction("console", "Console", {"job": "job"}),
                                 RowAction("build", "Build", {"job": "job"}, inputs=["parameters"])],
                    refresh_secs=60),
        ]

    def attention(self):
        failed = self.invoke("jobs", {"folder": env("JENKINS_DEFAULT_FOLDER"), "failed_only": True})
        if not failed:
            return []
        return [{"severity": "warning", "title": f"{len(failed)} Jenkins job(s) failing",
                 "detail": ", ".join(f"{j['job']} #{j['last_build']}" for j in failed[:6])}]

    # -------------------------------------------------------------- demo
    _JOBS = [
        ("payments/payments-api/main", "Success", 1842, "SUCCESS", 2, 412),
        ("payments/payments-worker/main", "Failure", 977, "FAILURE", 1, 233),
        ("checkout/checkout-web/main", "Success", 3310, "SUCCESS", 5, 640),
        ("search/search-api/main", "Unstable", 512, "UNSTABLE", 3, 701),
        ("platform/terraform-plan", "Building", 88, "Building", 0.1, 0),
    ]

    def demo_jobs(self, folder=None, failed_only=False):
        rows = [{"job": j, "status": s, "last_build": n, "result": r, "when": ago(hours=h), "duration_s": d,
                 "url": f"https://jenkins.example.com{job_path(j)}/"}
                for j, s, n, r, h, d in self._JOBS if not folder or j.startswith(folder.strip("/") + "/")]
        if failed_only:
            rows = [r for r in rows if r["status"] in ("Failure", "Unstable")]
        return rows

    def demo_builds(self, job, limit=15):
        results = ["FAILURE", "FAILURE", "SUCCESS", "SUCCESS", "SUCCESS", "ABORTED", "SUCCESS"]
        return [{"job": job, "number": 977 - i, "result": results[i % len(results)], "when": ago(hours=6 * i + 1),
                 "duration_s": 230 + i * 7, "url": f"https://jenkins.example.com{job_path(job)}/{977 - i}/"}
                for i in range(min(limit, 10))]

    def demo_console(self, job, number=None, lines=300):
        return {"job": job, "build": number or "lastBuild", "log": (
            "Started by GitHub push by dev-jane\n[Pipeline] stage (Test)\n"
            "+ go test ./...\nok   payments/worker/config  0.21s\n"
            "--- FAIL: TestSettlementBatch (1.92s)\n    batch_test.go:88: expected 120 settled, got 118\n"
            "FAIL payments/worker/settlement 2.33s\nscript returned exit code 1\nFinished: FAILURE")}

    def demo_build(self, job, parameters=None):
        return {"queued": job, "queue_url": "https://jenkins.example.com/queue/item/4411/", "demo": True}
