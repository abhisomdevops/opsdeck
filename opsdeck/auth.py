"""Identity and roles.

local mode  - one person on their own machine; they are admin.
proxy mode  - deployed behind oauth2-proxy (or ALB OIDC). The proxy
              authenticates the user and passes their email in a header.
              The app must only be reachable through the proxy (see the Helm
              chart's NetworkPolicy), otherwise anyone could forge the header.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .config import Settings, get_settings

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

IDENTITY_HEADERS = (
    "x-auth-request-email",  # oauth2-proxy (nginx auth_request)
    "x-forwarded-email",  # oauth2-proxy (reverse proxy mode)
    "x-amzn-oidc-identity",  # AWS ALB OIDC
)


@dataclass(frozen=True)
class User:
    id: str
    role: str

    def can(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def role_for(s: Settings, email: str) -> str:
    email = email.lower()
    if email in s.admins or "*" in s.admins:
        return "admin"
    if email in s.operators or "*" in s.operators:
        return "operator"
    return s.default_role if s.default_role in ROLE_RANK else "viewer"


def current_user(request: Request) -> User:
    s: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if s.auth_mode == "local":
        return User(id=getpass.getuser(), role="admin")
    for header in IDENTITY_HEADERS:
        value = request.headers.get(header)
        if value:
            return User(id=value.strip(), role=role_for(s, value.strip()))
    raise HTTPException(401, "not authenticated (no identity header from proxy)")


def require(user: User, role: str) -> None:
    if not user.can(role):
        raise HTTPException(403, f"requires role '{role}' (you are '{user.role}')")
