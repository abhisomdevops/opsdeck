import copy
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Make sure tests never pick up a developer's real credentials or .env.
os.environ["OPSDECK_ENV_FILE"] = "/nonexistent/.env"
for var in list(os.environ):
    if var.split("_")[0] in {"JIRA", "CONFLUENCE", "JENKINS", "ARGOCD", "ARTIFACTORY", "PRISMA", "K8S", "AWS", "ANTHROPIC"}:
        del os.environ[var]
os.environ.pop("KUBERNETES_SERVICE_HOST", None)

from opsdeck.config import Settings
from opsdeck.main import create_app

HDR = {"X-OpsDeck": "1"}


def make_settings(tmp_path, **overrides) -> Settings:
    s = Settings()
    s.data_dir = tmp_path
    s.secret = "test-secret"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


# ------------------------------------------------------------- fake LLM
def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


class FakeLLM:
    """Replays scripted responses and records the requests it received."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        content, stop = self.script.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop)
