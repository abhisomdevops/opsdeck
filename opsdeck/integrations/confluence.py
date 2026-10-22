"""Confluence: search and read runbooks / postmortems.

Cloud: CONFLUENCE_URL=https://acme.atlassian.net/wiki, CONFLUENCE_USER=email,
CONFLUENCE_TOKEN=API token. Data Center: CONFLUENCE_URL=https://wiki.acme.com,
CONFLUENCE_TOKEN=personal access token.
"""

from __future__ import annotations

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, read_op
from ._common import ago, check, html_to_text, http_client, iso


def _cql_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Confluence(Integration):
    name = "confluence"
    title = "Confluence"
    category = "Work"
    icon = "book"
    description = "Runbooks, postmortems and service docs."
    required_env = ("CONFLUENCE_URL", "CONFLUENCE_TOKEN")

    def _client(self):
        return http_client(self.settings, env("CONFLUENCE_URL"), user=env("CONFLUENCE_USER"),
                           token=env("CONFLUENCE_TOKEN"))

    def health(self):
        with self._client() as c:
            check(c.get("/rest/api/space", params={"limit": 1}))
        return {"ok": True, "detail": "connected"}

    @read_op("Search pages by text (and optionally space).", (
        Param("query", required=True, description="Words to search for"),
        Param("space", description="Space key, e.g. SRE"),
        Param("limit", "integer", default=20),
    ))
    def search(self, query, space=None, limit=20):
        cql = f'type=page AND text ~ "{_cql_escape(query)}"'
        if space:
            cql += f' AND space="{_cql_escape(space)}"'
        cql += " ORDER BY lastmodified DESC"
        with self._client() as c:
            data = check(c.get("/rest/api/content/search", params={
                "cql": cql, "limit": min(limit, 50), "expand": "space,version"})).json()
        base = env("CONFLUENCE_URL").rstrip("/")
        return [{
            "id": r["id"], "title": r.get("title"), "space": (r.get("space") or {}).get("key"),
            "updated": iso((r.get("version") or {}).get("when")),
            "by": ((r.get("version") or {}).get("by") or {}).get("displayName"),
            "url": base + (r.get("_links") or {}).get("webui", ""),
        } for r in data.get("results", [])]

    @read_op("Read a page as plain text.", (Param("id", required=True, description="Page id"),))
    def page(self, id):
        with self._client() as c:
            p = check(c.get(f"/rest/api/content/{id}", params={"expand": "body.storage,version,space"})).json()
        return {"title": p.get("title"), "space": (p.get("space") or {}).get("key"),
                "updated": iso((p.get("version") or {}).get("when")),
                "text": html_to_text(((p.get("body") or {}).get("storage") or {}).get("value", ""))}

    def widget(self):
        cols = [Column("title"), Column("space"), Column("updated", fmt="time"), Column("by"),
                Column("url", "link", fmt="link")]
        return [Section("Search", "search", inputs=["query", "space"],
                        params={"query": env("CONFLUENCE_DEFAULT_QUERY", "runbook"),
                                "space": env("CONFLUENCE_DEFAULT_SPACE", "")},
                        columns=cols, row_actions=[RowAction("page", "Read", {"id": "id"})])]

    # -------------------------------------------------------------- demo
    _PAGES = [
        ("1001", "Runbook: payments-worker", "SRE", 12),
        ("1002", "Runbook: CrashLoopBackOff triage", "SRE", 40),
        ("1003", "Postmortem 2026-09-14: checkout 5xx", "SRE", 400),
        ("1004", "Runbook: EKS node group rotation", "PLAT", 900),
        ("1005", "Service catalog: search-api", "ENG", 2000),
    ]

    def demo_search(self, query, space=None, limit=20):
        q = (query or "").lower()
        rows = [{"id": i, "title": t, "space": s, "updated": ago(hours=h), "by": "Lee Chen",
                 "url": f"https://acme.atlassian.net/wiki/spaces/{s}/pages/{i}"}
                for i, t, s, h in self._PAGES if (not space or s == space)]
        hits = [r for r in rows if any(w in r["title"].lower() for w in q.split())]
        return (hits or rows)[:limit]

    def demo_page(self, id):
        return {"title": "Runbook: payments-worker", "space": "SRE", "updated": ago(hours=12), "text": (
            "Owner: Payments team (#payments-oncall)\n\nSymptoms\n- Pods in CrashLoopBackOff\n"
            "- Settlement lag alert firing\n\nTriage\n1. Check previous container logs.\n"
            "2. Verify secret payments-db has key PAYMENTS_DB_DSN.\n3. Check Argo CD app payments-prod diff.\n\n"
            "Mitigation\n- Roll back via Argo CD to the previous revision, or\n"
            "- Fix the Helm values and sync.\n\nEscalation: page payments secondary after 30 minutes.")}
