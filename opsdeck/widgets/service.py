"""Widget store, hot reload, data execution, approvals, history and dashboards.

A widget is a folder::

    widget.json   manifest: title, layout, inputs, data sources (read ops), ops it may call
    view.js       front end; runs in a sandboxed iframe with the widget SDK
    data.py       optional ``transform(sources, inputs, ctx)``; runs only once an admin approved its hash

Built-in widgets live in ``opsdeck/widgets_builtin``; custom ones (and edited
copies of built-ins) in ``$OPSDECK_DATA_DIR/widgets``. A file watcher reloads a
widget the moment any of its files change on disk (from the editor, the AI
builder, ``git pull`` or a text editor) and pushes an event to every open
browser, which re-renders just that widget. Nothing restarts.
"""

from __future__ import annotations

import asyncio
import builtins
import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..auth import User
from ..config import Settings, env
from ..registry import OpError, Registry
from .checks import ALLOWED_IMPORTS, ID_RE, lint_python, validate_manifest, validate_view

log = logging.getLogger("opsdeck.widgets")

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "widgets_builtin"
FILES = ("widget.json", "view.js", "data.py")
TRANSFORM_TIMEOUT = 20
SOURCE_TIMEOUT = 30

_SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    "abs all any bool dict divmod enumerate filter float format frozenset hash int isinstance issubclass iter len "
    "list map max min next object pow range repr reversed round set slice sorted str sum tuple type zip "
    "Exception ValueError KeyError TypeError IndexError ZeroDivisionError StopIteration True False None").split()}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002 - mirrors __import__
    if level or name not in ALLOWED_IMPORTS:
        raise ImportError(f"import of '{name}' is not allowed in widgets")
    return __import__(name, globals, locals, fromlist, level)


def _rev(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in FILES:
        h.update(name.encode() + b"\0" + (files.get(name) or "").encode() + b"\0")
    return h.hexdigest()[:16]


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def render_template(value: Any, inputs: dict) -> Any:
    """'{{inputs.x}}' alone keeps the input's type; embedded placeholders are string-substituted."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*inputs\.(\w+)\s*\}\}", value)
        if whole:
            return inputs.get(whole.group(1))
        return re.sub(r"\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: str(inputs.get(m.group(1), "") or ""), value)
    if isinstance(value, dict):
        return {k: render_template(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, inputs) for v in value]
    return value


# ---------------------------------------------------------------- event bus
class EventBus:
    """Fan-out of events to SSE subscribers. Safe to publish from any thread."""

    def __init__(self):
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self.history: list[dict] = []  # last few events, for tests and late subscribers

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lp, x) for lp, x in self._subs if x is not q]

    def publish(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        self.history = (self.history + [event])[-50:]
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except (RuntimeError, asyncio.QueueFull):  # loop closed or slow client: drop
                pass


# ------------------------------------------------------------------ context
class TransformContext:
    """What data.py may use: read-only ops the manifest declared, plus helpers."""

    def __init__(self, registry: Registry, allowed: set[str], widget_id: str):
        self._registry = registry
        self._allowed = allowed
        self.widget_id = widget_id
        self.log_lines: list[str] = []

    def read(self, integration: str, op: str, **params) -> Any:
        ref = f"{integration}.{op}"
        if ref not in self._allowed:
            raise OpError(f"widget did not declare {ref} in sources or uses", 403)
        return self._registry.read(integration, op, params)

    def log(self, *parts) -> None:
        self.log_lines.append(" ".join(str(p) for p in parts)[:500])

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------ service
class WidgetService:
    def __init__(self, settings: Settings, registry: Registry, audit: AuditLog, all_classes: list | None = None):
        self.settings = settings
        self.registry = registry
        self.audit = audit
        self.custom_dir = settings.data_dir / "widgets"
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.dash_file = settings.data_dir / "dashboards.json"
        self.bus = EventBus()
        self.python_mode = (env("OPSDECK_WIDGET_PYTHON", "approval") or "approval").lower()  # approval | off | trusted
        self._all_classes = all_classes or []
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="widget")
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self.reload_all()

    # ------------------------------------------------------------ catalog
    def catalog(self) -> dict[str, dict]:
        cat = {c["id"]: c for c in self.registry.catalog(kinds=("read", "action"))}
        enabled = {i.name for i in self.registry.all()}
        for cls in self._all_classes:
            if cls.name in enabled:
                continue
            for spec in cls.ops().values():
                cat.setdefault(f"{cls.name}.{spec.name}", {
                    "id": f"{cls.name}.{spec.name}", "integration": cls.name, "op": spec.name, "kind": spec.kind,
                    "risk": spec.risk, "mode": "disabled", "description": spec.description,
                    "params": {p.name: p.json_schema() | {"required": p.required} for p in spec.params}})
        return cat

    # --------------------------------------------------------------- disk
    def _dir_for(self, wid: str) -> tuple[Path | None, str]:
        custom = self.custom_dir / wid
        if (custom / "widget.json").is_file():
            return custom, "custom"
        builtin = BUILTIN_DIR / wid
        if (builtin / "widget.json").is_file():
            return builtin, "builtin"
        return None, ""

    def _read_files(self, d: Path) -> dict[str, str]:
        out = {}
        for name in FILES:
            p = d / name
            if p.is_file():
                out[name] = p.read_text(encoding="utf-8")
        return out

    def _approval(self, wid: str) -> dict | None:
        p = self.custom_dir / wid / ".approval.json"
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _load(self, wid: str) -> dict | None:
        d, source = self._dir_for(wid)
        if d is None:
            return None
        files = self._read_files(d)
        try:
            manifest = json.loads(files.get("widget.json") or "{}")
        except json.JSONDecodeError as exc:
            manifest = {"id": wid, "title": wid}
            parse_error = f"widget.json: {exc}"
        else:
            parse_error = None
        manifest["id"] = wid
        errors, warnings = validate_manifest(manifest, self.catalog())
        if parse_error:
            errors.insert(0, parse_error)
        errors += validate_view(files.get("view.js", ""))
        py = files.get("data.py")
        py_errors = lint_python(py) if py else []
        approval = self._approval(wid)
        if py is None:
            py_state = "none"
        elif self.python_mode == "off":
            py_state = "disabled"
        elif self.python_mode == "trusted" or (source == "builtin"):
            py_state = "trusted"
        elif approval and approval.get("sha256") == _sha(py) and not py_errors:
            py_state = "approved"
        else:
            py_state = "needs_approval"
        return {
            "id": wid, "source": source, "dir": d, "files": files, "manifest": manifest, "rev": _rev(files),
            "errors": errors, "warnings": warnings, "python_errors": py_errors, "python": py_state,
            "approval": approval, "status": manifest.get("status", "published" if source == "builtin" else "draft"),
            "builtin_available": (BUILTIN_DIR / wid / "widget.json").is_file(),
        }

    def reload_all(self) -> None:
        ids = set()
        for root in (BUILTIN_DIR, self.custom_dir):
            if root.is_dir():
                ids |= {p.name for p in root.iterdir() if (p / "widget.json").is_file() and ID_RE.match(p.name)}
        with self._lock:
            self._cache = {}
            for wid in sorted(ids):
                w = self._load(wid)
                if w:
                    self._cache[wid] = w

    def reload(self, wid: str, reason: str = "changed") -> dict | None:
        with self._lock:
            old = self._cache.get(wid)
            w = self._load(wid)
            if w is None:
                self._cache.pop(wid, None)
            else:
                self._cache[wid] = w
        if w is None and old is not None:
            self.bus.publish({"type": "widget_deleted", "id": wid})
        elif w is not None and (old is None or old["rev"] != w["rev"] or reason != "changed"
                                or old["python"] != w["python"] or old["status"] != w["status"]):
            self.bus.publish({"type": "widget_changed", "id": wid, "rev": w["rev"], "reason": reason,
                              "errors": len(w["errors"])})
        return w

    # ------------------------------------------------------------ watcher
    def start_watcher(self) -> None:
        if self._watcher or env("OPSDECK_WIDGET_WATCH", "true") == "false":
            return
        try:
            import watchfiles  # noqa: F401
        except ImportError:  # pragma: no cover
            log.warning("watchfiles not installed; widget hot reload from disk is off (editor saves still apply)")
            return
        self._watcher = threading.Thread(target=self._watch, name="widget-watch", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        import watchfiles

        roots = [p for p in (self.custom_dir, BUILTIN_DIR) if p.is_dir()]
        log.info("watching %s for widget changes", ", ".join(map(str, roots)))
        for changes in watchfiles.watch(*roots, stop_event=self._stop, debounce=150, step=50, yield_on_timeout=False):
            touched = set()
            for _change, path in changes:
                p = Path(path)
                for root in roots:
                    try:
                        rel = p.resolve().relative_to(root.resolve())
                    except ValueError:
                        continue
                    if rel.parts and not rel.parts[0].startswith(".") and ".history" not in rel.parts:
                        touched.add(rel.parts[0])
            for wid in touched:
                if ID_RE.match(wid):
                    try:
                        self.reload(wid)
                    except Exception:  # noqa: BLE001 - never kill the watcher
                        log.exception("reloading widget %s failed", wid)

    # -------------------------------------------------------------- query
    def get(self, wid: str) -> dict:
        with self._lock:
            w = self._cache.get(wid)
        if w is None:
            w = self.reload(wid, reason="loaded")
        if w is None:
            raise OpError(f"unknown widget '{wid}'", 404)
        return w

    def summary(self, w: dict) -> dict:
        m = w["manifest"]
        return {"id": w["id"], "title": m.get("title", w["id"]), "description": m.get("description", ""),
                "category": m.get("category", "Custom"), "width": m.get("width", "half"),
                "height": m.get("height", 320), "refresh_secs": m.get("refresh_secs", 0),
                "inputs": m.get("inputs") or [], "uses": self.allowed_ops(w), "status": w["status"],
                "source": w["source"], "rev": w["rev"], "python": w["python"], "errors": w["errors"],
                "warnings": w["warnings"], "python_errors": w["python_errors"], "author": m.get("author"),
                "updated": m.get("updated"), "builtin_available": w["builtin_available"]}

    def list(self) -> list[dict]:
        with self._lock:
            items = list(self._cache.values())
        return [self.summary(w) for w in items]

    def detail(self, wid: str) -> dict:
        w = self.get(wid)
        return self.summary(w) | {"manifest": w["manifest"], "view_js": w["files"].get("view.js", ""),
                                  "data_py": w["files"].get("data.py"), "approval": w["approval"],
                                  "history": self.history(wid)}

    @staticmethod
    def allowed_ops(w: dict) -> list[str]:
        m = w["manifest"]
        refs = [f"{s.get('integration')}.{s.get('op')}" for s in m.get("sources") or [] if isinstance(s, dict)]
        return sorted(set(refs) | set(m.get("uses") or []))

    # --------------------------------------------------------------- data
    def data(self, user: User, wid: str, raw_inputs: dict) -> dict:
        w = self.get(wid)
        m = w["manifest"]
        if w["errors"]:
            raise OpError("widget has errors: " + "; ".join(w["errors"][:3]), 422)
        inputs = self._coerce_inputs(m, raw_inputs)
        started = time.monotonic()
        sources, errors = {}, {}

        def run(src):
            params = {k: v for k, v in render_template(src.get("params") or {}, inputs).items() if v not in (None, "")}
            return self.registry.read(src["integration"], src["op"], params)

        futs = {s["id"]: self._pool.submit(run, s) for s in m.get("sources") or []}
        for sid, fut in futs.items():
            try:
                sources[sid] = fut.result(timeout=SOURCE_TIMEOUT)
            except FutTimeout:
                errors[sid] = "timed out"
                sources[sid] = None
            except OpError as exc:
                errors[sid] = str(exc)
                sources[sid] = None
        result: Any = sources
        logs: list[str] = []
        py = w["files"].get("data.py")
        if py:
            if w["python"] in ("approved", "trusted"):
                ctx = TransformContext(self.registry, set(self.allowed_ops(w)) - {
                    c for c, i in self.catalog().items() if i["kind"] != "read"}, wid)
                fut = self._pool.submit(self._run_transform, wid, py, sources, inputs, ctx)
                try:
                    result = fut.result(timeout=TRANSFORM_TIMEOUT)
                except FutTimeout:
                    errors["transform"] = f"data.py took longer than {TRANSFORM_TIMEOUT}s"
                except Exception as exc:  # noqa: BLE001 - report widget code errors to the author
                    errors["transform"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                logs = ctx.log_lines
            elif w["python"] == "needs_approval":
                errors["transform"] = "data.py changed and is waiting for admin approval; showing raw sources"
            elif w["python"] == "disabled":
                errors["transform"] = "Python data functions are disabled (OPSDECK_WIDGET_PYTHON=off)"
        try:
            json.dumps(result, default=str)
        except (TypeError, ValueError) as exc:
            errors["transform"] = f"transform returned non-JSON data: {exc}"
            result = sources
        return {"data": json.loads(json.dumps(result, default=str)), "inputs": inputs, "errors": errors,
                "logs": logs, "rev": w["rev"], "elapsed_ms": int((time.monotonic() - started) * 1000),
                "demo": sorted({s["integration"] for s in m.get("sources") or []
                                if self._mode(s["integration"]) == "demo"})}

    def _mode(self, integration: str) -> str:
        try:
            return self.registry.get(integration).mode
        except OpError:
            return "disabled"

    @staticmethod
    def _run_transform(wid: str, source: str, sources: dict, inputs: dict, ctx: TransformContext) -> Any:
        ns: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS | {"__import__": _safe_import, "print": ctx.log},
                              "__name__": f"widget_{wid.replace('-', '_')}"}
        exec(compile(source, f"<widget {wid}/data.py>", "exec"), ns)  # noqa: S102 - gated by admin approval
        return ns["transform"](sources, inputs, ctx)

    @staticmethod
    def _coerce_inputs(m: dict, raw: dict) -> dict:
        out = {}
        for inp in m.get("inputs") or []:
            name, typ = inp["name"], inp.get("type", "string")
            v = raw.get(name, inp.get("default"))
            if v in (None, ""):
                out[name] = inp.get("default") if v is None else ("" if typ != "boolean" else False)
                continue
            if typ == "integer":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise OpError(f"input '{name}' must be an integer") from None
            elif typ == "boolean":
                v = v if isinstance(v, bool) else str(v).lower() in {"1", "true", "yes", "on"}
            else:
                v = str(v)
                if typ == "select" and inp.get("options") and v not in [str(o) for o in inp["options"]]:
                    raise OpError(f"input '{name}' must be one of {inp['options']}")
            out[name] = v
        return out

    def bridged_read(self, wid: str, integration: str, op: str, params: dict) -> Any:
        w = self.get(wid)
        if f"{integration}.{op}" not in self.allowed_ops(w):
            raise OpError(f"widget '{wid}' did not declare {integration}.{op}", 403)
        return self.registry.read(integration, op, params)

    def check_action(self, wid: str, integration: str, op: str) -> None:
        w = self.get(wid)
        if f"{integration}.{op}" not in (w["manifest"].get("uses") or []):
            raise OpError(f"widget '{wid}' did not declare {integration}.{op} in uses", 403)

    # -------------------------------------------------------------- write
    def _require_author(self, user: User) -> None:
        role = env("OPSDECK_WIDGET_AUTHOR_ROLE", "operator") or "operator"
        if not user.can(role):
            raise OpError(f"creating or editing widgets requires role '{role}'", 403)

    def validate(self, manifest: dict, view_js: str, data_py: str | None) -> dict:
        errors, warnings = validate_manifest(manifest, self.catalog())
        errors += validate_view(view_js or "")
        py_errors = lint_python(data_py) if data_py else []
        return {"errors": errors, "warnings": warnings, "python_errors": py_errors,
                "ok": not errors and not py_errors}

    def save(self, user: User, wid: str, manifest: dict, view_js: str, data_py: str | None = None,
             via: str = "editor", allow_invalid: bool = True) -> dict:
        self._require_author(user)
        if not ID_RE.match(wid):
            raise OpError("widget id must be 2-49 chars of lowercase letters, digits and dashes")
        manifest = dict(manifest or {})
        manifest["id"] = wid
        check = self.validate(manifest, view_js, data_py)
        if not allow_invalid and not check["ok"]:
            return {"saved": False, **check}
        d = self.custom_dir / wid
        with self._lock:
            existing_dir, source = self._dir_for(wid)
            if existing_dir is not None:
                self._snapshot(wid, existing_dir)
                prev = json.loads(self._read_files(existing_dir).get("widget.json") or "{}")
                manifest.setdefault("created", prev.get("created"))
                manifest.setdefault("author", prev.get("author"))
            now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            manifest.setdefault("created", now)
            manifest.setdefault("author", user.id)
            manifest["updated"] = now
            manifest["updated_by"] = user.id
            manifest.setdefault("status", "draft")
            d.mkdir(parents=True, exist_ok=True)
            (d / "widget.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (d / "view.js").write_text(view_js or "", encoding="utf-8")
            if data_py:
                (d / "data.py").write_text(data_py, encoding="utf-8")
            elif (d / "data.py").exists():
                (d / "data.py").unlink()
        self.audit.record(user=user.id, role=user.role, integration="widgets", op="save", mode="n/a",
                          status="executed", params={"id": wid, "has_python": bool(data_py)}, via=via)
        w = self.reload(wid, reason="saved")
        return {"saved": True, "widget": self.summary(w), **check}

    def set_status(self, user: User, wid: str, status: str) -> dict:
        self._require_author(user)
        if status not in {"draft", "published"}:
            raise OpError("status must be draft or published")
        w = self.get(wid)
        if status == "published" and w["errors"]:
            raise OpError("fix the widget's errors before publishing", 422)
        files = w["files"]
        m = dict(w["manifest"], status=status)
        return self.save(user, wid, m, files.get("view.js", ""), files.get("data.py"), via="publish")

    def approve(self, user: User, wid: str) -> dict:
        if not user.can("admin"):
            raise OpError("approving widget Python code requires role 'admin'", 403)
        w = self.get(wid)
        py = w["files"].get("data.py")
        if not py:
            raise OpError("this widget has no data.py to approve")
        if w["python_errors"]:
            raise OpError("data.py has lint errors: " + "; ".join(w["python_errors"][:3]), 422)
        d = self.custom_dir / wid
        if w["source"] == "builtin":  # approving a built-in makes no sense: they are trusted
            return {"approved": True, "trusted": True}
        appr = {"sha256": _sha(py), "by": user.id, "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        (d / ".approval.json").write_text(json.dumps(appr), encoding="utf-8")
        self.audit.record(user=user.id, role=user.role, integration="widgets", op="approve_python", mode="n/a",
                          status="executed", params={"id": wid, "sha256": appr["sha256"][:16]}, via="ui")
        self.reload(wid, reason="approved")
        return {"approved": True, **appr}

    def delete(self, user: User, wid: str) -> dict:
        if not user.can("admin"):
            raise OpError("deleting widgets requires role 'admin'", 403)
        d = self.custom_dir / wid
        if not d.is_dir():
            raise OpError("built-in widgets cannot be deleted (only your edited copy can be reset)", 400)
        self._snapshot(wid, d)
        for name in (*FILES, ".approval.json"):
            if (d / name).exists():
                (d / name).unlink()
        try:
            d.rmdir()
        except OSError:
            pass
        self.audit.record(user=user.id, role=user.role, integration="widgets", op="delete", mode="n/a",
                          status="executed", params={"id": wid}, via="ui")
        w = self.reload(wid, reason="reset")
        return {"deleted": wid, "reverted_to_builtin": w is not None}

    # ------------------------------------------------------------ history
    def _hist_dir(self, wid: str) -> Path:
        return self.custom_dir / ".history" / wid

    def _snapshot(self, wid: str, src: Path) -> None:
        files = self._read_files(src)
        if not files:
            return
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
        dest = self._hist_dir(wid) / f"{stamp}-{_rev(files)[:8]}"
        dest.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (dest / name).write_text(text, encoding="utf-8")
        versions = sorted(self._hist_dir(wid).iterdir())
        for old in versions[:-50]:  # keep the last 50 versions
            shutil.rmtree(old, ignore_errors=True)

    def history(self, wid: str) -> list[dict]:
        d = self._hist_dir(wid)
        if not d.is_dir():
            return []
        out = []
        for v in sorted(d.iterdir(), reverse=True):
            try:
                m = json.loads((v / "widget.json").read_text())
            except (OSError, json.JSONDecodeError):
                m = {}
            out.append({"version": v.name, "updated": m.get("updated"), "by": m.get("updated_by") or m.get("author"),
                        "has_python": (v / "data.py").exists()})
        return out

    def rollback(self, user: User, wid: str, version: str) -> dict:
        if not re.fullmatch(r"[0-9T]+-[0-9a-f]{8}", version or ""):
            raise OpError("invalid version")
        v = self._hist_dir(wid) / version
        if not v.is_dir():
            raise OpError("unknown version", 404)
        files = self._read_files(v)
        m = json.loads(files.get("widget.json") or "{}")
        m["rolled_back_from"] = version
        return self.save(user, wid, m, files.get("view.js", ""), files.get("data.py"), via="rollback")

    # ---------------------------------------------------------- dashboard
    DEFAULT_DASHBOARD = [{"widget": "incident-board", "width": "full"},
                         {"widget": "golden-signals", "width": "half"},
                         {"widget": "cluster-health", "width": "half"},
                         {"widget": "cve-hotlist", "width": "full"}]

    def _dashboards(self) -> dict:
        try:
            return json.loads(self.dash_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def dashboard(self, user: User) -> list[dict]:
        items = self._dashboards().get(user.id)
        if items is None:
            items = [i for i in self.DEFAULT_DASHBOARD if i["widget"] in self._cache]
        return [i for i in items if i.get("widget") in self._cache]

    def save_dashboard(self, user: User, items: list[dict]) -> list[dict]:
        clean = []
        for it in items[:40]:
            if isinstance(it, dict) and it.get("widget") in self._cache:
                clean.append({"widget": it["widget"], "width": it.get("width") if it.get("width") in
                              {"full", "half", "third"} else "half", "inputs": it.get("inputs") or {}})
        with self._lock:
            data = self._dashboards()
            data[user.id] = clean
            self.dash_file.write_text(json.dumps(data, indent=1))
        return clean
