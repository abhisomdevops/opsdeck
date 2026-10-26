"""Merge incidents and alerts from several systems into one ranked list."""

RANK = {"critical": 0, "high": 1, "warning": 2, "low": 3}


def _sev(value):
    v = str(value or "").lower()
    if v in ("critical", "page", "p1", "highest"):
        return "critical"
    if v in ("high", "p2", "error"):
        return "high"
    if v in ("warning", "warn", "medium", "p3"):
        return "warning"
    return "low"


def transform(sources, inputs, ctx):
    items = []
    for i in sources.get("pd") or []:
        items.append({"source": "PagerDuty", "severity": _sev(i.get("priority") or i.get("urgency")),
                      "title": i.get("title"), "service": i.get("service"), "state": i.get("status"),
                      "since": i.get("created"), "owner": i.get("assignee"), "ref": i.get("id"), "url": i.get("url")})
    for a in sources.get("prom") or []:
        labels = a.get("labels") or {}
        items.append({"source": "Prometheus", "severity": _sev(a.get("severity")), "title": a.get("summary") or a.get("alert"),
                      "service": labels.get("service") or labels.get("namespace"), "state": a.get("state"),
                      "since": a.get("since"), "owner": "", "ref": a.get("alert"), "url": ""})
    for a in sources.get("graf") or []:
        items.append({"source": "Grafana", "severity": _sev(a.get("severity")), "title": a.get("alert"),
                      "service": a.get("folder"), "state": a.get("state"), "since": a.get("since"), "owner": "",
                      "ref": a.get("alert"), "url": ""})
    for j in sources.get("jira") or []:
        if j.get("type") != "Incident":
            continue
        items.append({"source": "Jira", "severity": _sev(j.get("priority")), "title": f"{j.get('key')}: {j.get('summary')}",
                      "service": "", "state": j.get("status"), "since": j.get("updated"), "owner": j.get("assignee"),
                      "ref": j.get("key"), "url": j.get("url")})
    items.sort(key=lambda x: (RANK.get(x["severity"], 9), str(x.get("since") or "")))
    pd = sources.get("pd") or []
    return {
        "items": items,
        "counts": {
            "incidents": len(pd),
            "unacked": sum(1 for i in pd if i.get("status") == "triggered"),
            "critical": sum(1 for x in items if x["severity"] == "critical"),
            "alerts": len(sources.get("prom") or []) + len(sources.get("graf") or []),
        },
    }
