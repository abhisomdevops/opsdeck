"""Validation for widget manifests and the optional Python data function.

The Python check is a lint, not a sandbox: it rejects obvious escape hatches
(imports outside an allowlist, dunder access, exec/eval/open) so reviewers see
problems early. The real control is that a data.py only runs after an admin
approves its exact SHA-256 (see service.py), the same way a code review gates
a merge.
"""

from __future__ import annotations

import ast
import re

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
OPREF_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
WIDTHS = {"full", "half", "third"}
INPUT_TYPES = {"string", "integer", "boolean", "select"}
MAX_VIEW_BYTES = 100_000
MAX_PY_BYTES = 50_000

ALLOWED_IMPORTS = {"math", "statistics", "datetime", "time", "re", "json", "collections", "itertools", "functools",
                   "operator", "string", "textwrap", "decimal", "fractions", "heapq", "bisect", "dataclasses",
                   "typing", "ipaddress", "urllib.parse"}
BANNED_NAMES = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                "delattr", "input", "breakpoint", "memoryview", "exit", "quit", "help"}


def lint_python(source: str) -> list[str]:
    if len(source.encode()) > MAX_PY_BYTES:
        return [f"data.py is larger than {MAX_PY_BYTES} bytes"]
    try:
        tree = ast.parse(source, filename="data.py")
    except SyntaxError as exc:
        return [f"data.py line {exc.lineno}: {exc.msg}"]
    errors: list[str] = []
    has_transform = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    errors.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_IMPORTS:
                errors.append(f"line {node.lineno}: import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            errors.append(f"line {node.lineno}: access to private attribute '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            errors.append(f"line {node.lineno}: use of '{node.id}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            errors.append(f"line {node.lineno}: global/nonlocal is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "transform" and node in tree.body:
            has_transform = True
    if not has_transform:
        errors.append("data.py must define a top-level function transform(sources, inputs, ctx)")
    return errors


def validate_manifest(m: dict, catalog: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). ``catalog`` maps 'integration.op' -> op info (all known ops,
    including ones from disabled integrations marked with mode='disabled')."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(m, dict):
        return ["widget.json must be an object"], []
    if not ID_RE.match(str(m.get("id", ""))):
        errors.append("id must be 2-49 chars of lowercase letters, digits and dashes")
    if not str(m.get("title", "")).strip() or len(str(m.get("title"))) > 80:
        errors.append("title is required (max 80 chars)")
    if len(str(m.get("description", ""))) > 500:
        errors.append("description max 500 chars")
    if m.get("width", "half") not in WIDTHS:
        errors.append(f"width must be one of {sorted(WIDTHS)}")
    h = m.get("height", 320)
    if not isinstance(h, int) or not 120 <= h <= 1600:
        errors.append("height must be an integer between 120 and 1600 (pixels)")
    r = m.get("refresh_secs", 0)
    if not isinstance(r, int) or (r and not 10 <= r <= 3600):
        errors.append("refresh_secs must be 0 or 10-3600")

    names = set()
    for i, inp in enumerate(m.get("inputs") or []):
        if not isinstance(inp, dict) or not NAME_RE.match(str(inp.get("name", ""))):
            errors.append(f"inputs[{i}].name must be an identifier")
            continue
        names.add(inp["name"])
        if inp.get("type", "string") not in INPUT_TYPES:
            errors.append(f"inputs[{i}].type must be one of {sorted(INPUT_TYPES)}")
        if inp.get("type") == "select" and not isinstance(inp.get("options"), list):
            errors.append(f"inputs[{i}] of type select needs an options list")
    if len(m.get("inputs") or []) > 10:
        errors.append("at most 10 inputs")

    ids = set()
    sources = m.get("sources") or []
    if not isinstance(sources, list) or len(sources) > 10:
        errors.append("sources must be a list of at most 10")
        sources = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict) or not NAME_RE.match(str(s.get("id", ""))):
            errors.append(f"sources[{i}].id must be an identifier")
            continue
        if s["id"] in ids:
            errors.append(f"duplicate source id '{s['id']}'")
        ids.add(s["id"])
        ref = f"{s.get('integration')}.{s.get('op')}"
        info = catalog.get(ref)
        if info is None:
            errors.append(f"sources[{i}]: unknown operation {ref}")
            continue
        if info["kind"] != "read":
            errors.append(f"sources[{i}]: {ref} is an action; sources must be read operations")
        if info.get("mode") == "disabled":
            warnings.append(f"sources[{i}]: {s.get('integration')} is not enabled on this instance")
        params = s.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"sources[{i}].params must be an object")
            continue
        unknown = set(params) - set(info["params"])
        if unknown:
            errors.append(f"sources[{i}]: unknown params for {ref}: {sorted(unknown)} (valid: {sorted(info['params'])})")
        for pname, pinfo in info["params"].items():
            if pinfo.get("required") and pname not in params:
                errors.append(f"sources[{i}]: {ref} requires param '{pname}'")
        for v in params.values():
            for ref_name in re.findall(r"\{\{\s*inputs\.(\w+)\s*\}\}", str(v)):
                if ref_name not in names:
                    errors.append(f"sources[{i}] references unknown input '{ref_name}'")

    for u in m.get("uses") or []:
        if not OPREF_RE.match(str(u)) or u not in catalog:
            errors.append(f"uses: unknown operation '{u}' (format integration.op)")
    return errors, warnings


def validate_view(js: str) -> list[str]:
    errors = []
    if len(js.encode()) > MAX_VIEW_BYTES:
        errors.append(f"view.js is larger than {MAX_VIEW_BYTES} bytes")
    if not re.search(r"function\s+render\s*\(|(?:const|let|var)\s+render\s*=", js):
        errors.append("view.js must define a top-level function render(ctx)")
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts)\s*\(", js):
        errors.append("view.js cannot make network calls; use sdk.read() / ctx.data instead")
    return errors
