"""Runtime configuration.

Everything is driven by environment variables (optionally loaded from a .env
file in the working directory). Core settings live on ``Settings``; each
integration reads its own variables through ``env()`` so adding an
integration never requires touching this file.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.environ.get("OPSDECK_ENV_FILE", ".env"), override=False)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    value = env(name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    def __init__(self) -> None:
        # local: single user on a laptop, no login, user gets admin.
        # proxy: behind oauth2-proxy / ALB OIDC; identity comes from headers.
        self.auth_mode = (env("OPSDECK_AUTH_MODE", "local") or "local").lower()
        self.admins = {e.lower() for e in env_list("OPSDECK_ADMINS")}
        self.operators = {e.lower() for e in env_list("OPSDECK_OPERATORS")}
        self.default_role = (env("OPSDECK_DEFAULT_ROLE", "viewer") or "viewer").lower()

        # Live (non-demo) write actions are off unless explicitly enabled.
        self.enable_writes = env_bool("OPSDECK_ENABLE_WRITES", False)
        # auto: integrations without credentials run on demo data.
        # off: unconfigured integrations are hidden. on: everything is demo.
        self.demo = (env("OPSDECK_DEMO", "auto") or "auto").lower()

        self.env_name = env("OPSDECK_ENV_NAME", "local")
        self.secret = env("OPSDECK_SECRET") or secrets.token_hex(32)
        self.data_dir = Path(env("OPSDECK_DATA_DIR", "./data") or "./data")
        self.ca_bundle = env("OPSDECK_CA_BUNDLE")  # corporate proxy / private CA
        self.http_timeout = float(env("OPSDECK_HTTP_TIMEOUT", "20") or 20)
        self.action_ttl_seconds = int(env("OPSDECK_ACTION_TTL", "300") or 300)

        # AI assistant
        self.llm_provider = (env("OPSDECK_LLM_PROVIDER", "anthropic") or "anthropic").lower()
        self.anthropic_api_key = env("ANTHROPIC_API_KEY")
        self.anthropic_model = env("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.bedrock_region = env("BEDROCK_REGION") or env("AWS_REGION")
        self.bedrock_model = env("BEDROCK_MODEL_ID")
        self.llm_max_tokens = int(env("OPSDECK_LLM_MAX_TOKENS", "4096") or 4096)
        self.llm_max_steps = int(env("OPSDECK_LLM_MAX_STEPS", "8") or 8)

    @property
    def verify(self):
        return self.ca_bundle or True


@lru_cache
def get_settings() -> Settings:
    return Settings()
