"""Two-step action flow: prepare -> human confirm -> execute.

``prepare`` validates the request and returns a signed, single-use,
short-lived token bound to the user, the exact params and the action. The UI
shows the summary and the user must click Confirm, which calls ``execute``.
The AI assistant can only ever call ``prepare``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .audit import AuditLog
from .auth import User
from .config import Settings
from .registry import OpError, Registry, _short_error

RISK_ROLE = {"low": "operator", "medium": "operator", "high": "admin"}


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _encode(settings: Settings, data: dict) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_sign(settings.secret, raw)}"


def _decode(settings: Settings, token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        raise OpError("malformed confirmation token", 400) from None
    if not hmac.compare_digest(sig, _sign(settings.secret, raw)):
        raise OpError("invalid confirmation token", 403)
    return json.loads(raw)


class ActionService:
    def __init__(self, settings: Settings, registry: Registry, audit: AuditLog):
        self.settings = settings
        self.registry = registry
        self.audit = audit

    def prepare(self, user: User, integration: str, op: str, raw_params: dict, via: str = "ui") -> dict:
        integ = self.registry.get(integration)
        spec = integ.spec(op)
        if spec.kind != "action":
            raise OpError(f"'{op}' is not an action", 400)
        needed = RISK_ROLE[spec.risk]
        if not user.can(needed):
            raise OpError(f"{spec.risk}-risk actions require role '{needed}' (you are '{user.role}')", 403)
        if integ.mode == "live" and not self.settings.enable_writes:
            raise OpError("live write actions are disabled (set OPSDECK_ENABLE_WRITES=true)", 403)
        params = integ.coerce_params(op, raw_params)
        summary = integ.describe_action(op, params)
        expires = int(time.time()) + self.settings.action_ttl_seconds
        token = _encode(self.settings, {
            "jti": secrets.token_hex(12), "user": user.id, "integration": integration,
            "op": op, "params": params, "exp": expires, "via": via,
        })
        self.audit.record(user=user.id, role=user.role, integration=integration, op=op,
                          mode=integ.mode, status="prepared", params=params, via=via)
        return {
            "token": token, "summary": summary, "risk": spec.risk, "mode": integ.mode,
            "integration": integration, "op": op, "params": params, "expires_at": expires,
        }

    def execute(self, user: User, token: str) -> dict:
        data = _decode(self.settings, token)
        if data["user"] != user.id:
            raise OpError("this confirmation belongs to another user", 403)
        if time.time() > data["exp"]:
            raise OpError("confirmation expired; prepare the action again", 410)
        integ = self.registry.get(data["integration"])
        spec = integ.spec(data["op"])
        # Re-check authorization at execution time (role/flags may have changed).
        if not user.can(RISK_ROLE[spec.risk]):
            raise OpError("not authorized", 403)
        if integ.mode == "live" and not self.settings.enable_writes:
            raise OpError("live write actions are disabled", 403)
        if not self.audit.consume_token(data["jti"]):
            raise OpError("this confirmation was already used", 409)

        base = dict(user=user.id, role=user.role, integration=data["integration"], op=data["op"],
                    mode=integ.mode, params=data["params"], via=data.get("via", "ui"))
        try:
            result: Any = integ.invoke(data["op"], data["params"])
        except Exception as exc:
            self.audit.record(status="failed", detail=_short_error(exc), **base)
            if isinstance(exc, OpError):
                raise
            raise OpError(f"action failed: {_short_error(exc)}", 502) from exc
        self.audit.record(status="executed", detail=json.dumps(result, default=str)[:2000], **base)
        return {"ok": True, "mode": integ.mode, "result": result}
