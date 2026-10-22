from fastapi.testclient import TestClient

from opsdeck.integrations import ALL_INTEGRATIONS
from opsdeck.main import create_app

from .conftest import HDR, FakeLLM, make_settings, text_block, tool_block


def test_all_integrations_listed_in_demo_mode(client):
    items = client.get("/api/integrations").json()
    assert {i["name"] for i in items} == {c.name for c in ALL_INTEGRATIONS}
    assert all(i["mode"] == "demo" for i in items)


def test_every_widget_section_loads(client):
    for integ in client.get("/api/integrations").json():
        ops = {o["name"]: o for o in integ["ops"]}
        for sec in integ["widget"]:
            assert sec["op"] in ops, f"{integ['name']} widget references unknown op {sec['op']}"
            params = {k: v for k, v in sec["params"].items() if v not in (None, "")}
            r = client.get(f"/api/integrations/{integ['name']}/ops/{sec['op']}", params=params)
            assert r.status_code == 200, (integ["name"], sec["op"], r.text)
            for ra in sec["row_actions"]:
                assert ra["op"] in ops, f"{integ['name']} row action references unknown op {ra['op']}"


def test_every_op_has_demo_twin():
    for cls in ALL_INTEGRATIONS:
        for op in cls.ops():
            assert hasattr(cls, f"demo_{op}"), f"{cls.name}.{op} has no demo_{op}"


def test_overview_reports_attention(client):
    data = client.get("/api/overview").json()
    assert len(data["health"]) == len(ALL_INTEGRATIONS)
    assert data["attention"][0]["severity"] == "critical"


def test_bad_params_are_rejected(client):
    r = client.get("/api/integrations/kubernetes/ops/pods", params={"bogus": 1})
    assert r.status_code == 400
    r = client.get("/api/integrations/aws/ops/alarms", params={"state": "NOPE"})
    assert r.status_code == 400


def test_actions_cannot_be_called_as_reads(client):
    r = client.get("/api/integrations/kubernetes/ops/rollout_restart",
                   params={"namespace": "payments", "deployment": "payments-api"})
    assert r.status_code == 403


def test_post_requires_csrf_header(client):
    r = client.post("/api/integrations/argocd/actions/sync/prepare", json={"params": {"name": "x"}})
    assert r.status_code == 403


def test_action_prepare_execute_and_audit(client):
    prep = client.post("/api/integrations/argocd/actions/sync/prepare",
                       json={"params": {"name": "checkout-prod"}}, headers=HDR).json()
    assert prep["risk"] == "medium" and "checkout-prod" in prep["summary"]
    res = client.post("/api/actions/execute", json={"token": prep["token"]}, headers=HDR)
    assert res.status_code == 200 and res.json()["result"]["sync_started"] == "checkout-prod"
    # single use
    again = client.post("/api/actions/execute", json={"token": prep["token"]}, headers=HDR)
    assert again.status_code == 409
    statuses = [a["status"] for a in client.get("/api/audit").json()]
    assert statuses[:2] == ["executed", "prepared"]


def test_tampered_token_rejected(client):
    prep = client.post("/api/integrations/kubernetes/actions/scale/prepare",
                       json={"params": {"namespace": "a", "deployment": "b", "replicas": 1}}, headers=HDR).json()
    body, sig = prep["token"].split(".")
    r = client.post("/api/actions/execute", json={"token": body + "." + "0" * len(sig)}, headers=HDR)
    assert r.status_code == 403


def test_proxy_auth_roles(tmp_path):
    s = make_settings(tmp_path, auth_mode="proxy", operators={"op@acme.com"}, admins={"boss@acme.com"})
    c = TestClient(create_app(s))
    assert c.get("/api/me").status_code == 401
    viewer = {"X-Forwarded-Email": "dev@acme.com", **HDR}
    assert c.get("/api/me", headers=viewer).json()["role"] == "viewer"
    r = c.post("/api/integrations/argocd/actions/sync/prepare", json={"params": {"name": "x"}}, headers=viewer)
    assert r.status_code == 403
    op = {"X-Forwarded-Email": "op@acme.com", **HDR}
    assert c.post("/api/integrations/argocd/actions/sync/prepare", json={"params": {"name": "x"}},
                  headers=op).status_code == 200
    # high-risk needs admin
    scale = {"params": {"namespace": "a", "deployment": "b", "replicas": 3}}
    assert c.post("/api/integrations/kubernetes/actions/scale/prepare", json=scale, headers=op).status_code == 403
    boss = {"X-Forwarded-Email": "boss@acme.com", **HDR}
    prep = c.post("/api/integrations/kubernetes/actions/scale/prepare", json=scale, headers=boss).json()
    # another user cannot redeem the admin's token
    assert c.post("/api/actions/execute", json={"token": prep["token"]}, headers=op).status_code == 403
    assert c.get("/api/audit", headers=viewer).status_code == 403


def test_live_writes_blocked_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOCD_URL", "https://argocd.invalid")
    monkeypatch.setenv("ARGOCD_TOKEN", "t")
    c = TestClient(create_app(make_settings(tmp_path)))
    argo = next(i for i in c.get("/api/integrations").json() if i["name"] == "argocd")
    assert argo["mode"] == "live"
    r = c.post("/api/integrations/argocd/actions/sync/prepare", json={"params": {"name": "x"}}, headers=HDR)
    assert r.status_code == 403 and "OPSDECK_ENABLE_WRITES" in r.json()["detail"]


def test_demo_off_hides_unconfigured(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path, demo="off")))
    assert c.get("/api/integrations").json() == []


def test_chat_unavailable_without_key(client):
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=HDR)
    assert r.status_code == 503


def test_chat_tool_loop_reads_and_proposes(tmp_path):
    llm = FakeLLM([
        ([text_block("Checking pods."), tool_block("t1", "kubernetes__pods", {"problems_only": True})], "tool_use"),
        ([tool_block("t2", "kubernetes__rollout_restart", {"namespace": "payments", "deployment": "payments-worker"})],
         "tool_use"),
        ([text_block("payments-worker is crashlooping; I proposed a restart.")], "end_turn"),
    ])
    c = TestClient(create_app(make_settings(tmp_path), llm_client=llm))
    res = c.post("/api/chat", json={"messages": [{"role": "user", "content": "what's broken?"}]}, headers=HDR).json()
    assert [t["tool"] for t in res["tools"]] == ["kubernetes__pods", "kubernetes__rollout_restart"]
    assert len(res["proposals"]) == 1 and "payments-worker" in res["proposals"][0]["summary"]
    assert "crashlooping" in res["reply"]
    # The pod data was fed back to the model as a tool result.
    second_call_msgs = llm.calls[1]["messages"]
    assert "CrashLoopBackOff" in second_call_msgs[-1]["content"][0]["content"]
    # The action was only prepared, never executed.
    statuses = [a["status"] for a in c.get("/api/audit").json()]
    assert statuses == ["prepared"]
    # Tool names are valid for the API.
    import re
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"]) for t in llm.calls[0]["tools"])


def test_viewer_gets_no_action_tools(tmp_path):
    llm = FakeLLM([([text_block("ok")], "end_turn")])
    s = make_settings(tmp_path, auth_mode="proxy")
    c = TestClient(create_app(s, llm_client=llm))
    c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]},
           headers={"X-Forwarded-Email": "dev@acme.com", **HDR})
    names = {t["name"] for t in llm.calls[0]["tools"]}
    assert "kubernetes__pods" in names and "kubernetes__rollout_restart" not in names


def test_index_has_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
