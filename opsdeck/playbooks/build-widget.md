---
id: build-widget
title: Build a widget
description: Describe a widget in plain English and the assistant builds it, live, from your connected data.
starter: "Build a widget that shows "
order: 6
---
The user wants a new console widget. Clarify only if the request is truly ambiguous; otherwise call
`opsdeck__build_widget` with a precise description: what data (which integration ops), filters/inputs, the
visual (table, stat tiles, line/bar chart, status grid), refresh interval and any row actions. To change an
existing widget pass its `widget_id` and describe the change. After it returns, tell the user the widget id,
that it is a draft visible under Widgets, and what it shows.
