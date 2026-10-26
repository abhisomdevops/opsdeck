"""GitHub (github.com or GitHub Enterprise Server): code, commits, PRs, Actions,
and opening pull requests.

GITHUB_TOKEN: a fine-grained token (or a GitHub App installation token) with
read access to contents, pull requests, actions and metadata; add *write* on
contents and pull requests only if this console should open PRs.
GITHUB_API_URL: https://ghe.acme.com/api/v3 for GitHub Enterprise Server.
GITHUB_ORG: default owner for repository lists and code search.

Opening a PR is an [ACTION]: the confirmation dialog shows a unified diff of
every file against the base branch, and the change lands on a new branch, so
nothing is ever pushed to the default branch.
"""

from __future__ import annotations

import base64
import difflib
import re

from ..config import env
from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago, check, clip, http_client, iso

MAX_FILE_CHARS = 60000
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
CHANGE_ITEMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path in the repository"},
        "content": {"type": "string", "description": "COMPLETE new file content"},
        "delete": {"type": "boolean", "description": "Delete this file instead"},
    },
    "required": ["path"],
}


def _repo(repo: str) -> str:
    repo = (repo or "").strip().removeprefix("https://github.com/").rstrip("/")
    if "/" not in repo and env("GITHUB_ORG"):
        repo = f"{env('GITHUB_ORG')}/{repo}"
    if not _REPO.match(repo):
        raise OpError("repo must look like owner/name")
    return repo


def _branch_name(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"
    return f"opsdeck/{slug}"


def unified_diff(path: str, old: str | None, new: str | None) -> str:
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, fromfile=f"a/{path}" if old is not None else "/dev/null",
                                tofile=f"b/{path}" if new is not None else "/dev/null")
    return "".join(diff) or f"(no change to {path})\n"


class GitHub(Integration):
    name = "github"
    title = "GitHub"
    category = "Delivery"
    icon = "git"
    description = "Code search, files, commits, pull requests, Actions runs, and opening PRs."
    required_env = ("GITHUB_TOKEN",)

    def _client(self):
        return http_client(self.settings, env("GITHUB_API_URL", "https://api.github.com"), token=env("GITHUB_TOKEN"),
                           headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def health(self):
        with self._client() as c:
            me = check(c.get("/user")).json()
        return {"ok": True, "detail": f"as {me.get('login')}"}

    # ------------------------------------------------------------ reads
    @read_op("List repositories for an org (default GITHUB_ORG) or the token's user.", (
        Param("org", description="Organization or user"), Param("limit", "integer", default=50),
    ))
    def repos(self, org=None, limit=50):
        org = org or env("GITHUB_ORG")
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        with self._client() as c:
            data = check(c.get(path, params={"per_page": min(limit, 100), "sort": "pushed"})).json()
        return [{"repo": r["full_name"], "default_branch": r.get("default_branch"), "language": r.get("language"),
                 "pushed": iso(r.get("pushed_at")), "archived": r.get("archived"), "url": r.get("html_url")}
                for r in data]

    @read_op("Search code. Use distinctive strings (config keys, error text, resource names).", (
        Param("query", required=True, description="Search terms, GitHub code search syntax"),
        Param("repo", description="Limit to owner/name"),
        Param("limit", "integer", default=20),
    ))
    def search_code(self, query, repo=None, limit=20):
        q = query
        if repo:
            q += f" repo:{_repo(repo)}"
        elif env("GITHUB_ORG") and "org:" not in q and "repo:" not in q:
            q += f" org:{env('GITHUB_ORG')}"
        with self._client() as c:
            data = check(c.get("/search/code", params={"q": q, "per_page": min(limit, 50)},
                               headers={"Accept": "application/vnd.github.text-match+json"})).json()
        rows = []
        for it in data.get("items", []):
            frag = " … ".join(m.get("fragment", "") for m in it.get("text_matches", [])[:2])
            rows.append({"repo": it["repository"]["full_name"], "path": it["path"],
                         "match": frag[:400], "url": it.get("html_url")})
        return rows

    @read_op("Read a file from a repository.", (
        Param("repo", required=True), Param("path", required=True), Param("ref", description="Branch, tag or SHA"),
    ))
    def file(self, repo, path, ref=None):
        repo = _repo(repo)
        with self._client() as c:
            r = c.get(f"/repos/{repo}/contents/{path.lstrip('/')}", params={"ref": ref} if ref else None)
            if r.status_code == 404:
                raise OpError(f"{path} not found in {repo}{'@' + ref if ref else ''}", 404)
            data = check(r).json()
        if isinstance(data, list):
            return {"repo": repo, "path": path, "type": "dir",
                    "entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in data]}
        text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return {"repo": repo, "path": path, "ref": ref, "sha": data.get("sha"), "size": data.get("size"),
                "text": clip(text, MAX_FILE_CHARS)}

    @read_op("List file paths in a repository (optionally under a prefix). Good for finding manifests.", (
        Param("repo", required=True), Param("ref", description="Branch (default: repo default)"),
        Param("prefix", description="Only paths starting with this"),
        Param("pattern", description="Regex the path must match, e.g. (Dockerfile|requirements.*txt|go.mod)$"),
    ))
    def tree(self, repo, ref=None, prefix=None, pattern=None):
        repo = _repo(repo)
        with self._client() as c:
            ref = ref or check(c.get(f"/repos/{repo}")).json()["default_branch"]
            data = check(c.get(f"/repos/{repo}/git/trees/{ref}", params={"recursive": "1"})).json()
        rx = re.compile(pattern) if pattern else None
        paths = [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"
                 and (not prefix or t["path"].startswith(prefix)) and (not rx or rx.search(t["path"]))]
        return [{"path": p} for p in paths[:2000]]

    @read_op("Recent commits on a branch (optionally for one path).", (
        Param("repo", required=True), Param("branch"), Param("path"),
        Param("hours", "integer", "Look-back window", default=72), Param("limit", "integer", default=30),
    ))
    def commits(self, repo, branch=None, path=None, hours=72, limit=30):
        import datetime as dt

        repo = _repo(repo)
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
        params = {"since": since, "per_page": min(limit, 100)}
        if branch:
            params["sha"] = branch
        if path:
            params["path"] = path
        with self._client() as c:
            data = check(c.get(f"/repos/{repo}/commits", params=params)).json()
        return [{"sha": x["sha"][:10], "message": (x["commit"]["message"] or "").splitlines()[0][:200],
                 "author": (x["commit"].get("author") or {}).get("name"),
                 "when": iso((x["commit"].get("author") or {}).get("date")), "url": x.get("html_url")} for x in data]

    @read_op("Pull requests.", (
        Param("repo", required=True), Param("state", enum=["open", "closed", "all"], default="open"),
        Param("limit", "integer", default=30),
    ))
    def pulls(self, repo, state="open", limit=30):
        repo = _repo(repo)
        with self._client() as c:
            data = check(c.get(f"/repos/{repo}/pulls", params={"state": state, "per_page": min(limit, 100),
                                                                "sort": "updated", "direction": "desc"})).json()
        return [{"number": p["number"], "title": p["title"], "state": "merged" if p.get("merged_at") else p["state"],
                 "author": (p.get("user") or {}).get("login"), "branch": (p.get("head") or {}).get("ref"),
                 "updated": iso(p.get("updated_at")), "merged": iso(p.get("merged_at")), "url": p.get("html_url")}
                for p in data]

    @read_op("GitHub Actions workflow runs.", (
        Param("repo", required=True), Param("status", enum=["failure", "success", "in_progress", "queued"]),
        Param("branch"), Param("limit", "integer", default=20),
    ))
    def workflow_runs(self, repo, status=None, branch=None, limit=20):
        repo = _repo(repo)
        params = {"per_page": min(limit, 100)}
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        with self._client() as c:
            data = check(c.get(f"/repos/{repo}/actions/runs", params=params)).json()
        return [{"id": r["id"], "workflow": r.get("name"), "branch": r.get("head_branch"),
                 "sha": (r.get("head_sha") or "")[:10], "event": r.get("event"),
                 "status": r.get("conclusion") or r.get("status"), "started": iso(r.get("run_started_at")),
                 "url": r.get("html_url")} for r in data.get("workflow_runs", [])]

    @read_op("What changed between two refs (tags, SHAs, branches): commits and files. "
             "Use for deploy-to-deploy diffs.", (
                 Param("repo", required=True), Param("base", required=True), Param("head", required=True),
             ))
    def compare(self, repo, base, head):
        repo = _repo(repo)
        with self._client() as c:
            d = check(c.get(f"/repos/{repo}/compare/{base}...{head}")).json()
        return {
            "status": d.get("status"), "ahead_by": d.get("ahead_by"),
            "commits": [{"sha": x["sha"][:10], "message": x["commit"]["message"].splitlines()[0][:200],
                         "author": (x["commit"].get("author") or {}).get("name")} for x in d.get("commits", [])[-50:]],
            "files": [{"path": f["filename"], "status": f["status"], "changes": f.get("changes"),
                       "patch": clip(f.get("patch") or "", 3000)} for f in d.get("files", [])[:60]],
        }

    # ---------------------------------------------------------- actions
    @action("Open a pull request with file changes on a new branch. Send the COMPLETE content of each changed "
            "file (not a diff).", (
                Param("repo", required=True),
                Param("title", required=True),
                Param("body", required=True, description="PR description: why, what, test plan, ticket link"),
                Param("changes", "array", "Files to create/replace/delete", required=True, items=CHANGE_ITEMS),
                Param("base", description="Base branch (default: repo default branch)"),
                Param("branch", description="New branch name (default: opsdeck/<title-slug>)"),
                Param("draft", "boolean", "Open as draft", default=True),
            ), risk="medium")
    def create_pull_request(self, repo, title, body, changes, base=None, branch=None, draft=True):
        repo = _repo(repo)
        self._validate_changes(changes)
        branch = branch or _branch_name(title)
        with self._client() as c:
            base = base or check(c.get(f"/repos/{repo}")).json()["default_branch"]
            base_sha = check(c.get(f"/repos/{repo}/git/ref/heads/{base}")).json()["object"]["sha"]
            base_tree = check(c.get(f"/repos/{repo}/git/commits/{base_sha}")).json()["tree"]["sha"]
            tree = []
            for ch in changes:
                if ch.get("delete"):
                    tree.append({"path": ch["path"], "mode": "100644", "type": "blob", "sha": None})
                else:
                    blob = check(c.post(f"/repos/{repo}/git/blobs",
                                        json={"content": ch["content"], "encoding": "utf-8"})).json()
                    tree.append({"path": ch["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]})
            new_tree = check(c.post(f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": tree})).json()
            commit = check(c.post(f"/repos/{repo}/git/commits", json={
                "message": f"{title}\n\n{body[:2000]}", "tree": new_tree["sha"], "parents": [base_sha]})).json()
            check(c.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]}))
            pr = check(c.post(f"/repos/{repo}/pulls", json={
                "title": title, "head": branch, "base": base, "body": body, "draft": draft})).json()
        return {"pr": pr["number"], "url": pr.get("html_url"), "branch": branch, "base": base}

    @staticmethod
    def _validate_changes(changes):
        if not changes:
            raise OpError("changes must list at least one file")
        if len(changes) > 50:
            raise OpError("at most 50 files per PR")
        for ch in changes:
            if not isinstance(ch, dict) or not ch.get("path"):
                raise OpError("each change needs a path")
            if ".." in ch["path"].split("/") or ch["path"].startswith("/"):
                raise OpError(f"invalid path {ch['path']}")
            if not ch.get("delete") and not isinstance(ch.get("content"), str):
                raise OpError(f"{ch['path']}: content (complete file) is required unless delete=true")

    def describe_action(self, op, params):
        if op != "create_pull_request":
            return super().describe_action(op, params)
        self._validate_changes(params["changes"])
        repo = _repo(params["repo"])
        head = (f"Open {'draft ' if params.get('draft') else ''}PR in {repo}: {params['title']}\n"
                f"Branch {params.get('branch') or _branch_name(params['title'])} -> {params.get('base') or 'default'}\n\n")
        diffs = []
        for ch in params["changes"]:
            try:
                cur = self.invoke("file", {"repo": repo, "path": ch["path"], "ref": params.get("base")}).get("text")
            except OpError:
                cur = None
            diffs.append(unified_diff(ch["path"], cur, None if ch.get("delete") else ch["content"]))
        return head + "".join(diffs)[:20000]

    # ----------------------------------------------------------- widget
    def widget(self):
        org_repo = env("GITHUB_DEFAULT_REPO", "acme/payments-worker" if self.mode == "demo" else "")
        return [
            Section("Code search", "search_code", inputs=["query", "repo"],
                    params={"query": "PAYMENTS_DB_DSN" if self.mode == "demo" else "", "repo": ""},
                    columns=[Column("repo"), Column("path", fmt="code"), Column("match", fmt="code"),
                             Column("url", fmt="link")],
                    row_actions=[RowAction("file", "View", {"repo": "repo", "path": "path"})]),
            Section("Recent commits", "commits", inputs=["repo", "hours"], params={"repo": org_repo, "hours": 72},
                    columns=[Column("sha", fmt="code"), Column("message"), Column("author"), Column("when", fmt="time"),
                             Column("url", fmt="link")], width="half"),
            Section("Pull requests", "pulls", inputs=["repo", "state"], params={"repo": org_repo, "state": "all"},
                    columns=[Column("number", fmt="num"), Column("title"), Column("state", fmt="status"),
                             Column("author"), Column("updated", fmt="time"), Column("url", fmt="link")], width="half"),
            Section("Actions runs", "workflow_runs", inputs=["repo", "status"], params={"repo": org_repo},
                    columns=[Column("workflow"), Column("branch"), Column("sha", fmt="code"),
                             Column("status", fmt="status"), Column("started", fmt="time"), Column("url", fmt="link")]),
        ]

    def attention(self):
        repo = env("GITHUB_DEFAULT_REPO") or ("acme/payments-worker" if self.mode == "demo" else None)
        if not repo:
            return []
        failed = self.invoke("workflow_runs", {"repo": repo, "status": "failure", "branch": None, "limit": 5})
        if not failed:
            return []
        return [{"severity": "warning", "title": f"{len(failed)} failed Actions run(s) in {repo}",
                 "detail": ", ".join(f"{r['workflow']} on {r['branch']}" for r in failed[:3])}]

    # ------------------------------------------------------------- demo
    _FILES = {
        ("acme/payments-worker", "deploy/helm/values-prod.yaml"): (
            "image:\n  repository: registry.example.com/payments-worker\n  tag: 1.42.0\n"
            "replicas: 1\nenv:\n  - name: PAYMENTS_DSN\n    valueFrom:\n      secretKeyRef:\n"
            "        name: payments-db\n        key: dsn\n  - name: KAFKA_BOOTSTRAP\n    value: kafka-0.kafka:9092\n"),
        ("acme/payments-worker", "worker/config.py"): (
            "import os\n\n\ndef load():\n    dsn = os.environ.get(\"PAYMENTS_DB_DSN\")\n    if not dsn:\n"
            "        raise SystemExit(\"missing required env PAYMENTS_DB_DSN\")\n    return {\"dsn\": dsn}\n"),
        ("acme/payments-worker", "Dockerfile"): (
            "FROM python:3.11.4-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
            "COPY worker/ worker/\nCMD [\"python\", \"-m\", \"worker\"]\n"),
        ("acme/payments-worker", "requirements.txt"): "requests==2.31.0\nkafka-python==2.0.2\npsycopg[binary]==3.1.18\n",
        ("acme/checkout-web", "go.mod"): (
            "module github.com/acme/checkout-web\n\ngo 1.21\n\nrequire (\n\tgolang.org/x/net v0.15.0\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n)\n"),
        ("acme/platform-infra", "terraform/eks/main.tf"): (
            "module \"eks\" {\n  source          = \"terraform-aws-modules/eks/aws\"\n  version         = \"20.8.4\"\n"
            "  cluster_name    = \"prod-eks\"\n  cluster_version = \"1.30\"\n  cluster_endpoint_public_access = true\n}\n"),
    }

    def demo_repos(self, org=None, limit=50):
        return [{"repo": r, "default_branch": "main", "language": lang, "pushed": ago(hours=h), "archived": False,
                 "url": f"https://github.com/{r}"} for r, lang, h in [
                     ("acme/payments-worker", "Python", 2), ("acme/checkout-web", "Go", 20),
                     ("acme/platform-infra", "HCL", 50), ("acme/search-api", "Java", 6)]][:limit]

    def demo_search_code(self, query, repo=None, limit=20):
        words = [w.lower() for w in re.findall(r"[\w.-]+", query) if ":" not in w]
        rows = []
        for (r, path), text in self._FILES.items():
            if repo and _repo(repo) != r:
                continue
            for line in text.splitlines():
                if any(w in line.lower() for w in words):
                    rows.append({"repo": r, "path": path, "match": line.strip()[:200],
                                 "url": f"https://github.com/{r}/blob/main/{path}"})
                    break
        return rows[:limit]

    def demo_file(self, repo, path, ref=None):
        repo = _repo(repo)
        text = self._FILES.get((repo, path.lstrip("/")))
        if text is None:
            raise OpError(f"{path} not found in {repo}", 404)
        return {"repo": repo, "path": path, "ref": ref, "sha": "demo", "size": len(text), "text": text}

    def demo_tree(self, repo, ref=None, prefix=None, pattern=None):
        repo = _repo(repo)
        rx = re.compile(pattern) if pattern else None
        return [{"path": p} for (r, p) in self._FILES if r == repo
                and (not prefix or p.startswith(prefix)) and (not rx or rx.search(p))]

    def demo_commits(self, repo, branch=None, path=None, hours=72, limit=30):
        repo = _repo(repo)
        rows = {
            "acme/payments-worker": [
                ("9f2c1ab3d4", "Rename DB secret key to PAYMENTS_DSN in Helm values (#482)", "Jo Park", 2),
                ("71aa0e9c2b", "Release 1.42.0", "release-bot", 2.1),
                ("5b3e77f0aa", "Batch settlement retries with jitter (#479)", "Dana Ruiz", 30),
            ],
            "acme/checkout-web": [("c0ffee1234", "Bump gin to 1.9.1", "Sam Patel", 40)],
        }.get(repo, [])
        return [{"sha": s, "message": m, "author": a, "when": ago(hours=h), "url": f"https://github.com/{repo}/commit/{s}"}
                for s, m, a, h in rows][:limit]

    def demo_pulls(self, repo, state="open", limit=30):
        repo = _repo(repo)
        rows = [(482, "Rename DB secret key to PAYMENTS_DSN in Helm values", "merged", "jpark", "secret-rename", 2),
                (485, "Add readiness probe for kafka connectivity", "open", "druiz", "probe", 5)]
        return [{"number": n, "title": t, "state": s, "author": a, "branch": b, "updated": ago(hours=h),
                 "merged": ago(hours=h) if s == "merged" else None, "url": f"https://github.com/{repo}/pull/{n}"}
                for n, t, s, a, b, h in rows if state == "all" or (state == "open") == (s == "open")][:limit]

    def demo_workflow_runs(self, repo, status=None, branch=None, limit=20):
        rows = [(1, "release", "main", "71aa0e9c2b", "success", 2.1), (2, "ci", "probe", "e4d1f00b9a", "failure", 5),
                (3, "ci", "main", "9f2c1ab3d4", "success", 2)]
        return [{"id": i, "workflow": w, "branch": b, "sha": s, "event": "push", "status": st, "started": ago(hours=h),
                 "url": f"https://github.com/{repo}/actions/runs/{i}"} for i, w, b, s, st, h in rows
                if (not status or st == status) and (not branch or b == branch)][:limit]

    def demo_compare(self, repo, base, head):
        return {"status": "ahead", "ahead_by": 2,
                "commits": [{"sha": "71aa0e9c2b", "message": "Release 1.42.0", "author": "release-bot"},
                            {"sha": "9f2c1ab3d4", "message": "Rename DB secret key to PAYMENTS_DSN in Helm values (#482)",
                             "author": "Jo Park"}],
                "files": [{"path": "deploy/helm/values-prod.yaml", "status": "modified", "changes": 2,
                           "patch": "@@ -4,3 +4,3 @@ env:\n-  - name: PAYMENTS_DB_DSN\n+  - name: PAYMENTS_DSN"}]}

    def demo_create_pull_request(self, repo, title, body, changes, base=None, branch=None, draft=True):
        self._validate_changes(changes)
        return {"pr": 486, "url": f"https://github.com/{_repo(repo)}/pull/486", "branch": branch or _branch_name(title),
                "base": base or "main", "demo": True}
