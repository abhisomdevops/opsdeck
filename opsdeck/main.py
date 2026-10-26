"""FastAPI application: REST API + static web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .actions import ActionService
from .assistant import Assistant, ExtraTool
from .audit import AuditLog
from .auth import User, current_user, require
from .config import Settings, get_settings
from .integrations import ALL_INTEGRATIONS
from .llm import Providers
from .playbooks import Playbooks
from .registry import OpError, Registry
from .widgets import WidgetService
from .widgets.builder import WidgetBuilder
from .widgets.frame import frame_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("opsdeck")
# httpx logs full URLs at INFO (query strings can hold JQL etc.); keep them quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"


class PrepareBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    widget: str | None = None  # set when a widget proposes the action


class ExecuteBody(BaseModel):
    token: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    messages: list[ChatMessage]
    provider: str | None = None
    model: str | None = None
    playbook: str | None = None


class WidgetBody(BaseModel):
    manifest: dict[str, Any]
    view_js: str = ""
    data_py: str | None = None


class StatusBody(BaseModel):
    status: str


class RollbackBody(BaseModel):
    version: str


class GenerateBody(BaseModel):
    prompt: str
    widget_id: str | None = None
    provider: str | None = None
    model: str | None = None


class DashboardBody(BaseModel):
    items: list[dict[str, Any]]


def sse(events: Iterator[dict]) -> Iterator[str]:
    """Server-sent events from a sync generator; errors become an error event."""
    try:
        for ev in events:
            yield f"data: {json.dumps(ev, default=str)}\n\n"
    except OpError as exc:
        yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
    except Exception as exc:  # noqa: BLE001
        log.exception("stream failed")
        yield f"data: {json.dumps({'type': 'error', 'detail': f'internal error: {exc}'})}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def create_app(settings: Settings | None = None, integrations: list | None = None,
               llm_client: Any = None) -> FastAPI:
    settings = settings or get_settings()
    registry = Registry(settings, integrations if integrations is not None else ALL_INTEGRATIONS)
    audit = AuditLog(settings.data_dir / "audit.db")
    actions = ActionService(settings, registry, audit)
    providers = Providers(settings, anthropic_client=llm_client)
    playbooks = Playbooks(settings.data_dir)
    assistant = Assistant(settings, registry, actions, providers=providers, playbooks=playbooks)
    widgets = WidgetService(settings, registry, audit, all_classes=ALL_INTEGRATIONS)
    builder = WidgetBuilder(settings, widgets, providers)
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="opsdeck")

    def _build_widget_tool(user: User, args: dict):
        res = builder.build(user, args.get("description", ""), widget_id=args.get("widget_id") or None)
        w = res["widget"]
        if not w:
            return {"ok": False, "errors": res["errors"] or ["the builder did not save a widget"], "notes": res["notes"]}
        return {"_event": "widget", "ok": res["ok"], "id": w["id"], "title": w["title"], "status": w["status"],
                "python": w["python"], "errors": w["errors"], "notes": res["notes"][-1:] if res["notes"] else []}

    assistant.register_tool(ExtraTool(
        name="opsdeck__build_widget",
        description=("Build a new console widget, or change an existing one, from a plain-English description "
                     "(data to show, filters, visual, refresh, row actions). Saves it as a draft and returns its id."),
        schema={"type": "object", "properties": {
            "description": {"type": "string", "description": "Precise description of the widget or the change"},
            "widget_id": {"type": "string", "description": "Existing widget id to modify (omit to create)"}},
            "required": ["description"]},
        fn=_build_widget_tool, min_role="operator"))
    assistant.register_tool(ExtraTool(
        name="opsdeck__list_widgets", description="List the console's widgets (id, title, description, status).",
        schema={"type": "object", "properties": {}},
        fn=lambda _u, _a: [{k: w[k] for k in ("id", "title", "description", "status", "source")} for w in widgets.list()]))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        widgets.start_watcher()
        yield
        widgets.stop_watcher()

    app = FastAPI(title="OpsDeck", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.audit = audit
    app.state.widgets = widgets
    app.state.assistant = assistant

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
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/w/"):  # widget frames set their own CSP
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'")
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
        via = "ui"
        if body.widget:
            widgets.check_action(body.widget, name, op)
            via = f"widget:{body.widget}"
        return actions.prepare(user, name, op, body.params, via=via)

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
        if not assistant.available and not body.provider:
            raise HTTPException(503, assistant.status()["detail"])
        return assistant.run(user, [m.model_dump() for m in body.messages], provider=body.provider,
                             model=body.model, playbook=body.playbook)

    @app.post("/api/chat/stream")
    def chat_stream(body: ChatBody, user: User = Depends(current_user)):
        if not assistant.available and not body.provider:
            raise HTTPException(503, assistant.status()["detail"])
        events = assistant.stream(user, [m.model_dump() for m in body.messages], provider=body.provider,
                                  model=body.model, playbook=body.playbook)
        return StreamingResponse(sse(events), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/llm/providers")
    def llm_providers(_user: User = Depends(current_user)):
        return providers.status()

    @app.get("/api/llm/models")
    def llm_models(provider: str, _user: User = Depends(current_user)):
        return {"provider": provider, "models": providers.get(provider).list_models()}

    @app.get("/api/playbooks")
    def list_playbooks(_user: User = Depends(current_user)):
        return [{k: v for k, v in p.items() if k != "instructions"} for p in playbooks.all()]

    # -------------------------------------------------------------- widgets
    @app.get("/api/widgets")
    def list_widgets(_user: User = Depends(current_user)):
        return widgets.list()

    @app.get("/api/widgets/catalog")
    def widget_catalog(_user: User = Depends(current_user)):
        return sorted(widgets.catalog().values(), key=lambda c: c["id"])

    @app.get("/api/widgets/guide", include_in_schema=False)
    def widget_guide(_user: User = Depends(current_user)):
        from .widgets.builder import GUIDE

        return {"markdown": GUIDE}

    @app.post("/api/widgets/generate")
    def generate_widget(body: GenerateBody, user: User = Depends(current_user)):
        return builder.build(user, body.prompt, widget_id=body.widget_id, provider=body.provider, model=body.model)

    @app.post("/api/widgets/generate/stream")
    def generate_widget_stream(body: GenerateBody, user: User = Depends(current_user)):
        widgets._require_author(user)
        events = builder.stream(user, body.prompt, widget_id=body.widget_id, provider=body.provider, model=body.model)
        return StreamingResponse(sse(events), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/widgets/validate")
    def validate_widget(body: WidgetBody, _user: User = Depends(current_user)):
        return widgets.validate(body.manifest, body.view_js, body.data_py)

    @app.get("/api/widgets/{wid}")
    def get_widget(wid: str, _user: User = Depends(current_user)):
        return widgets.detail(wid)

    @app.put("/api/widgets/{wid}")
    def put_widget(wid: str, body: WidgetBody, user: User = Depends(current_user)):
        return widgets.save(user, wid, body.manifest, body.view_js, body.data_py)

    @app.delete("/api/widgets/{wid}")
    def delete_widget(wid: str, user: User = Depends(current_user)):
        return widgets.delete(user, wid)

    @app.post("/api/widgets/{wid}/status")
    def widget_status(wid: str, body: StatusBody, user: User = Depends(current_user)):
        return widgets.set_status(user, wid, body.status)

    @app.post("/api/widgets/{wid}/approve")
    def approve_widget(wid: str, user: User = Depends(current_user)):
        return widgets.approve(user, wid)

    @app.post("/api/widgets/{wid}/rollback")
    def rollback_widget(wid: str, body: RollbackBody, user: User = Depends(current_user)):
        return widgets.rollback(user, wid, body.version)

    @app.get("/api/widgets/{wid}/data")
    def widget_data(wid: str, request: Request, user: User = Depends(current_user)):
        return widgets.data(user, wid, dict(request.query_params))

    @app.get("/api/widgets/{wid}/ops/{name}/{op}")
    def widget_read(wid: str, name: str, op: str, request: Request, _user: User = Depends(current_user)):
        return {"data": widgets.bridged_read(wid, name, op, dict(request.query_params))}

    @app.get("/w/{wid}/frame", include_in_schema=False)
    def widget_frame(wid: str, _user: User = Depends(current_user)):
        w = widgets.get(wid)
        html, csp = frame_html(w["files"].get("view.js", ""))
        return HTMLResponse(html, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})

    @app.get("/api/dashboard")
    def get_dashboard(user: User = Depends(current_user)):
        return {"items": widgets.dashboard(user)}

    @app.put("/api/dashboard")
    def put_dashboard(body: DashboardBody, user: User = Depends(current_user)):
        return {"items": widgets.save_dashboard(user, body.items)}

    @app.get("/api/events")
    async def events(request: Request, _user: User = Depends(current_user)):
        q = widgets.bus.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                widgets.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

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
