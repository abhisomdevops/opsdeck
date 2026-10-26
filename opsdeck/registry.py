"""Integration framework.

An integration is a Python class that declares:

* read operations  (``@read_op``)  - safe, called freely by widgets and the AI
* actions          (``@action``)   - mutate a system; always go through the
  prepare -> human confirm -> execute flow in ``opsdeck.actions``
* a widget layout  (``widget()``)  - declarative sections the browser renders
* optional ``attention()`` items shown on the Overview page

Every op can have a ``demo_<op>`` twin that returns canned data, so the whole
console runs on a laptop with zero credentials.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Settings

log = logging.getLogger("opsdeck.registry")


class OpError(Exception):
    """Raised for user-facing errors (bad params, upstream failure)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- op metadata


@dataclass
class Param:
    name: str
    type: str = "string"  # string | integer | number | boolean | array | object
    description: str = ""
    required: bool = False
    default: Any = None
    enum: list[str] | None = None
    items: dict | None = None  # JSON schema of array items (type="array")

    def json_schema(self) -> dict:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        if self.type == "array":
            schema["items"] = self.items or {"type": "string"}
        return schema

    def coerce(self, value: Any) -> Any:
        if value is None or value == "":
            if self.required:
                raise OpError(f"missing required parameter '{self.name}'")
            return self.default
        try:
            if self.type in ("array", "object"):
                if isinstance(value, str):
                    value = json.loads(value)
                want = list if self.type == "array" else dict
                if not isinstance(value, want):
                    raise TypeError
                return value
            if self.type == "integer":
                value = int(value)
            elif self.type == "number":
                value = float(value)
            elif self.type == "boolean":
                value = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            else:
                value = str(value)
        except (TypeError, ValueError):  # json.JSONDecodeError is a ValueError
            raise OpError(f"parameter '{self.name}' must be {self.type}") from None
        if self.enum and value not in self.enum:
            raise OpError(f"parameter '{self.name}' must be one of {self.enum}")
        return value


@dataclass
class OpSpec:
    name: str
    kind: str  # read | action
    description: str
    params: list[Param]
    risk: str = "none"  # none | low | medium | high
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "title": self.title or self.name.replace("_", " ").title(),
            "description": self.description,
            "risk": self.risk,
            "params": [asdict(p) for p in self.params],
        }


def read_op(description: str, params: tuple[Param, ...] = (), title: str = ""):
    def deco(fn: Callable) -> Callable:
        fn._opspec = OpSpec(fn.__name__, "read", description, list(params), "none", title)
        return fn

    return deco


def action(description: str, params: tuple[Param, ...] = (), risk: str = "medium", title: str = ""):
    if risk not in {"low", "medium", "high"}:
        raise ValueError("risk must be low, medium or high")

    def deco(fn: Callable) -> Callable:
        fn._opspec = OpSpec(fn.__name__, "action", description, list(params), risk, title)
        return fn

    return deco


# ------------------------------------------------------------- widget layout


@dataclass
class Column:
    key: str
    label: str = ""
    fmt: str = "text"  # text | status | time | link | code | num | bytes | duration


@dataclass
class RowAction:
    op: str
    label: str
    map: dict[str, str] = field(default_factory=dict)  # op param -> row key
    inputs: list[str] = field(default_factory=list)  # extra params prompted from user
    params: dict[str, Any] = field(default_factory=dict)  # fixed params


@dataclass
class Section:
    title: str
    op: str
    columns: list[Column] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)  # op params exposed as filters
    params: dict[str, Any] = field(default_factory=dict)  # fixed/default params
    row_actions: list[RowAction] = field(default_factory=list)
    kind: str = "table"  # table | kv
    width: str = "full"  # full | half
    refresh_secs: int = 0


# ---------------------------------------------------------------- base class


class Integration:
    name: str = ""
    title: str = ""
    category: str = "Other"
    icon: str = "box"
    description: str = ""
    required_env: tuple[str, ...] = ()

    def __init__(self, settings: Settings):
        self.settings = settings

    # --- configuration -------------------------------------------------
    def is_configured(self) -> bool:
        from .config import env

        return bool(self.required_env) and all(env(v) for v in self.required_env)

    @property
    def mode(self) -> str:
        """live | demo | disabled"""
        if self.settings.demo == "on":
            return "demo"
        if self.is_configured():
            return "live"
        return "demo" if self.settings.demo == "auto" else "disabled"

    # --- overridable hooks ---------------------------------------------
    def health(self) -> dict:
        """Cheap connectivity check for live mode."""
        return {"ok": True}

    def widget(self) -> list[Section]:
        return []

    def attention(self) -> list[dict]:
        """Items for the Overview page: {severity, title, detail}."""
        return []

    def describe_action(self, op: str, params: dict) -> str:
        """Human-readable summary shown in the confirmation dialog."""
        args = ", ".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
        return f"{self.title}: {op.replace('_', ' ')} ({args})"

    # --- introspection ---------------------------------------------------
    @classmethod
    def ops(cls) -> dict[str, OpSpec]:
        found: dict[str, OpSpec] = {}
        for _, member in inspect.getmembers(cls, predicate=inspect.isfunction):
            spec = getattr(member, "_opspec", None)
            if spec is not None:
                found[spec.name] = spec
        return found

    def spec(self, op: str) -> OpSpec:
        spec = self.ops().get(op)
        if spec is None:
            raise OpError(f"{self.name} has no operation '{op}'", 404)
        return spec

    def coerce_params(self, op: str, raw: dict) -> dict:
        spec = self.spec(op)
        known = {p.name for p in spec.params}
        unknown = set(raw) - known
        if unknown:
            raise OpError(f"unknown parameter(s) for {op}: {sorted(unknown)}")
        return {p.name: p.coerce(raw.get(p.name)) for p in spec.params}

    def invoke(self, op: str, params: dict) -> Any:
        """Run an op (read or action) with already-coerced params.

        Callers must go through ``Registry.read`` / ``opsdeck.actions`` so that
        authorization and confirmation are enforced.
        """
        if self.mode == "disabled":
            raise OpError(f"{self.title} is not configured", 409)
        if self.mode == "demo":
            fn = getattr(self, f"demo_{op}", None)
            if fn is None:
                raise OpError(f"{self.title} demo mode has no data for '{op}'", 501)
        else:
            fn = getattr(self, op)
        return fn(**params)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "icon": self.icon,
            "description": self.description,
            "mode": self.mode,
            "required_env": list(self.required_env),
            "ops": [s.to_dict() for s in self.ops().values()],
            "widget": [asdict(s) for s in self.widget()],
        }


# ------------------------------------------------------------------ registry


class Registry:
    def __init__(self, settings: Settings, classes: list[type[Integration]]):
        self.settings = settings
        self._items: dict[str, Integration] = {}
        for cls in classes:
            inst = cls(settings)
            if inst.mode != "disabled":
                self._items[inst.name] = inst

    def all(self) -> list[Integration]:
        return list(self._items.values())

    def catalog(self, kinds: tuple[str, ...] = ("read",)) -> list[dict]:
        """Flat list of ops (``integration.op``) with params, for the AI and widget builder."""
        out = []
        for integ in self.all():
            for spec in integ.ops().values():
                if spec.kind in kinds:
                    out.append({"id": f"{integ.name}.{spec.name}", "integration": integ.name, "op": spec.name,
                                "kind": spec.kind, "risk": spec.risk, "mode": integ.mode,
                                "description": spec.description,
                                "params": {p.name: p.json_schema() | {"required": p.required} for p in spec.params}})
        return out

    def get(self, name: str) -> Integration:
        item = self._items.get(name)
        if item is None:
            raise OpError(f"unknown or disabled integration '{name}'", 404)
        return item

    def read(self, name: str, op: str, raw_params: dict) -> Any:
        integ = self.get(name)
        spec = integ.spec(op)
        if spec.kind != "read":
            raise OpError(f"'{op}' is an action; use the confirmation flow", 403)
        params = integ.coerce_params(op, raw_params)
        try:
            return integ.invoke(op, params)
        except OpError:
            raise
        except Exception as exc:  # upstream failure -> 502 with a readable message
            log.exception("read %s.%s failed", name, op)
            raise OpError(f"{integ.title} {op} failed: {_short_error(exc)}", 502) from exc


def _short_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 400 else text[:400] + "..."
