"""Exercise the live (non-demo) HTTP code paths against a mock transport."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from opsdeck.integrations import _common
from opsdeck.main import create_app

from .conftest import HDR, make_settings

SEEN: list[httpx.Request] = []


def handler(req: httpx.Request) -> httpx.Response:
    SEEN.append(req)
    path = req.url.path
    if path == "/rest/api/3/search/jql":
        return httpx.Response(200, json={"issues": [{"key": "OPS-1", "fields": {
            "summary": "s", "status": {"name": "Open"}, "priority": {"name": "High"},
            "assignee": None, "updated": "2026-10-01T00:00:00.000+0000", "issuetype": {"name": "Bug"}}}]})
    if path == "/job/team/job/svc/api/json":
        return httpx.Response(200, json={"jobs": [{"name": "main", "fullName": "team/svc/main", "color": "red_anime",
                                                    "url": "u", "lastBuild": {"number": 3, "building": True}}]})
    if path == "/api/v1/applications":
        return httpx.Response(200, json={"items": [{"metadata": {"name": "app"}, "spec": {
            "project": "p", "destination": {"namespace": "ns"}, "source": {"repoURL": "r", "path": "x"}},
            "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}}}]})
    if path == "/api/v1/applications/app/sync":
        return httpx.Response(200, json={"status": {"operationState": {"phase": "Running"}}})
    if path == "/artifactory/api/search/aql":
        assert req.headers["content-type"] == "text/plain"
        return httpx.Response(200, json={"results": [{"repo": "docker-local", "path": "a", "name": "b",
                                                      "size": 1, "modified": "2026-10-01T00:00:00Z"}]})
    if path == "/login":
        return httpx.Response(200, json={"token": "tok"})
    if path == "/v2/alert":
        assert req.headers["x-redlock-auth"] == "tok"
        return httpx.Response(200, json={"items": [
            {"id": "1", "policy": {"severity": "low", "name": "a"}, "resource": {}},
            {"id": "2", "policy": {"severity": "critical", "name": "b"}, "resource": {}}]})
    if path == "/wiki/rest/api/content/search":
        return httpx.Response(200, json={"results": [{"id": "9", "title": "Runbook", "space": {"key": "SRE"},
                                                      "_links": {"webui": "/spaces/SRE/pages/9"}}]})
    return httpx.Response(404, text=f"no route {path}")


@pytest.fixture
def live(tmp_path, monkeypatch):
    env = {
        "JIRA_URL": "https://acme.atlassian.net", "JIRA_USER": "me@acme.com", "JIRA_TOKEN": "t",
        "JENKINS_URL": "https://ci.acme.com", "JENKINS_USER": "u", "JENKINS_TOKEN": "t",
        "ARGOCD_URL": "https://argo.acme.com", "ARGOCD_TOKEN": "t",
        "ARTIFACTORY_URL": "https://acme.jfrog.io/artifactory", "ARTIFACTORY_TOKEN": "t",
        "PRISMA_URL": "https://api.prismacloud.io", "PRISMA_ACCESS_KEY": "k", "PRISMA_SECRET_KEY": "s",
        "CONFLUENCE_URL": "https://acme.atlassian.net/wiki", "CONFLUENCE_TOKEN": "t",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_common, "TEST_TRANSPORT", httpx.MockTransport(handler))
    from opsdeck.integrations.prisma import Prisma
    monkeypatch.setattr(Prisma, "_token", None)
    SEEN.clear()
    return TestClient(create_app(make_settings(tmp_path, enable_writes=True)))


def get(c, integ, op, **params):
    r = c.get(f"/api/integrations/{integ}/ops/{op}", params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_jira_cloud_uses_search_jql_with_basic_auth(live):
    rows = get(live, "jira", "search", jql="project = OPS")
    assert rows[0]["key"] == "OPS-1" and rows[0]["assignee"] == "Unassigned"
    assert SEEN[-1].headers["authorization"].startswith("Basic ")
    assert SEEN[-1].url.params["jql"] == "project = OPS"


def test_jenkins_folder_path_and_building_status(live):
    rows = get(live, "jenkins", "jobs", folder="team/svc")
    assert rows[0]["status"] == "Building" and rows[0]["result"] == "Building"


def test_argocd_list_and_live_sync(live):
    assert get(live, "argocd", "applications")[0]["sync"] == "OutOfSync"
    prep = live.post("/api/integrations/argocd/actions/sync/prepare", json={"params": {"name": "app"}},
                     headers=HDR).json()
    assert prep["mode"] == "live"
    res = live.post("/api/actions/execute", json={"token": prep["token"]}, headers=HDR).json()
    assert res["result"]["phase"] == "Running"
    assert json.loads(SEEN[-1].content) == {"prune": False}
    assert SEEN[-1].headers["authorization"] == "Bearer t"


def test_artifactory_aql(live):
    rows = get(live, "artifactory", "recent", repo="docker-local", hours=6)
    assert rows[0]["path"] == "a/b"
    assert '"repo": "docker-local"' in SEEN[-1].content.decode()


def test_prisma_login_and_summary(live):
    s = get(live, "prisma", "summary")
    assert s["critical"] == 1 and s["low"] == 1
    assert get(live, "prisma", "alerts")[0]["severity"] == "critical"


def test_confluence_search_link(live):
    rows = get(live, "confluence", "search", query='say "hi"')
    assert rows[0]["url"] == "https://acme.atlassian.net/wiki/spaces/SRE/pages/9"
    assert 'text ~ "say \\"hi\\""' in SEEN[-1].url.params["cql"]


def test_upstream_error_is_502(live):
    r = live.get("/api/integrations/argocd/ops/app_resources", params={"name": "missing"})
    assert r.status_code == 502 and "HTTP 404" in r.json()["detail"]
