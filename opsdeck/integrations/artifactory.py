"""JFrog Artifactory: repositories, recently published artifacts, image tags.

ARTIFACTORY_URL=https://acme.jfrog.io/artifactory, ARTIFACTORY_TOKEN=access
token (an identity token for a read-only user is enough).
"""

from __future__ import annotations

import json

from ..config import env
from ..registry import Column, Integration, OpError, Param, RowAction, Section, read_op
from ._common import ago, check, http_client, iso


class Artifactory(Integration):
    name = "artifactory"
    title = "Artifactory"
    category = "Delivery"
    icon = "package"
    description = "Artifact and container image registry."
    required_env = ("ARTIFACTORY_URL", "ARTIFACTORY_TOKEN")

    def _client(self):
        return http_client(self.settings, env("ARTIFACTORY_URL"), token=env("ARTIFACTORY_TOKEN"))

    def health(self):
        with self._client() as c:
            check(c.get("/api/system/ping"))
            v = check(c.get("/api/system/version")).json()
        return {"ok": True, "detail": f"Artifactory {v.get('version', '?')}"}

    @read_op("List repositories.", (Param("package_type", description="e.g. docker, maven, npm, helm"),))
    def repositories(self, package_type=None):
        params = {"packageType": package_type} if package_type else {}
        with self._client() as c:
            items = check(c.get("/api/repositories", params=params)).json()
        return [{"key": r["key"], "type": r.get("type"), "package_type": r.get("packageType"),
                 "url": r.get("url")} for r in items]

    @read_op("Artifacts modified recently (AQL), newest first.", (
        Param("repo", description="Repository key; empty for all"),
        Param("hours", "integer", default=24), Param("limit", "integer", default=50),
    ))
    def recent(self, repo=None, hours=24, limit=50):
        criteria: dict = {"modified": {"$last": f"{max(1, min(hours, 720))}h"}, "type": "file"}
        if repo:
            criteria["repo"] = repo
        aql = (f"items.find({json.dumps(criteria)})"
               '.include("repo","path","name","size","modified","modified_by")'
               f'.sort({{"$desc":["modified"]}}).limit({min(limit, 500)})')
        with self._client() as c:
            res = check(c.post("/api/search/aql", content=aql, headers={"Content-Type": "text/plain"})).json()
        return [{"repo": r["repo"], "path": f"{r['path']}/{r['name']}".lstrip("./"), "size": r.get("size"),
                 "modified": iso(r.get("modified")), "by": r.get("modified_by")}
                for r in res.get("results", [])]

    @read_op("Tags of a Docker image in a Docker repository.", (
        Param("repo", required=True), Param("image", required=True, description="e.g. team/service"),
    ))
    def docker_tags(self, repo, image):
        if ".." in image:
            raise OpError("invalid image name")
        with self._client() as c:
            res = check(c.get(f"/api/docker/{repo}/v2/{image}/tags/list")).json()
        return [{"image": f"{image}:{t}", "tag": t} for t in reversed(res.get("tags") or [])]

    def widget(self):
        return [
            Section("Recently published", "recent", inputs=["repo", "hours"],
                    params={"repo": env("ARTIFACTORY_DEFAULT_REPO", ""), "hours": 24, "limit": 50},
                    columns=[Column("repo"), Column("path", fmt="code"), Column("size", fmt="bytes"),
                             Column("modified", fmt="time"), Column("by")], refresh_secs=300),
            Section("Repositories", "repositories", inputs=["package_type"],
                    columns=[Column("key", fmt="code"), Column("type"), Column("package_type"),
                             Column("url", "link", fmt="link")],
                    row_actions=[RowAction("recent", "Recent", {"repo": "key"})]),
        ]

    # -------------------------------------------------------------- demo
    def demo_repositories(self, package_type=None):
        rows = [("docker-local", "LOCAL", "Docker"), ("docker-remote", "REMOTE", "Docker"),
                ("helm-local", "LOCAL", "Helm"), ("libs-release", "LOCAL", "Maven"), ("npm-virtual", "VIRTUAL", "Npm")]
        return [{"key": k, "type": t, "package_type": p, "url": f"https://acme.jfrog.io/artifactory/{k}"}
                for k, t, p in rows if not package_type or p.lower() == package_type.lower()]

    def demo_recent(self, repo=None, hours=24, limit=50):
        rows = [
            ("docker-local", "payments-worker/1.42.0/manifest.json", 2241, 2, "ci-jenkins"),
            ("docker-local", "payments-api/1.42.0/manifest.json", 2239, 2, "ci-jenkins"),
            ("helm-local", "payments/payments-1.42.0.tgz", 18822, 3, "ci-jenkins"),
            ("docker-local", "checkout-web/3.8.1/manifest.json", 2101, 9, "ci-jenkins"),
            ("libs-release", "com/acme/ledger/2.4.0/ledger-2.4.0.jar", 4_822_113, 20, "ci-jenkins"),
        ]
        return [{"repo": r, "path": p, "size": s, "modified": ago(hours=h), "by": b}
                for r, p, s, h, b in rows if (not repo or r == repo) and h <= hours][:limit]

    def demo_docker_tags(self, repo, image):
        return [{"image": f"{image}:{t}", "tag": t} for t in ["1.42.0", "1.41.3", "1.41.2", "1.41.1", "latest"]]
