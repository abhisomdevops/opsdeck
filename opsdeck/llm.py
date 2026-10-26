"""LLM providers behind one small interface.

The agent loop works with a neutral conversation format so the same
investigation can run on Claude, OpenAI (or any OpenAI-compatible server such
as vLLM, LiteLLM, LM Studio, OpenRouter, Azure OpenAI), Gemini or Ollama:

    {"role": "user", "content": "text"}
    {"role": "assistant", "text": "...", "tool_calls": [ToolCall...], "raw": {...}}
    {"role": "tool", "results": [{"id", "name", "content", "is_error"}]}

``raw`` keeps the provider's own assistant message so it can be replayed
verbatim on the next turn (Gemini needs its thought signatures back, for
example). It is ignored by other providers.

Only Anthropic uses an SDK; the others are plain HTTPS calls via httpx so no
extra dependencies are needed and corporate CA bundles work everywhere.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings, env
from .registry import OpError

log = logging.getLogger("opsdeck.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    usage: dict = field(default_factory=dict)  # {"input_tokens", "output_tokens"}


class Provider:
    name = ""
    title = ""

    def __init__(self, settings: Settings, model: str | None = None):
        self.settings = settings
        self.model = model or self.default_model()

    # ------------------------------------------------------------------ api
    def default_model(self) -> str:
        return ""

    def configured(self) -> bool:
        return False

    def missing(self) -> str:
        return ""

    def list_models(self) -> list[str]:
        return [self.model] if self.model else []

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> LLMResponse:
        raise NotImplementedError

    def with_model(self, model: str | None) -> Provider:
        if not model or model == self.model:
            return self
        clone = type(self).__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.model = model
        return clone

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "model": self.model,
                "configured": self.configured(), "detail": "ready" if self.configured() else self.missing()}

    # --------------------------------------------------------------- helper
    def _http(self, base_url: str, headers: dict | None = None, timeout: float = 180) -> httpx.Client:
        from .integrations import _common

        return httpx.Client(base_url=base_url.rstrip("/"), headers=headers or {}, timeout=timeout,
                            verify=self.settings.verify, transport=_common.TEST_TRANSPORT)


def _post_json(client: httpx.Client, path: str, body: dict, params: dict | None = None) -> dict:
    resp = client.post(path, json=body, params=params)
    if resp.status_code >= 400:
        raise OpError(f"LLM HTTP {resp.status_code}: {resp.text[:400]}", 502)
    return resp.json()


# ================================================================ Anthropic
class AnthropicProvider(Provider):
    """Claude via the Anthropic API, or Claude on AWS Bedrock (``bedrock=True``)."""

    def __init__(self, settings: Settings, model: str | None = None, *, bedrock: bool = False, client: Any = None):
        self.bedrock = bedrock
        self.name = "bedrock" if bedrock else "anthropic"
        self.title = "Claude on AWS Bedrock" if bedrock else "Anthropic Claude"
        self._client = client
        super().__init__(settings, model)

    def default_model(self) -> str:
        return (self.settings.bedrock_model if self.bedrock else self.settings.anthropic_model) or ""

    def configured(self) -> bool:
        if self._client is not None:
            return True
        return bool(self.settings.bedrock_model) if self.bedrock else bool(self.settings.anthropic_api_key)

    def missing(self) -> str:
        return "set BEDROCK_MODEL_ID" if self.bedrock else "set ANTHROPIC_API_KEY"

    def client(self):
        if self._client is None:
            import anthropic

            if self.bedrock:
                self._client = anthropic.AnthropicBedrock(aws_region=self.settings.bedrock_region)
            else:
                kw = {"api_key": self.settings.anthropic_api_key}
                if env("ANTHROPIC_BASE_URL"):
                    kw["base_url"] = env("ANTHROPIC_BASE_URL")
                self._client = anthropic.Anthropic(**kw)
        return self._client

    def list_models(self) -> list[str]:
        if self.bedrock or not self.configured():
            return super().list_models()
        try:
            models = [m.id for m in self.client().models.list(limit=50).data]
            return models or super().list_models()
        except Exception:  # noqa: BLE001 - listing is best effort
            return super().list_models()

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no output)"}]})
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     "is_error": r.get("is_error", False)} for r in m["results"]]})
        return out

    def chat(self, system, messages, tools, max_tokens):
        kw: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "system": system,
                              "messages": self._messages(messages)}
        if tools:
            kw["tools"] = tools
        resp = self.client().messages.create(**kw)
        out = LLMResponse()
        texts = []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                out.tool_calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        out.text = "\n\n".join(t for t in texts if t)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            out.usage = {"input_tokens": getattr(usage, "input_tokens", 0) or 0,
                         "output_tokens": getattr(usage, "output_tokens", 0) or 0}
        return out


# =================================================== OpenAI and compatibles
class OpenAIProvider(Provider):
    """OpenAI Chat Completions, Azure OpenAI, or any OpenAI-compatible server.

    OPENAI_BASE_URL points at vLLM / LiteLLM / LM Studio / OpenRouter etc.
    Azure: set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and OPENAI_MODEL to
    your *deployment* name.
    """

    name, title = "openai", "OpenAI (or compatible)"

    def default_model(self) -> str:
        return env("OPENAI_MODEL", "gpt-5") or ""

    @property
    def azure(self) -> bool:
        return bool(env("AZURE_OPENAI_ENDPOINT"))

    def configured(self) -> bool:
        if self.azure:
            return bool(env("AZURE_OPENAI_API_KEY"))
        return bool(env("OPENAI_API_KEY") or env("OPENAI_BASE_URL"))

    def missing(self) -> str:
        return "set OPENAI_API_KEY (or OPENAI_BASE_URL for a compatible server)"

    def _client(self) -> httpx.Client:
        if self.azure:
            base = f"{env('AZURE_OPENAI_ENDPOINT').rstrip('/')}/openai/deployments/{self.model}"
            return self._http(base, {"api-key": env("AZURE_OPENAI_API_KEY")})
        headers = {"Authorization": f"Bearer {env('OPENAI_API_KEY')}"} if env("OPENAI_API_KEY") else {}
        return self._http(env("OPENAI_BASE_URL", "https://api.openai.com/v1"), headers)

    def list_models(self) -> list[str]:
        if self.azure or not self.configured():
            return super().list_models()
        try:
            with self._client() as c:
                data = c.get("/models").json().get("data", [])
            ids = sorted(m["id"] for m in data)
            return ids or super().list_models()
        except Exception:  # noqa: BLE001
            return super().list_models()

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                          "function": {"name": tc.name, "arguments": json.dumps(tc.input)}}
                                         for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return out

    def chat(self, system, messages, tools, max_tokens):
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(system, messages),
                                "max_completion_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        params = {"api-version": env("AZURE_OPENAI_API_VERSION", "2024-10-21")} if self.azure else None
        with self._client() as c:
            data = _post_json(c, "/chat/completions", body, params)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        out = LLMResponse(text=msg.get("content") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            out.tool_calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex[:12], fn.get("name", ""), args))
        u = data.get("usage") or {}
        out.usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        return out


# ==================================================================== Gemini
_GEMINI_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required", "nullable", "format"}


def _gemini_schema(schema: dict) -> dict:
    out = {k: v for k, v in schema.items() if k in _GEMINI_SCHEMA_KEYS}
    if "properties" in out:
        out["properties"] = {k: _gemini_schema(v) for k, v in out["properties"].items()}
        if not out["properties"]:
            out.pop("properties")
            out.pop("required", None)
    if "items" in out:
        out["items"] = _gemini_schema(out["items"])
    if not out.get("required"):
        out.pop("required", None)
    return out


class GeminiProvider(Provider):
    name, title = "gemini", "Google Gemini"

    def default_model(self) -> str:
        return env("GEMINI_MODEL", "gemini-2.5-pro") or ""

    def configured(self) -> bool:
        return bool(env("GEMINI_API_KEY") or env("GOOGLE_API_KEY"))

    def missing(self) -> str:
        return "set GEMINI_API_KEY"

    def _client(self) -> httpx.Client:
        key = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
        return self._http(env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
                          {"x-goog-api-key": key})

    def list_models(self) -> list[str]:
        if not self.configured():
            return super().list_models()
        try:
            with self._client() as c:
                data = c.get("/models", params={"pageSize": 100}).json().get("models", [])
            ids = sorted(m["name"].removeprefix("models/") for m in data
                         if "generateContent" in m.get("supportedGenerationMethods", []))
            return ids or super().list_models()
        except Exception:  # noqa: BLE001
            return super().list_models()

    @staticmethod
    def _contents(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                raw = m.get("raw")
                if isinstance(raw, dict) and raw.get("provider") == "gemini":
                    out.append(raw["content"])  # replay verbatim (keeps thought signatures)
                    continue
                parts: list[dict] = [{"text": m["text"]}] if m.get("text") else []
                parts += [{"functionCall": {"name": tc.name, "args": tc.input}} for tc in m.get("tool_calls", [])]
                out.append({"role": "model", "parts": parts or [{"text": "(no output)"}]})
            elif m["role"] == "tool":
                out.append({"role": "user", "parts": [
                    {"functionResponse": {"name": r["name"], "response": {
                        "error" if r.get("is_error") else "result": r["content"]}}} for r in m["results"]]})
        return out

    def chat(self, system, messages, tools, max_tokens):
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"], "parameters": _gemini_schema(t["input_schema"])}
                for t in tools]}]
        with self._client() as c:
            data = _post_json(c, f"/models/{self.model}:generateContent", body)
        cand = (data.get("candidates") or [{}])[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        out = LLMResponse(raw={"provider": "gemini", "content": content})
        texts = []
        for part in content.get("parts", []):
            if "text" in part and not part.get("thought"):
                texts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                out.tool_calls.append(ToolCall(uuid.uuid4().hex[:12], fc.get("name", ""), dict(fc.get("args") or {})))
        out.text = "".join(texts)
        u = data.get("usageMetadata") or {}
        out.usage = {"input_tokens": u.get("promptTokenCount", 0), "output_tokens": u.get("candidatesTokenCount", 0)}
        return out


# ==================================================================== Ollama
class OllamaProvider(Provider):
    name, title = "ollama", "Ollama (local models)"

    def default_model(self) -> str:
        return env("OLLAMA_MODEL", "qwen3") or ""

    def configured(self) -> bool:
        return bool(env("OLLAMA_URL"))

    def missing(self) -> str:
        return "set OLLAMA_URL (e.g. http://localhost:11434)"

    def _client(self) -> httpx.Client:
        return self._http(env("OLLAMA_URL"), timeout=600)

    def list_models(self) -> list[str]:
        if not self.configured():
            return super().list_models()
        try:
            with self._client() as c:
                tags = c.get("/api/tags").json().get("models", [])
            return sorted(t["name"] for t in tags) or super().list_models()
        except Exception:  # noqa: BLE001
            return super().list_models()

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"function": {"name": tc.name, "arguments": tc.input}} for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "content": r["content"], "tool_name": r["name"]})
        return out

    def chat(self, system, messages, tools, max_tokens):
        body: dict[str, Any] = {"model": self.model, "stream": False, "messages": self._messages(system, messages),
                                "options": {"num_predict": max_tokens}}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        with self._client() as c:
            data = _post_json(c, "/api/chat", body)
        msg = data.get("message") or {}
        out = LLMResponse(text=msg.get("content") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            out.tool_calls.append(ToolCall(uuid.uuid4().hex[:12], fn.get("name", ""), args))
        out.usage = {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)}
        return out


# ================================================================== registry
class Providers:
    """All known providers; ``get(name, model)`` returns a ready provider."""

    def __init__(self, settings: Settings, anthropic_client: Any = None):
        self.settings = settings
        self._items: dict[str, Provider] = {
            "anthropic": AnthropicProvider(settings, client=anthropic_client),
            "bedrock": AnthropicProvider(settings, bedrock=True),
            "openai": OpenAIProvider(settings),
            "gemini": GeminiProvider(settings),
            "ollama": OllamaProvider(settings),
        }
        default = settings.llm_provider
        if anthropic_client is not None:
            default = "anthropic"
        self.default = default

    def available(self) -> list[Provider]:
        return [p for p in self._items.values() if p.configured()]

    def default_provider(self) -> Provider | None:
        if self.default == "none":
            return None
        p = self._items.get(self.default)
        if p and p.configured():
            return p
        avail = self.available()
        return avail[0] if avail else None

    def get(self, name: str | None = None, model: str | None = None) -> Provider:
        if self.default == "none":
            raise OpError("AI assistant disabled (OPSDECK_LLM_PROVIDER=none)", 503)
        p = self._items.get(name) if name else self.default_provider()
        if p is None:
            raise OpError(f"unknown LLM provider '{name}'" if name else "no LLM provider configured", 503)
        if not p.configured():
            raise OpError(f"{p.title} is not configured: {p.missing()}", 503)
        return p.with_model(model)

    def status(self) -> dict:
        d = self.default_provider()
        return {
            "available": d is not None,
            "provider": d.name if d else None,
            "model": d.model if d else None,
            "detail": "ready" if d else ("AI assistant disabled (OPSDECK_LLM_PROVIDER=none)" if self.default == "none"
                                         else "configure an LLM provider (ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                                              "GEMINI_API_KEY, OLLAMA_URL or Bedrock)"),
            "providers": [p.to_dict() for p in self._items.values()],
        }
