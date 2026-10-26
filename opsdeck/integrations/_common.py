"""Shared helpers for HTTP-based integrations."""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

import httpx

from ..config import Settings, env
from ..registry import OpError

# Tests set this to an httpx.MockTransport to exercise live code paths offline.
TEST_TRANSPORT: httpx.BaseTransport | None = None


def http_client(settings: Settings, base_url: str, *, token: str | None = None,
                user: str | None = None, headers: dict | None = None,
                timeout: float | None = None) -> httpx.Client:
    """Basic auth when a user is given, bearer token otherwise."""
    hdrs = {"Accept": "application/json", "User-Agent": "opsdeck/0.1"}
    hdrs.update(headers or {})
    auth = None
    if user and token:
        auth = httpx.BasicAuth(user, token)
    elif token:
        hdrs["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=base_url.rstrip("/"), headers=hdrs, auth=auth,
                        verify=settings.verify, timeout=timeout or settings.http_timeout,
                        follow_redirects=True, transport=TEST_TRANSPORT)


def check(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        body = resp.text[:300].replace("\n", " ")
        raise OpError(f"HTTP {resp.status_code} from {resp.request.url.host}: {body}", 502)
    return resp


def iso(ts: Any) -> str | None:
    """Normalize epoch seconds/millis or ISO strings to an ISO-8601 UTC string."""
    if ts in (None, "", 0):
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e11:  # millis
            ts = ts / 1000
        return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()
    if isinstance(ts, dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        return ts.isoformat()
    return str(ts)


def ago(**kw) -> str:
    """ISO timestamp for 'now minus kw' — used by demo data."""
    return (dt.datetime.now(dt.UTC) - dt.timedelta(**kw)).isoformat()


_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str, limit: int = 20000) -> str:
    text = re.sub(r"<(br|/p|/li|/h\d|/tr)[^>]*>", "\n", markup or "", flags=re.IGNORECASE)
    text = html.unescape(_TAG.sub("", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n... (truncated, {len(text)} chars total)"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def tail(text: str, lines: int) -> str:
    parts = (text or "").splitlines()
    return "\n".join(parts[-lines:])


__all__ = ["ago", "check", "clip", "env", "html_to_text", "http_client", "iso", "now_utc", "tail"]
