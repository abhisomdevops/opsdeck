"""AI widget builder: describe a widget in plain English, get a working one.

The model gets the widget guide, the catalog of operations this instance
exposes, and (when editing) the widget's current files. It can sample real
operation output to learn the data shape, then calls ``save_widget``. Every
save is validated and dry-run against live sources; errors go back to the
model so it fixes them itself. The result is saved as a draft and appears in
every browser immediately via hot reload.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

from ..auth import User
from ..config import Settings
from ..llm import Providers
from ..registry import OpError
from .service import BUILTIN_DIR, WidgetService

log = logging.getLogger("opsdeck.widgets.builder")
GUIDE = (Path(__file__).parent / "guide.md").read_text(encoding="utf-8")
MAX_STEPS = 10
SAMPLE_CHARS = 5000

BUILDER_PROMPT = """You build widgets for OpsDeck, an SRE operations console. Follow the widget guide exactly.

{guide}

## Operations available on this instance
Format: integration.op(param: type, ...*required) - description [mode]
{catalog}

## How to work
1. Pick the operations that answer the request. If you are unsure what an operation returns, call \
`sample_op` first (it runs the real read operation and shows the JSON shape).
2. Write the widget and call `save_widget` with the complete manifest (a JSON object, not a string), \
view_js, and data_py only if it is really needed. Prefer JavaScript-only widgets: data.py needs an \
admin's approval before it runs.
3. `save_widget` validates and dry-runs the sources. If it reports errors, fix them and save again.
4. When it saves cleanly, reply with two or three sentences: what the widget shows and how to use it.

Rules: render something useful when a source is null or empty; use sdk components and text nodes \
(never innerHTML with data); keep it focused; ids are lowercase-with-dashes; put actions only in \
"uses" and always route them through sdk.action; do not invent operations or params.
{existing}"""

EXAMPLE_IDS = ("golden-signals", "incident-board")


def _catalog_text(catalog: dict[str, dict]) -> str:
    lines = []
    for ref, info in sorted(catalog.items()):
        params = ", ".join(f"{n}: {p.get('type', 'string')}{'*' if p.get('required') else ''}"
                           f"{'=' + json.dumps(p['default']) if p.get('default') not in (None, '') else ''}"
                           f"{' ' + '|'.join(p['enum']) if p.get('enum') else ''}"
                           for n, p in info["params"].items())
        tag = "ACTION, " if info["kind"] == "action" else ""
        lines.append(f"- {ref}({params}) - {info['description']} [{tag}{info.get('mode', 'live')}]")
    return "\n".join(lines)


def _examples() -> str:
    out = []
    for wid in EXAMPLE_IDS:
        d = BUILTIN_DIR / wid
        if not d.is_dir():
            continue
        parts = [f"### Example widget: {wid}"]
        for name in ("widget.json", "view.js", "data.py"):
            p = d / name
            if p.is_file():
                lang = {"widget.json": "json", "view.js": "js", "data.py": "python"}[name]
                parts.append(f"{name}:\n```{lang}\n{p.read_text()}\n```")
        out.append("\n".join(parts))
    return "\n\n".join(out)


def _shape(value, depth=0) -> str:
    if isinstance(value, list):
        return f"list[{len(value)}] of {_shape(value[0], depth + 1) if value else 'nothing'}"
    if isinstance(value, dict) and depth < 3:
        return "{" + ", ".join(f"{k}: {_shape(v, depth + 1)}" for k, v in list(value.items())[:25]) + "}"
    return type(value).__name__


TOOLS = [
    {"name": "sample_op", "description": "Run a read operation and return a truncated sample of its JSON output and shape.",
     "input_schema": {"type": "object", "properties": {
         "integration": {"type": "string"}, "op": {"type": "string"},
         "params": {"type": "object", "description": "Operation parameters"}},
         "required": ["integration", "op"]}},
    {"name": "save_widget", "description": "Validate, save (as a draft) and dry-run the widget. Returns errors to fix.",
     "input_schema": {"type": "object", "properties": {
         "manifest": {"type": "object", "description": "widget.json content as an object"},
         "view_js": {"type": "string", "description": "Complete view.js defining function render(ctx)"},
         "data_py": {"type": "string", "description": "Optional complete data.py; omit unless needed"}},
         "required": ["manifest", "view_js"]}},
]


class WidgetBuilder:
    def __init__(self, settings: Settings, widgets: WidgetService, providers: Providers):
        self.settings = settings
        self.widgets = widgets
        self.providers = providers

    def _unique_id(self, wanted: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", (wanted or "widget").lower()).strip("-")[:40] or "widget"
        if len(base) < 2:
            base = f"w-{base}"
        wid, n = base, 2
        existing = {w["id"] for w in self.widgets.list()}
        while wid in existing:
            wid = f"{base}-{n}"
            n += 1
        return wid

    def stream(self, user: User, prompt: str, widget_id: str | None = None, provider: str | None = None,
               model: str | None = None) -> Iterator[dict]:
        self.widgets._require_author(user)
        if not prompt or not prompt.strip():
            raise OpError("describe the widget you want")
        llm = self.providers.get(provider, model)
        existing = ""
        if widget_id:
            d = self.widgets.detail(widget_id)
            existing = (f"\n\n## You are editing the existing widget '{widget_id}'. Keep its id. Current files:\n"
                        f"widget.json:\n```json\n{json.dumps(d['manifest'], indent=2)}\n```\nview.js:\n```js\n"
                        f"{d['view_js']}\n```\n" + (f"data.py:\n```python\n{d['data_py']}\n```\n" if d["data_py"] else ""))
        system = BUILDER_PROMPT.format(guide=GUIDE, catalog=_catalog_text(self.widgets.catalog()),
                                       existing=existing) + "\n\n" + _examples()
        messages: list[dict] = [{"role": "user", "content": prompt.strip()}]
        target = widget_id
        saved: dict | None = None
        yield {"type": "start", "provider": llm.name, "model": llm.model, "widget_id": widget_id}

        for _ in range(MAX_STEPS):
            try:
                resp = llm.chat(system, messages, TOOLS, max(self.settings.llm_max_tokens, 8192))
            except Exception as exc:  # noqa: BLE001
                log.exception("builder LLM call failed")
                yield {"type": "error", "detail": f"{llm.title} request failed: {str(exc)[:400]}"}
                break
            if resp.text:
                yield {"type": "text", "text": resp.text}
            messages.append({"role": "assistant", "text": resp.text, "tool_calls": resp.tool_calls, "raw": resp.raw})
            if not resp.tool_calls:
                break
            results = []
            for tc in resp.tool_calls:
                yield {"type": "tool", "tool": tc.name, "input": {k: v for k, v in tc.input.items()
                                                                  if k in ("integration", "op", "params")}}
                if tc.name == "sample_op":
                    content, ok = self._sample(tc.input)
                elif tc.name == "save_widget":
                    content, ok, saved_now, target = self._save(user, tc.input, target, prompt)
                    if saved_now:
                        saved = saved_now
                        yield {"type": "saved", "widget": saved_now["widget"], "ok": ok}
                else:
                    content, ok = f"unknown tool {tc.name}", False
                yield {"type": "tool_result", "tool": tc.name, "ok": ok}
                results.append({"id": tc.id, "name": tc.name, "content": content, "is_error": not ok})
            messages.append({"role": "tool", "results": results})

        if saved:
            yield {"type": "done", "widget": saved["widget"], "ok": not saved["widget"]["errors"]}
        else:
            yield {"type": "done", "widget": None, "ok": False}

    def build(self, user: User, prompt: str, **kw) -> dict:
        out = {"widget": None, "ok": False, "notes": [], "errors": []}
        for ev in self.stream(user, prompt, **kw):
            if ev["type"] == "text":
                out["notes"].append(ev["text"])
            elif ev["type"] == "error":
                out["errors"].append(ev["detail"])
            elif ev["type"] == "done":
                out["widget"], out["ok"] = ev["widget"], ev["ok"]
        return out

    # -------------------------------------------------------------- tools
    def _sample(self, args: dict) -> tuple[str, bool]:
        try:
            data = self.widgets.registry.read(args.get("integration", ""), args.get("op", ""), args.get("params") or {})
        except OpError as exc:
            return f"error: {exc}", False
        text = json.dumps(data, default=str)
        sample = text[:SAMPLE_CHARS] + ("... [truncated]" if len(text) > SAMPLE_CHARS else "")
        return f"shape: {_shape(data)}\nsample: {sample}", True

    def _save(self, user: User, args: dict, target: str | None, prompt: str = ""):
        manifest = args.get("manifest")
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except json.JSONDecodeError as exc:
                return f"error: manifest is not valid JSON: {exc}", False, None, target
        if not isinstance(manifest, dict):
            return "error: manifest must be an object", False, None, target
        wid = target or self._unique_id(str(manifest.get("id") or manifest.get("title") or "widget"))
        manifest.setdefault("status", "draft")
        if prompt:
            manifest["prompt"] = prompt.strip()[:2000]
        try:
            res = self.widgets.save(user, wid, manifest, args.get("view_js") or "", args.get("data_py") or None,
                                    via="ai-builder")
        except OpError as exc:
            return f"error: {exc}", False, None, target
        problems = res["errors"] + [f"data.py: {e}" for e in res["python_errors"]]
        report = {"saved_as": wid, "errors": problems, "warnings": res["warnings"]}
        ok = not problems
        if ok:
            try:
                dry = self.widgets.data(user, wid, {})
                report["source_errors"] = {k: v for k, v in dry["errors"].items() if k != "transform"
                                           or "approval" not in v}
                report["data_shape"] = _shape(dry["data"])
                report["data_sample"] = json.dumps(dry["data"], default=str)[:1500]
                if report["source_errors"]:
                    ok = False
                if res["widget"]["python"] == "needs_approval":
                    report["note"] = ("data.py saved; it will run after an admin approves it. The dry run used raw "
                                      "sources, so view.js must also cope with that shape until then, or move "
                                      "the logic into view.js.")
            except OpError as exc:
                report["source_errors"] = {"widget": str(exc)}
                ok = False
        report["status"] = "ok" if ok else "fix the errors and call save_widget again"
        return json.dumps(report, default=str), ok, res, wid
