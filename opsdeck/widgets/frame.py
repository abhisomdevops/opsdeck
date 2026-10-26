"""HTML document for a widget's sandboxed iframe.

The frame is loaded with ``sandbox="allow-scripts"`` (no same-origin), so it
gets an opaque origin: it cannot read the console's cookies, storage or DOM.
Its CSP blocks every network request (``connect-src 'none'``), and only the
nonce'd inline scripts below run. All data reaches it via postMessage from the
parent, which enforces the widget's declared operations.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

_HERE = Path(__file__).parent
_SDK_JS = (_HERE / "sdk.js").read_text(encoding="utf-8")
_SDK_CSS = (_HERE / "sdk.css").read_text(encoding="utf-8")


def _script_safe(js: str) -> str:
    # Prevent the widget's code from closing the <script> element early.
    return re.sub(r"</(script)", r"<\\/\1", js, flags=re.I).replace("<!--", "<\\!--")


def frame_html(view_js: str) -> tuple[str, str]:
    """Return (html, csp)."""
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src data:; "
           "font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_SDK_CSS}</style>
<script nonce="{nonce}">{_script_safe(_SDK_JS)}</script>
</head><body><div id="root"></div>
<script nonce="{nonce}">
{_script_safe(view_js)}
</script>
<script nonce="{nonce}">if (typeof render === "function") window.render = render;</script>
</body></html>"""
    return html, csp
