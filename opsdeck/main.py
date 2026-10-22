"""FastAPI application: REST API + static web UI."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .actions import ActionService
from .assistant import Assistant
from .audit import AuditLog
from .auth import User, current_user, require
from .config import Settings, get_settings
from .integrations import ALL_INTEGRATIONS
from .registry import OpError, Registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("opsdeck")
# httpx logs full URLs at INFO (query strings can hold JQL etc.); keep them quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"


class PrepareBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class ExecuteBody(BaseModel):
    token: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    messages: list[ChatMessage]


def create_app(settings: Settings | None = None, integrations: list | None = None,
               llm_client: Any = None) -> FastAPI:
    settings = settings or get_settings()
    registry = Registry(settings, integrations if integrations is not None else ALL_INTEGRATIONS)
    audit = AuditLog(settings.data_dir / "audit.db")
    actions = ActionService(settings, registry, audit)
    assistant = Assistant(settings, registry, actions, client=llm_client)
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="opsdeck")

    app = FastAPI(title="OpsDeck", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.registry = registry
    app.state.audit = audit

    # ---------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        # CSRF defence: state-changing calls must carry a custom header, which a
        # cross-site form or simple request cannot set without a CORS preflight.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            if request.headers.get("x-opsdeck") != "1":
                return JSONResponse({"detail": "missing X-OpsDeck header"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'")
        return response

    @app.exception_handler(OpError)
    async def op_error(_request: Request, exc: OpError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # ------------------------------------------------------------- probes
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        return {"ok": True, "integrations": len(registry.all())}

    # ----------------------------------------------------------------- api
    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {
            "user": user.id, "role": user.role, "env": settings.env_name,
            "auth_mode": settings.auth_mode, "writes_enabled": settings.enable_writes,
            "assistant": assistant.status(), "version": __version__,
        }

    @app.get("/api/integrations")
    def list_integrations(_user: User = Depends(current_user)):
        return [i.to_dict() for i in registry.all()]

    def _health(integ) -> dict:
        start = time.monotonic()
        if integ.mode == "demo":
            return {"name": integ.name, "mode": "demo", "ok": True, "detail": "demo data", "latency_ms": 0}
        try:
            detail = integ.health()
            ok = bool(detail.get("ok", True))
            msg = detail.get("detail", "connected")
        except Exception as exc:  # noqa: BLE001 - surface any failure as unhealthy
            ok, msg = False, str(exc)[:300]
        return {"name": integ.name, "mode": integ.mode, "ok": ok, "detail": msg,
                "latency_ms": int((time.monotonic() - start) * 1000)}

    @app.get("/api/integrations/{name}/health")
    def health(name: str, _user: User = Depends(current_user)):
        return _health(registry.get(name))

    @app.get("/api/integrations/{name}/ops/{op}")
    def read(name: str, op: str, request: Request, _user: User = Depends(current_user)):
        params = dict(request.query_params)
        return {"data": registry.read(name, op, params)}

    @app.post("/api/integrations/{name}/actions/{op}/prepare")
    def prepare(name: str, op: str, body: PrepareBody, user: User = Depends(current_user)):
        return actions.prepare(user, name, op, body.params, via="ui")

    @app.post("/api/actions/execute")
    def execute(body: ExecuteBody, user: User = Depends(current_user)):
        return actions.execute(user, body.token)

    @app.get("/api/overview")
    def overview(_user: User = Depends(current_user)):
        items = registry.all()

        def one(integ):
            h = _health(integ)
            attention: list[dict] = []
            if h["ok"]:
                try:
                    attention = integ.attention()
                except Exception as exc:  # noqa: BLE001
                    attention = [{"severity": "warning", "title": "Could not load status",
                                  "detail": str(exc)[:200]}]
            for a in attention:
                a.setdefault("integration", integ.name)
                a.setdefault("integration_title", integ.title)
            return h, attention

        futures = {pool.submit(one, i): i for i in items}
        health_list, attention_list = [], []
        for fut, integ in futures.items():
            try:
                h, att = fut.result(timeout=20)
            except FutTimeout:
                h, att = {"name": integ.name, "mode": integ.mode, "ok": False,
                          "detail": "timed out", "latency_ms": 20000}, []
            health_list.append(h)
            attention_list.extend(att)
        order = {"critical": 0, "warning": 1, "info": 2}
        attention_list.sort(key=lambda a: order.get(a.get("severity"), 3))
        return {"health": health_list, "attention": attention_list}

    @app.get("/api/audit")
    def audit_log(limit: int = 200, user: User = Depends(current_user)):
        require(user, "operator")
        return audit.recent(min(max(limit, 1), 1000))

    @app.post("/api/chat")
    def chat(body: ChatBody, user: User = Depends(current_user)):
        if not assistant.available:
            raise HTTPException(503, assistant.status()["detail"])
        return assistant.run(user, [m.model_dump() for m in body.messages])

    # ------------------------------------------------------------ static UI
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def run() -> None:
    """Entry point for the ``opsdeck`` console script."""
    import uvicorn

    from .config import env

    host = env("OPSDECK_HOST", "127.0.0.1")
    port = int(env("OPSDECK_PORT", "8080") or 8080)
    reload = env("OPSDECK_RELOAD", "false") in {"1", "true", "yes"}
    uvicorn.run("opsdeck.main:app", host=host, port=port, reload=reload)
