"""Jira: search issues with JQL, view issues, add comments.

Cloud: JIRA_URL=https://acme.atlassian.net, JIRA_USER=email, JIRA_TOKEN=API token.
Server/Data Center: JIRA_URL=https://jira.acme.com, JIRA_TOKEN=personal access
token (leave JIRA_USER empty to use Bearer auth).
"""

from __future__ import annotations

from ..config import env
from ..registry import Column, Integration, Param, RowAction, Section, action, read_op
from ._common import ago, check, http_client, iso

FIELDS = "summary,status,priority,assignee,updated,issuetype,labels"
DEFAULT_JQL = "statusCategory != Done AND priority in (Highest, High) ORDER BY updated DESC"


def is_cloud() -> bool:
    return "atlassian.net" in (env("JIRA_URL") or "") or env("JIRA_DEPLOYMENT") == "cloud"


class Jira(Integration):
    name = "jira"
    title = "Jira"
    category = "Work"
    icon = "jira"
    description = "Incidents, ops tickets and change work tracked in Jira."
    required_env = ("JIRA_URL", "JIRA_TOKEN")

    def _client(self):
        return http_client(self.settings, env("JIRA_URL"), user=env("JIRA_USER"), token=env("JIRA_TOKEN"))

    def health(self):
        with self._client() as c:
            me = check(c.get("/rest/api/2/myself")).json()
        return {"ok": True, "detail": f"as {me.get('displayName') or me.get('name')}"}

    @staticmethod
    def _row(issue: dict) -> dict:
        f = issue.get("fields", {})
        return {
            "key": issue["key"], "type": (f.get("issuetype") or {}).get("name"),
            "summary": f.get("summary"), "status": (f.get("status") or {}).get("name"),
            "priority": (f.get("priority") or {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "updated": iso(f.get("updated")),
            "url": f"{env('JIRA_URL', '').rstrip('/')}/browse/{issue['key']}",
        }

    @read_op("Search issues with JQL.", (
        Param("jql", description="JQL query", default=None),
        Param("limit", "integer", default=30),
    ))
    def search(self, jql=None, limit=30):
        jql = jql or env("JIRA_DEFAULT_JQL", DEFAULT_JQL)
        params = {"jql": jql, "maxResults": min(limit, 100), "fields": FIELDS}
        # Jira Cloud retired /rest/api/*/search in favour of /search/jql.
        path = "/rest/api/3/search/jql" if is_cloud() else "/rest/api/2/search"
        with self._client() as c:
            data = check(c.get(path, params=params)).json()
        return [self._row(i) for i in data.get("issues", [])]

    @read_op("Full details of one issue including recent comments.", (Param("key", required=True),))
    def issue(self, key):
        with self._client() as c:
            i = check(c.get(f"/rest/api/2/issue/{key}",
                            params={"fields": FIELDS + ",description,reporter,created,comment"})).json()
        f = i.get("fields", {})
        comments = (f.get("comment") or {}).get("comments", [])[-5:]
        out = self._row(i)
        out.update({
            "reporter": (f.get("reporter") or {}).get("displayName"), "created": iso(f.get("created")),
            "labels": ", ".join(f.get("labels") or []), "description": (f.get("description") or "")[:6000],
            "recent_comments": "\n\n".join(
                f"{(c.get('author') or {}).get('displayName')} ({c.get('created', '')[:16]}): {c.get('body', '')}"
                for c in comments)[:6000],
        })
        return out

    @action("Add a comment to an issue.", (
        Param("key", required=True), Param("body", required=True, description="Comment text"),
    ), risk="low")
    def add_comment(self, key, body):
        with self._client() as c:
            r = check(c.post(f"/rest/api/2/issue/{key}/comment", json={"body": body})).json()
        return {"commented": key, "id": r.get("id")}

    def describe_action(self, op, params):
        if op == "add_comment":
            return f"Add comment to {params['key']}:\n\n{params['body']}"
        return super().describe_action(op, params)

    def widget(self):
        cols = [Column("key", fmt="code"), Column("type"), Column("summary"), Column("status", fmt="status"),
                Column("priority", fmt="status"), Column("assignee"), Column("updated", fmt="time"),
                Column("url", "link", fmt="link")]
        actions = [RowAction("issue", "Open", {"key": "key"}),
                   RowAction("add_comment", "Comment", {"key": "key"}, inputs=["body"])]
        sections = [Section("Issues", "search", inputs=["jql"],
                            params={"jql": env("JIRA_DEFAULT_JQL", DEFAULT_JQL)},
                            columns=cols, row_actions=actions, refresh_secs=120)]
        if env("JIRA_INCIDENT_JQL"):
            sections.insert(0, Section("Open incidents", "search", params={"jql": env("JIRA_INCIDENT_JQL")},
                                       columns=cols, row_actions=actions, refresh_secs=60))
        return sections

    # -------------------------------------------------------------- demo
    _ISSUES = [
        ("OPS-1412", "Incident", "payments-worker crashlooping after 1.42.0 deploy", "In Progress", "Highest",
         "Dana Ruiz", 0.2),
        ("OPS-1409", "Incident", "Elevated 5xx on checkout-web in us-east-1", "Monitoring", "High", "Sam Patel", 3),
        ("OPS-1398", "Task", "Rotate Artifactory service account token", "To Do", "High", "Unassigned", 26),
        ("SRE-877", "Story", "Add SLO burn-rate alerts for search-api", "In Review", "Medium", "Lee Chen", 50),
        ("CHG-221", "Change", "EKS 1.30 -> 1.31 upgrade (prod)", "Scheduled", "High", "Dana Ruiz", 70),
    ]

    def demo_search(self, jql=None, limit=30):
        return [{"key": k, "type": t, "summary": s, "status": st, "priority": p, "assignee": a,
                 "updated": ago(hours=h), "url": f"https://acme.atlassian.net/browse/{k}"}
                for k, t, s, st, p, a, h in self._ISSUES][:limit]

    def demo_issue(self, key):
        row = next((r for r in self.demo_search() if r["key"] == key), self.demo_search()[0])
        return dict(row, reporter="PagerDuty", created=ago(hours=5), labels="sev2, payments",
                    description="payments-worker pods entering CrashLoopBackOff since the 1.42.0 rollout.\n"
                                "Impact: settlement batch delayed. Rollback under evaluation.",
                    recent_comments="Sam Patel: logs show missing PAYMENTS_DB_DSN env var.\n\n"
                                    "Dana Ruiz: secret was renamed in the Helm values; fixing.")

    def demo_add_comment(self, key, body):
        return {"commented": key, "id": "100234", "demo": True}
