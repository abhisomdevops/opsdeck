# OpsDeck widget guide

A widget is three files in a folder named after its id:

| File | Required | What it is |
|---|---|---|
| `widget.json` | yes | Manifest: title, layout, inputs, data sources, operations it may call |
| `view.js` | yes | Front end. Defines `function render(ctx)`. Runs in a sandboxed iframe with no network access. |
| `data.py` | no | `def transform(sources, inputs, ctx)` that reshapes data on the server. Runs only after an admin approves its exact content. |

Saving any file (editor, AI builder, `git pull`, a text editor on the data volume) hot-reloads the widget in every open browser. Nothing restarts.

## widget.json

```json
{
  "id": "payments-lag",                 // lowercase, digits, dashes; same as the folder name
  "title": "Payments settlement lag",
  "description": "One line on what it shows and why.",
  "category": "Observability",          // groups widgets in the gallery
  "width": "half",                      // full | half | third (dashboard default)
  "height": 320,                        // starting height in px; the frame grows to fit content
  "refresh_secs": 60,                   // 0 = manual only; else 10-3600
  "inputs": [                           // filters shown above the widget
    {"name": "namespace", "label": "Namespace", "type": "string", "default": "payments"},
    {"name": "window", "type": "select", "options": ["30", "60", "180"], "default": "60"},
    {"name": "only_bad", "type": "boolean", "default": true}
  ],
  "sources": [                          // read operations run on the server for every render
    {"id": "lag", "integration": "prometheus", "op": "query_range",
     "params": {"query": "max(settlement_lag_seconds{namespace=\"{{inputs.namespace}}\"})", "minutes": "{{inputs.window}}"}},
    {"id": "pods", "integration": "kubernetes", "op": "pods",
     "params": {"namespace": "{{inputs.namespace}}", "problems_only": "{{inputs.only_bad}}"}}
  ],
  "uses": ["kubernetes.pod_logs", "kubernetes.rollout_restart"]   // extra ops view.js may call
}
```

- `{{inputs.name}}` as the whole value keeps the input's type; inside a longer string it is substituted as text.
- Only operations listed in `sources` and `uses` can be called. Sources must be read operations. Actions (changes) must be in `uses` and always show the user a confirmation dialog.
- A source that fails becomes `null` and its error appears in `ctx.errors[source_id]`; render something useful anyway.

## view.js

```js
function render(ctx) {
  const { root, data, inputs, errors, sdk } = ctx;
  // data = { <source id>: <op result>, ... }   (or whatever data.py returned)
  root.append(sdk.stats([{ label: "Lag", value: sdk.fmt.duration(data.lag?.[0]?.last), status: "warn" }]));
  root.append(sdk.lineChart(sdk.promSeries(data.lag), { title: "Lag (s)", thresholds: [{ value: 900, label: "15m" }] }));
}
```

`render` is called again on every refresh and input change with a cleared `root`. It may be `async`.

### ctx

| Field | |
|---|---|
| `root` | Element to render into (cleared before each call) |
| `data` | Source results keyed by source id, or `data.py`'s return value |
| `inputs` | Current input values |
| `errors` | `{source_id or "transform": message}` |
| `demo` | Integrations that are serving demo data |
| `widget` | `{id, title}` |
| `sdk` | Helpers below |

### sdk

Building blocks (all build DOM with text nodes; never use innerHTML with data):

| Helper | |
|---|---|
| `sdk.h(tag, props, ...children)` | Create an element. `props`: `class`, `style` (object), `onclick`, `text`, attributes |
| `sdk.stats([{label, value, unit, status, sub, spark}])` | Row of KPI tiles. `spark` = array of numbers or `[t, v]` points |
| `sdk.tiles([{label, value, status, sub, onClick}])` | Grid of small status tiles |
| `sdk.table(rows, {columns:[{key, label, fmt, render}], actions:[{label, kind:"act", when(row), onClick(row)}], onRowClick, maxRows, empty})` | Sortable table. `fmt`: text, status, time, num, pct, bytes, ms, code |
| `sdk.lineChart(series, {title, height, yFormat, area, thresholds:[{value,label,status}], min, max})` | Time series. `series = [{name, points:[[unixSeconds, value], ...]}]` |
| `sdk.promSeries(rows, labelKey)` | Convert `prometheus.query_range` rows to `lineChart` series |
| `sdk.barChart([{label, value, status}], {title, format})` | Horizontal bars |
| `sdk.sparkline(values)` | Tiny trend line |
| `sdk.badge(text, status?)` | Status pill; status auto-detected from text if omitted |
| `sdk.section(title, ...children)`, `sdk.note(text, status)`, `sdk.md(text)` | Layout and text |
| `sdk.status(value)` | Maps words like "firing", "Degraded", "ok" to `ok`/`warn`/`crit` |
| `sdk.fmt.num / pct / bytes / ms / duration / rel / time` | Formatting |

Calls to the host (return Promises):

| Call | |
|---|---|
| `await sdk.read(integration, op, params)` | Run a declared read operation (e.g. load logs when a row is clicked) |
| `await sdk.action(integration, op, params)` | Propose a declared action; the user confirms in a dialog. Resolves to the result, or `null` if cancelled |
| `sdk.ask(text)` | Open the AI assistant with a question about what the widget shows |
| `sdk.refresh()` | Re-run sources now |
| `sdk.setInputs({name: value})` | Change inputs and re-run sources |

Styling: the frame has a dark theme. Use the sdk components; for custom layout use inline `style` objects and these CSS variables: `--text`, `--muted`, `--panel`, `--line`, `--accent`, `--ok`, `--warn`, `--crit`, `--mono`.

Not available: network calls (`fetch` etc. are blocked), external scripts or fonts, cookies, `localStorage`, the parent page.

## data.py (optional)

Use it when you need to fan out (look something up per row), join several sources, or aggregate a lot of data on the server. Otherwise transform in `view.js`.

```python
def transform(sources, inputs, ctx):
    rows = sources.get("pods") or []
    for r in rows[:10]:
        r["lag"] = ctx.read("prometheus", "query", query=f'settlement_lag_seconds{{pod="{r["name"]}"}}')
    return {"rows": rows}
```

- `ctx.read(integration, op, **params)` runs a read operation declared in `sources` or `uses`.
- `ctx.log(...)` (or `print`) adds a line to the editor's log panel. `ctx.now()` returns the current UTC datetime.
- Return JSON-serialisable data. 20 second time limit.
- Allowed imports: math, statistics, datetime, time, re, json, collections, itertools, functools, operator, string, textwrap, decimal, fractions, heapq, bisect, dataclasses, typing, ipaddress, urllib.parse. No file, network, process or dunder access.
- Every change needs an admin to click **Approve Python** before it runs (built-in widgets are trusted). Until then the widget renders with raw `sources`.
