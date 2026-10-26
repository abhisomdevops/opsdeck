"""HolmesGPT (CNCF Sandbox) as a second-opinion investigator.

If you already run the HolmesGPT HTTP server (Helm chart or `holmes serve`),
set HOLMES_URL and the OpsDeck agent can delegate a question to it and use
Holmes' own toolsets and runbooks. Optional: HOLMES_TOKEN, HOLMES_MODEL.
Request/response follow HolmesGPT's /api/chat endpoint; check the field names
against the version you run.
"""

from __future__ import annotations

from ..config import env
from ..registry import Integration, Param, Section, read_op
from ._common import check, clip, http_client


class Holmes(Integration):
    name = "holmes"
    title = "HolmesGPT"
    category = "AI"
    icon = "ai"
    description = "Delegate an investigation to a HolmesGPT server and get its analysis."
    required_env = ("HOLMES_URL",)

    def health(self):
        with http_client(self.settings, env("HOLMES_URL"), token=env("HOLMES_TOKEN")) as c:
            check(c.get("/healthz"))
        return {"ok": True, "detail": "connected"}

    @read_op("Ask HolmesGPT to investigate a question with its own toolsets (slow: up to a few minutes).",
             (Param("question", required=True),))
    def ask(self, question):
        body = {"ask": question}
        if env("HOLMES_MODEL"):
            body["model"] = env("HOLMES_MODEL")
        with http_client(self.settings, env("HOLMES_URL"), token=env("HOLMES_TOKEN"), timeout=600) as c:
            d = check(c.post("/api/chat", json=body)).json()
        calls = [{"tool": t.get("tool_name") or t.get("name"), "description": t.get("description", "")}
                 for t in d.get("tool_calls") or []]
        return {"analysis": clip(d.get("analysis") or d.get("result") or "", 20000), "tool_calls": calls[:40]}

    def widget(self):
        return [Section("Ask HolmesGPT", "ask", inputs=["question"], kind="kv",
                        params={"question": "Why is payments-worker crashlooping?"})]

    def demo_ask(self, question):
        return {"analysis": "(demo) payments-worker exits on start because PAYMENTS_DB_DSN is unset; the 1.42.0 Helm "
                            "values renamed the env var to PAYMENTS_DSN. Restore the old name or update the code.",
                "tool_calls": [{"tool": "kubectl_logs", "description": "previous logs"},
                               {"tool": "kubectl_describe", "description": "deployment env"}]}
