"""Playbooks: reusable investigation instructions for the assistant.

A playbook is a Markdown file with a small front-matter header:

    ---
    id: incident-rca
    title: Incident RCA
    description: one line shown in the UI
    starter: "Investigate this incident: "     # text pre-filled in the chat box
    order: 1
    ---
    Instructions appended to the system prompt when the playbook is active.

Built-in playbooks ship in ``opsdeck/playbooks``. Teams add or override them
by dropping files in ``$OPSDECK_DATA_DIR/playbooks``; files are re-read on
every request, so edits apply immediately without a restart.
"""

from __future__ import annotations

import re
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "playbooks"
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def parse(text: str, fallback_id: str) -> dict:
    meta: dict = {}
    body = text
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                meta[k.strip()] = v
        body = m.group(2)
    pid = re.sub(r"[^a-z0-9-]", "-", (meta.get("id") or fallback_id).lower())
    try:
        order = int(meta.get("order", 100))
    except ValueError:
        order = 100
    return {"id": pid, "title": meta.get("title") or pid.replace("-", " ").title(),
            "description": meta.get("description", ""), "starter": meta.get("starter", ""),
            "order": order, "instructions": body.strip()}


class Playbooks:
    def __init__(self, data_dir: Path):
        self.user_dir = data_dir / "playbooks"

    def all(self) -> list[dict]:
        found: dict[str, dict] = {}
        for d, source in ((BUILTIN_DIR, "builtin"), (self.user_dir, "custom")):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                try:
                    pb = parse(f.read_text(encoding="utf-8"), f.stem)
                except OSError:
                    continue
                pb["source"] = source
                found[pb["id"]] = pb  # custom overrides builtin with the same id
        return sorted(found.values(), key=lambda p: (p["order"], p["title"]))

    def get(self, pid: str | None) -> dict | None:
        if not pid:
            return None
        return next((p for p in self.all() if p["id"] == pid), None)
