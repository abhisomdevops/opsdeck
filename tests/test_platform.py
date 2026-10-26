"""Multi-provider agent, playbooks, widgets (hot reload, approval, sandbox bridge), AI builder, GitHub PRs."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from opsdeck.integrations import _common
from opsdeck.integrations.jira import issue_key
from opsdeck.main import create_app
from opsdeck.widgets.checks import lint_python

from .conftest import HDR, FakeLLM, make_settings, text_block, tool_block

VIEW = 'function render({ root, data, sdk }) { root.append(sdk.stats([{ label: "n", value: (data.pods || []).length }])); }'
MANIFEST = {"title": "Bad pods", "sources": [{"id": "pods", "integration": "kubernetes", "op": "pods",
                                             "params": {"problems_only": True}}],
            "uses": ["kubernetes.pod_logs", "kubernetes.rollout_restart"]}


@pytest.fixture
def mock_http():
    calls = []

    def install(handler):
        def wrapped(req):
            calls.append(req)
            return handler(req)
        _common.TEST_TRANSPORT = httpx.MockTransport(wrapped)
        return calls

    yield install
    _common.TEST_TRANSPORT = None


def sse_events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


# ---------------------------------------------------------------- providers
def test_openai_provider_tool_loop(tmp_path, monkeypatch, mock_http):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    replies = [
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "prometheus__alerts", "arguments": json.dumps({"state": "firing"})}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"content": "KubePodCrashLooping is firing."}}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
    ]
    calls = mock_http(lambda req: httpx.Response(200, json=replies.pop(0)))
    c = TestClient(create_app(make_settings(tmp_path, llm_provider="openai")))
    res = c.post("/api/chat", json={"messages": [{"role": "user", "content": "what is firing?"}]}, headers=HDR).json()
    assert res["reply"] == "KubePodCrashLooping is firing." and res["provider"] == "openai"
    assert res["usage"] == {"input_tokens": 30, "output_tokens": 12}
    second = json.loads(calls[1].content)
    assert second["messages"][-1]["role"] == "tool" and "KubePodCrashLooping" in second["messages"][-1]["content"]
    assert any(t["function"]["name"] == "github__create_pull_request" for t in second["tools"])


def test_gemini_replays_raw_parts_and_ollama_tools(tmp_path, monkeypatch, mock_http):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    gem = [
        {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "kubernetes__pods", "args": {"problems_only": True}}, "thoughtSignature": "sig"}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "payments-worker is down"}]}}]},
    ]
    oll = [{"message": {"content": "", "tool_calls": [{"function": {"name": "kubernetes__pods", "arguments": {}}}]}},
           {"message": {"content": "two pods unhealthy"}}]

    def handler(req):
        if "generativelanguage" in req.url.host:
            return httpx.Response(200, json=gem.pop(0))
        return httpx.Response(200, json=oll.pop(0))

    calls = mock_http(handler)
    c = TestClient(create_app(make_settings(tmp_path)))
    msgs = {"messages": [{"role": "user", "content": "status?"}]}
    r = c.post("/api/chat", json=msgs | {"provider": "gemini", "model": "gemini-test"}, headers=HDR).json()
    assert r["reply"] == "payments-worker is down" and r["model"] == "gemini-test"
    body = json.loads(calls[1].content)
    assert body["contents"][1]["parts"][0]["thoughtSignature"] == "sig"  # replayed verbatim
    assert "functionResponse" in body["contents"][2]["parts"][0]
    assert '"default":' not in json.dumps(body["tools"])  # schema keys Gemini rejects are stripped
    r = c.post("/api/chat", json=msgs | {"provider": "ollama"}, headers=HDR).json()
    assert r["reply"] == "two pods unhealthy"
    assert json.loads(calls[3].content)["messages"][-1]["role"] == "tool"


def test_provider_status_and_unknown(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path)))
    st = c.get("/api/llm/providers").json()
    assert {p["name"] for p in st["providers"]} == {"anthropic", "bedrock", "openai", "gemini", "ollama"}
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}], "provider": "openai"}, headers=HDR)
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_stream_and_playbook(tmp_path):
    llm = FakeLLM([([tool_block("t1", "loki__query", {"query": '{namespace="payments"}'})], "tool_use"),
                   ([text_block("Root cause: missing env var.")], "end_turn")])
    c = TestClient(create_app(make_settings(tmp_path), llm_client=llm))
    r = c.post("/api/chat/stream", json={"messages": [{"role": "user", "content": "rca"}], "playbook": "incident-rca"},
               headers=HDR)
    evs = sse_events(r)
    assert [e["type"] for e in evs] == ["start", "tool", "tool_result", "text", "done"]
    assert evs[0]["playbook"] == "incident-rca"
    assert "Active playbook: Incident RCA" in llm.calls[0]["system"]


def test_custom_playbook_overrides(tmp_path):
    (tmp_path / "playbooks").mkdir()
    (tmp_path / "playbooks" / "db.md").write_text("---\nid: db-failover\ntitle: DB failover\norder: 0\n---\nCheck replicas.")
    c = TestClient(create_app(make_settings(tmp_path)))
    pbs = c.get("/api/playbooks").json()
    assert pbs[0]["id"] == "db-failover" and pbs[0]["source"] == "custom"


# ------------------------------------------------------------------ widgets
def test_builtin_widgets_valid_and_render(client):
    ws = client.get("/api/widgets").json()
    assert {"incident-board", "golden-signals", "cluster-health", "cve-hotlist"} <= {w["id"] for w in ws}
    for w in ws:
        assert not w["errors"], (w["id"], w["errors"])
        d = client.get(f"/api/widgets/{w['id']}/data").json()
        assert not d["errors"], (w["id"], d["errors"])
    cve = client.get("/api/widgets/cve-hotlist/data").json()["data"]
    assert cve["rows"][0]["priority"] == "P1" and cve["kev"] >= 1


def test_widget_save_validate_and_hot_reload_event(client):
    bad = client.put("/api/widgets/bad-one", json={"manifest": {"title": "x", "sources": [
        {"id": "a", "integration": "kubernetes", "op": "nope"}]}, "view_js": "fetch('/x')"}, headers=HDR).json()
    assert bad["saved"] and any("unknown operation" in e for e in bad["errors"])
    assert any("network" in e for e in bad["errors"])
    assert client.get("/api/widgets/bad-one/data").status_code == 422

    app = client.app
    res = client.put("/api/widgets/bad-pods", json={"manifest": MANIFEST, "view_js": VIEW}, headers=HDR).json()
    assert res["ok"] and res["widget"]["status"] == "draft"
    ev = app.state.widgets.bus.history[-1]
    assert ev["type"] == "widget_changed" and ev["id"] == "bad-pods" and ev["rev"] == res["widget"]["rev"]
    data = client.get("/api/widgets/bad-pods/data").json()
    assert len(data["data"]["pods"]) >= 2 and data["demo"] == ["kubernetes"]
    # an on-disk edit is picked up by reload() (what the file watcher calls)
    p = app.state.settings.data_dir / "widgets" / "bad-pods" / "view.js"
    p.write_text(VIEW.replace('"n"', '"pods"'))
    w = app.state.widgets.reload("bad-pods")
    assert w["rev"] != res["widget"]["rev"] and app.state.widgets.bus.history[-1]["rev"] == w["rev"]
    # frame is sandboxed with a nonce CSP and no network
    fr = client.get("/w/bad-pods/frame")
    assert "connect-src 'none'" in fr.headers["content-security-policy"] and fr.headers["x-frame-options"] == "SAMEORIGIN"
    assert '"pods"' in fr.text
    # history + rollback
    client.put("/api/widgets/bad-pods", json={"manifest": MANIFEST | {"title": "v3"}, "view_js": VIEW}, headers=HDR)
    hist = client.get("/api/widgets/bad-pods").json()["history"]
    assert len(hist) >= 1
    client.post("/api/widgets/bad-pods/rollback", json={"version": hist[-1]["version"]}, headers=HDR)
    assert client.get("/api/widgets/bad-pods").json()["title"] == "Bad pods"


def test_widget_bridge_enforces_declared_ops(client):
    client.put("/api/widgets/bad-pods", json={"manifest": MANIFEST, "view_js": VIEW}, headers=HDR)
    ok = client.get("/api/widgets/bad-pods/ops/kubernetes/pod_logs", params={"namespace": "payments", "pod": "payments-worker-x"})
    assert ok.status_code == 200 and "PAYMENTS_DB_DSN" in ok.json()["data"]["log"]
    assert client.get("/api/widgets/bad-pods/ops/jira/search").status_code == 403
    prep = client.post("/api/integrations/kubernetes/actions/rollout_restart/prepare", headers=HDR,
                       json={"params": {"namespace": "payments", "deployment": "payments-worker"}, "widget": "bad-pods"})
    assert prep.status_code == 200
    assert client.get("/api/audit").json()[0]["via"] == "widget:bad-pods"
    denied = client.post("/api/integrations/kubernetes/actions/scale/prepare", headers=HDR,
                         json={"params": {"namespace": "a", "deployment": "b", "replicas": 1}, "widget": "bad-pods"})
    assert denied.status_code == 403


def test_python_needs_approval_and_lint(tmp_path):
    s = make_settings(tmp_path, auth_mode="proxy", operators={"op@acme.com"}, admins={"boss@acme.com"})
    c = TestClient(create_app(s))
    op = {"X-Forwarded-Email": "op@acme.com", **HDR}
    boss = {"X-Forwarded-Email": "boss@acme.com", **HDR}
    viewer = {"X-Forwarded-Email": "dev@acme.com", **HDR}
    py = "def transform(sources, inputs, ctx):\n    return {'count': len(sources['pods'] or [])}\n"
    assert c.put("/api/widgets/pyw", json={"manifest": MANIFEST, "view_js": VIEW, "data_py": py},
                 headers=viewer).status_code == 403
    w = c.put("/api/widgets/pyw", json={"manifest": MANIFEST, "view_js": VIEW, "data_py": py}, headers=op).json()["widget"]
    assert w["python"] == "needs_approval"
    d = c.get("/api/widgets/pyw/data", headers=op).json()
    assert "approval" in d["errors"]["transform"] and "pods" in d["data"]  # raw sources until approved
    assert c.post("/api/widgets/pyw/approve", headers=op).status_code == 403
    assert c.post("/api/widgets/pyw/approve", headers=boss).json()["approved"]
    assert c.get("/api/widgets/pyw/data", headers=op).json()["data"]["count"] >= 2
    # any change to data.py revokes the approval
    c.put("/api/widgets/pyw", json={"manifest": MANIFEST, "view_js": VIEW, "data_py": py.replace("count", "n")}, headers=op)
    assert c.get("/api/widgets/pyw", headers=op).json()["python"] == "needs_approval"


def test_python_lint_rejects_escapes():
    assert lint_python("def transform(s, i, ctx):\n    return 1\n") == []
    for src in ["import os\ndef transform(s,i,c): pass", "def transform(s,i,c): return ().__class__",
                "def transform(s,i,c): return open('/etc/passwd').read()", "def transform(s,i,c): return eval('1')",
                "def f(): pass"]:
        assert lint_python(src), src


def test_transform_runtime_restrictions(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path)))
    app = c.app
    app.state.widgets.python_mode = "trusted"  # skip approval to exercise the runtime guard itself
    py = "import math\ndef transform(s, i, ctx):\n    return {'x': ctx.read('jira', 'search')}\n"
    c.put("/api/widgets/sneaky", json={"manifest": MANIFEST, "view_js": VIEW, "data_py": py}, headers=HDR)
    d = c.get("/api/widgets/sneaky/data").json()
    assert "did not declare jira.search" in d["errors"]["transform"]


def test_dashboard_layout(client):
    assert [i["widget"] for i in client.get("/api/dashboard").json()["items"]][0] == "incident-board"
    out = client.put("/api/dashboard", json={"items": [{"widget": "cve-hotlist", "width": "full"}, {"widget": "nope"}]},
                     headers=HDR).json()
    assert out["items"] == [{"widget": "cve-hotlist", "width": "full", "inputs": {}}]


# ----------------------------------------------------------------- builder
def test_ai_builder_samples_saves_and_self_corrects(tmp_path):
    good = {"title": "Firing alerts", "sources": [{"id": "alerts", "integration": "prometheus", "op": "alerts",
                                                   "params": {"state": "firing"}}]}
    llm = FakeLLM([
        ([tool_block("s1", "sample_op", {"integration": "prometheus", "op": "alerts", "params": {}})], "tool_use"),
        ([tool_block("s2", "save_widget", {"manifest": {"title": "Firing alerts", "sources": [
            {"id": "alerts", "integration": "prometheus", "op": "alertz"}]}, "view_js": VIEW})], "tool_use"),
        ([tool_block("s3", "save_widget", {"manifest": good, "view_js": VIEW.replace("pods", "alerts")})], "tool_use"),
        ([text_block("Shows firing alerts.")], "end_turn"),
    ])
    c = TestClient(create_app(make_settings(tmp_path), llm_client=llm))
    r = c.post("/api/widgets/generate/stream", json={"prompt": "show firing prometheus alerts"}, headers=HDR)
    evs = sse_events(r)
    assert evs[-1]["type"] == "done" and evs[-1]["ok"] and evs[-1]["widget"]["id"] == "firing-alerts"
    assert "KubePodCrashLooping" in llm.calls[1]["messages"][-1]["content"][0]["content"]  # sample fed back
    assert "unknown operation" in llm.calls[2]["messages"][-1]["content"][0]["content"]  # error fed back
    assert c.get("/api/widgets/firing-alerts").json()["manifest"]["prompt"] == "show firing prometheus alerts"


def test_chat_can_build_widget(tmp_path):
    llm = FakeLLM([
        ([tool_block("t1", "opsdeck__build_widget", {"description": "bad pods"})], "tool_use"),
        # nested builder conversation
        ([tool_block("b1", "save_widget", {"manifest": MANIFEST, "view_js": VIEW})], "tool_use"),
        ([text_block("Built.")], "end_turn"),
        # back in the chat
        ([text_block("Created widget bad-pods.")], "end_turn"),
    ])
    c = TestClient(create_app(make_settings(tmp_path), llm_client=llm))
    evs = sse_events(c.post("/api/chat/stream", json={"messages": [{"role": "user", "content": "make a widget"}]}, headers=HDR))
    widget = next(e for e in evs if e["type"] == "widget")
    assert widget["id"] == "bad-pods" and widget["ok"]


# ---------------------------------------------------------------- GitHub PR
def test_github_pr_confirm_shows_diff_and_creates_branch(tmp_path, monkeypatch, mock_http):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    import base64

    def handler(req):
        p, m = req.url.path, req.method
        if p == "/repos/acme/svc/contents/app.yaml":
            return httpx.Response(200, json={"content": base64.b64encode(b"replicas: 1\nkey: OLD\n").decode(), "sha": "x"})
        if p == "/repos/acme/svc":
            return httpx.Response(200, json={"default_branch": "main"})
        if p == "/repos/acme/svc/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if p == "/repos/acme/svc/git/commits/base":
            return httpx.Response(200, json={"tree": {"sha": "tree0"}})
        if m == "POST" and p.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob1"})
        if m == "POST" and p.endswith("/git/trees"):
            assert json.loads(req.content)["base_tree"] == "tree0"
            return httpx.Response(201, json={"sha": "tree1"})
        if m == "POST" and p.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c1"})
        if m == "POST" and p.endswith("/git/refs"):
            assert json.loads(req.content)["ref"] == "refs/heads/opsdeck/ops-1-fix-key"
            return httpx.Response(201, json={})
        if m == "POST" and p.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/acme/svc/pull/7"})
        return httpx.Response(404, json={"message": p})

    mock_http(handler)
    s = make_settings(tmp_path, enable_writes=True)
    c = TestClient(create_app(s))
    params = {"repo": "acme/svc", "title": "OPS-1: fix key", "body": "why",
              "changes": [{"path": "app.yaml", "content": "replicas: 1\nkey: NEW\n"}]}
    prep = c.post("/api/integrations/github/actions/create_pull_request/prepare", json={"params": params}, headers=HDR).json()
    assert "-key: OLD" in prep["summary"] and "+key: NEW" in prep["summary"] and prep["mode"] == "live"
    res = c.post("/api/actions/execute", json={"token": prep["token"]}, headers=HDR).json()
    assert res["result"]["pr"] == 7


def test_github_rejects_bad_changes(client):
    r = client.post("/api/integrations/github/actions/create_pull_request/prepare", headers=HDR, json={"params": {
        "repo": "acme/payments-worker", "title": "t", "body": "b", "changes": [{"path": "../etc/passwd", "content": "x"}]}})
    assert r.status_code == 400


def test_jira_accepts_urls(client):
    assert issue_key("https://acme.atlassian.net/browse/OPS-1412") == "OPS-1412"
    r = client.get("/api/integrations/jira/ops/issue", params={"key": "https://acme.atlassian.net/browse/OPS-1412"})
    assert r.json()["data"]["key"] == "OPS-1412"
