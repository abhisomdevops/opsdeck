"""Kubernetes: pods, deployments, events, logs, rollout restart, scale.

Auth: in-cluster service account when running inside Kubernetes, otherwise
your kubeconfig (optionally a specific context). Restrict which namespaces the
console may touch with K8S_NAMESPACES.
"""

from __future__ import annotations

import datetime as dt
import threading

from ..config import env, env_bool, env_list
from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago, iso

_lock = threading.Lock()
_api_client = None


def k8s_enabled() -> bool:
    if env("K8S_ENABLED") is not None:  # explicit setting wins
        return env_bool("K8S_ENABLED")
    return bool(env("KUBERNETES_SERVICE_HOST"))  # in-cluster default


def api_client():
    """Shared, lazily-built ApiClient (also used by the Flux integration)."""
    global _api_client
    with _lock:
        if _api_client is None:
            from kubernetes import client, config

            if env("KUBERNETES_SERVICE_HOST") and not env("KUBECONFIG"):
                config.load_incluster_config()
            else:
                config.load_kube_config(context=env("K8S_CONTEXT"))
            _api_client = client.ApiClient()
        return _api_client


def allowed_namespace(ns: str | None) -> str | None:
    """Return ns if allowed. None means 'all namespaces' (only if unrestricted)."""
    allow = env_list("K8S_NAMESPACES")
    if ns in (None, "", "*", "all"):
        if allow:
            raise OpError("choose a namespace; this console is restricted to: " + ", ".join(allow))
        return None
    if allow and ns not in allow:
        raise OpError(f"namespace '{ns}' is not in K8S_NAMESPACES", 403)
    return ns


def _age(ts) -> str | None:
    return iso(ts)


def _pod_row(p) -> dict:
    statuses = p.status.container_statuses or []
    restarts = sum(c.restart_count for c in statuses)
    ready = sum(1 for c in statuses if c.ready)
    reason = p.status.phase
    for c in statuses:
        waiting = c.state.waiting if c.state else None
        terminated = c.state.terminated if c.state else None
        if waiting and waiting.reason:
            reason = waiting.reason
            break
        if terminated and terminated.reason and terminated.reason != "Completed":
            reason = terminated.reason
            break
    return {
        "namespace": p.metadata.namespace, "name": p.metadata.name, "status": reason,
        "ready": f"{ready}/{len(statuses)}", "restarts": restarts,
        "node": p.spec.node_name, "started": _age(p.status.start_time),
    }


def _is_problem(row: dict) -> bool:
    return row["status"] not in {"Running", "Succeeded"} or row["restarts"] >= 5 or (
        row["status"] == "Running" and row["ready"].split("/")[0] != row["ready"].split("/")[1])


class Kubernetes(Integration):
    name = "kubernetes"
    title = "Kubernetes"
    category = "Runtime"
    icon = "k8s"
    description = "Pods, deployments, events and logs for the configured cluster."

    def is_configured(self) -> bool:
        return k8s_enabled()

    def _core(self):
        from kubernetes import client

        return client.CoreV1Api(api_client())

    def _apps(self):
        from kubernetes import client

        return client.AppsV1Api(api_client())

    def health(self) -> dict:
        from kubernetes import client

        info = client.VersionApi(api_client()).get_code()
        ctx = env("K8S_CONTEXT") or ("in-cluster" if env("KUBERNETES_SERVICE_HOST") else "current context")
        return {"ok": True, "detail": f"{ctx}, server {info.git_version}"}

    # ------------------------------------------------------------ reads
    @read_op("List namespaces the console can see.")
    def namespaces(self):
        allow = env_list("K8S_NAMESPACES")
        if allow:
            return [{"name": n} for n in allow]
        items = self._core().list_namespace().items
        return [{"name": n.metadata.name, "status": n.status.phase, "created": iso(n.metadata.creation_timestamp)}
                for n in items]

    @read_op("List pods with status, readiness and restart counts.", (
        Param("namespace", description="Namespace; empty for all (if allowed)"),
        Param("problems_only", "boolean", "Only pods that are not healthy", default=False),
    ))
    def pods(self, namespace=None, problems_only=False):
        ns = allowed_namespace(namespace)
        core = self._core()
        items = core.list_namespaced_pod(ns).items if ns else core.list_pod_for_all_namespaces().items
        rows = [_pod_row(p) for p in items]
        if problems_only:
            rows = [r for r in rows if _is_problem(r)]
        return sorted(rows, key=lambda r: (not _is_problem(r), r["namespace"], r["name"]))

    @read_op("List deployments with desired/ready/updated replicas.", (
        Param("namespace", description="Namespace; empty for all (if allowed)"),
    ))
    def deployments(self, namespace=None):
        ns = allowed_namespace(namespace)
        apps = self._apps()
        items = apps.list_namespaced_deployment(ns).items if ns else apps.list_deployment_for_all_namespaces().items
        rows = []
        for d in items:
            desired = d.spec.replicas or 0
            ready = d.status.ready_replicas or 0
            image = ", ".join(c.image for c in d.spec.template.spec.containers)
            rows.append({
                "namespace": d.metadata.namespace, "name": d.metadata.name,
                "status": "Ready" if ready >= desired else ("ScaledDown" if desired == 0 else "Degraded"),
                "replicas": f"{ready}/{desired}", "updated": d.status.updated_replicas or 0,
                "image": image, "created": iso(d.metadata.creation_timestamp),
            })
        return rows

    @read_op("Recent cluster events, newest first.", (
        Param("namespace", description="Namespace; empty for all (if allowed)"),
        Param("warnings_only", "boolean", "Only Warning events", default=True),
        Param("limit", "integer", "Max events", default=100),
    ))
    def events(self, namespace=None, warnings_only=True, limit=100):
        ns = allowed_namespace(namespace)
        core = self._core()
        selector = "type=Warning" if warnings_only else None
        items = (core.list_namespaced_event(ns, field_selector=selector).items if ns
                 else core.list_event_for_all_namespaces(field_selector=selector).items)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

        def when(e):
            return e.last_timestamp or e.event_time or e.metadata.creation_timestamp or epoch

        items.sort(key=when, reverse=True)
        return [{
            "time": iso(when(e)), "namespace": e.metadata.namespace, "type": e.type,
            "reason": e.reason, "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": e.message, "count": e.count,
        } for e in items[:limit]]

    @read_op("Tail logs from a pod container.", (
        Param("namespace", required=True), Param("pod", required=True),
        Param("container", description="Container name (default: first)"),
        Param("tail", "integer", "Lines", default=200),
        Param("previous", "boolean", "Logs from the previous (crashed) container", default=False),
    ))
    def pod_logs(self, namespace, pod, container=None, tail=200, previous=False):
        ns = allowed_namespace(namespace)
        text = self._core().read_namespaced_pod_log(
            pod, ns, container=container, tail_lines=min(tail, 2000), previous=previous)
        return {"pod": pod, "log": text}

    # ---------------------------------------------------------- actions
    @action("Rolling restart of a deployment (like kubectl rollout restart).", (
        Param("namespace", required=True), Param("deployment", required=True),
    ), risk="medium")
    def rollout_restart(self, namespace, deployment):
        ns = allowed_namespace(namespace)
        now = dt.datetime.now(dt.UTC).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        self._apps().patch_namespaced_deployment(deployment, ns, body)
        return {"restarted": f"{ns}/{deployment}", "at": now}

    @action("Scale a deployment to N replicas.", (
        Param("namespace", required=True), Param("deployment", required=True),
        Param("replicas", "integer", "Desired replicas", required=True),
    ), risk="high")
    def scale(self, namespace, deployment, replicas):
        ns = allowed_namespace(namespace)
        if replicas < 0 or replicas > 100:
            raise OpError("replicas must be between 0 and 100")
        self._apps().patch_namespaced_deployment_scale(deployment, ns, {"spec": {"replicas": replicas}})
        return {"scaled": f"{ns}/{deployment}", "replicas": replicas}

    def describe_action(self, op, params):
        if op == "rollout_restart":
            return f"Rolling restart of deployment {params['namespace']}/{params['deployment']}"
        if op == "scale":
            return f"Scale deployment {params['namespace']}/{params['deployment']} to {params['replicas']} replicas"
        return super().describe_action(op, params)

    # ----------------------------------------------------------- widget
    def widget(self):
        ns_default = (env_list("K8S_NAMESPACES") or [""])[0]
        return [
            Section("Pods", "pods", inputs=["namespace", "problems_only"],
                    params={"namespace": ns_default, "problems_only": False},
                    columns=[Column("namespace"), Column("name", fmt="code"), Column("status", fmt="status"),
                             Column("ready"), Column("restarts", fmt="num"), Column("node"),
                             Column("started", fmt="time")],
                    row_actions=[
                        RowAction("pod_logs", "Logs", {"namespace": "namespace", "pod": "name"}),
                        RowAction("pod_logs", "Prev logs", {"namespace": "namespace", "pod": "name"},
                                  params={"previous": True}),
                    ], refresh_secs=30),
            Section("Deployments", "deployments", inputs=["namespace"], params={"namespace": ns_default},
                    columns=[Column("namespace"), Column("name", fmt="code"), Column("status", fmt="status"),
                             Column("replicas"), Column("image", fmt="code"), Column("created", fmt="time")],
                    row_actions=[
                        RowAction("rollout_restart", "Restart", {"namespace": "namespace", "deployment": "name"}),
                        RowAction("scale", "Scale", {"namespace": "namespace", "deployment": "name"},
                                  inputs=["replicas"]),
                    ]),
            Section("Warning events", "events", inputs=["namespace", "warnings_only"],
                    params={"namespace": ns_default, "warnings_only": True, "limit": 100},
                    columns=[Column("time", fmt="time"), Column("namespace"), Column("type", fmt="status"),
                             Column("reason"), Column("object", fmt="code"), Column("message"),
                             Column("count", fmt="num")]),
        ]

    def attention(self):
        ns = (env_list("K8S_NAMESPACES") or [None])
        rows = []
        for n in ns:
            rows.extend(self.invoke("pods", {"namespace": n, "problems_only": True}))
        if not rows:
            return []
        crash = [r for r in rows if r["status"] in {"CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff",
                                                       "ErrImagePull", "CreateContainerConfigError"}]
        sev = "critical" if crash else "warning"
        names = ", ".join(f"{r['namespace']}/{r['name']} ({r['status']})" for r in rows[:5])
        return [{"severity": sev, "title": f"{len(rows)} unhealthy pod(s)", "detail": names}]

    # ------------------------------------------------------------- demo
    _DEMO_PODS = [
        ("payments", "payments-api-7c9d8f6b5-2xkqp", "Running", "2/2", 0, "ip-10-0-1-12", 3),
        ("payments", "payments-api-7c9d8f6b5-9wzlm", "Running", "2/2", 1, "ip-10-0-2-40", 3),
        ("payments", "payments-worker-5d4b9c7f8-hq2nn", "CrashLoopBackOff", "0/1", 14, "ip-10-0-2-40", 1),
        ("checkout", "checkout-web-6f7b8c9d4-ab12c", "Running", "1/1", 0, "ip-10-0-1-12", 30),
        ("checkout", "checkout-web-6f7b8c9d4-cd34e", "Running", "1/1", 0, "ip-10-0-3-77", 30),
        ("search", "search-indexer-0", "Running", "1/1", 6, "ip-10-0-3-77", 72),
        ("search", "search-api-84d9f5c6b-x9p2r", "ImagePullBackOff", "0/1", 0, "ip-10-0-1-12", 0.3),
        ("platform", "ingress-nginx-controller-7b9f-q8r5t", "Running", "1/1", 0, "ip-10-0-1-12", 400),
    ]

    def demo_namespaces(self):
        return [{"name": n, "status": "Active", "created": ago(days=200)}
                for n in ["checkout", "flux-system", "payments", "platform", "search"]]

    def demo_pods(self, namespace=None, problems_only=False):
        rows = [{"namespace": ns, "name": n, "status": st, "ready": rd, "restarts": rs, "node": nd,
                 "started": ago(hours=h)} for ns, n, st, rd, rs, nd, h in self._DEMO_PODS
                if not namespace or ns == namespace]
        if problems_only:
            rows = [r for r in rows if _is_problem(r)]
        return sorted(rows, key=lambda r: (not _is_problem(r), r["namespace"], r["name"]))

    def demo_deployments(self, namespace=None):
        rows = [
            ("payments", "payments-api", "Ready", "2/2", "registry.example.com/payments-api:1.42.0"),
            ("payments", "payments-worker", "Degraded", "0/1", "registry.example.com/payments-worker:1.42.0"),
            ("checkout", "checkout-web", "Ready", "2/2", "registry.example.com/checkout-web:3.8.1"),
            ("search", "search-api", "Degraded", "0/1", "registry.example.com/search-api:2.0.0-rc1"),
            ("platform", "ingress-nginx-controller", "Ready", "1/1", "registry.k8s.io/ingress-nginx/controller:v1.11"),
        ]
        return [{"namespace": ns, "name": n, "status": s, "replicas": r, "updated": int(r[0]), "image": i,
                 "created": ago(days=40)} for ns, n, s, r, i in rows if not namespace or ns == namespace]

    def demo_events(self, namespace=None, warnings_only=True, limit=100):
        rows = [
            {"time": ago(minutes=2), "namespace": "payments", "type": "Warning", "reason": "BackOff",
             "object": "Pod/payments-worker-5d4b9c7f8-hq2nn", "count": 61,
             "message": "Back-off restarting failed container worker"},
            {"time": ago(minutes=9), "namespace": "search", "type": "Warning", "reason": "Failed",
             "object": "Pod/search-api-84d9f5c6b-x9p2r", "count": 12,
             "message": "Failed to pull image \"registry.example.com/search-api:2.0.0-rc1\": not found"},
            {"time": ago(minutes=35), "namespace": "search", "type": "Warning", "reason": "Unhealthy",
             "object": "Pod/search-indexer-0", "count": 4,
             "message": "Liveness probe failed: HTTP probe failed with statuscode: 503"},
            {"time": ago(hours=1), "namespace": "checkout", "type": "Normal", "reason": "ScalingReplicaSet",
             "object": "Deployment/checkout-web", "count": 1,
             "message": "Scaled up replica set checkout-web-6f7b8c9d4 to 2"},
        ]
        if warnings_only:
            rows = [r for r in rows if r["type"] == "Warning"]
        return [r for r in rows if not namespace or r["namespace"] == namespace][:limit]

    def demo_pod_logs(self, namespace, pod, container=None, tail=200, previous=False):
        if "worker" in pod:
            log = ("2026-10-22T18:40:01Z INFO  starting payments-worker v1.42.0\n"
                   "2026-10-22T18:40:01Z INFO  connecting to kafka bootstrap kafka-0.kafka:9092\n"
                   "2026-10-22T18:40:02Z INFO  loading config from /etc/worker/config.yaml\n"
                   "2026-10-22T18:40:02Z ERROR missing required env PAYMENTS_DB_DSN\n"
                   "2026-10-22T18:40:02Z FATAL configuration invalid, exiting (code 2)")
        else:
            log = "\n".join(f"2026-10-22T18:{i:02d}:00Z INFO  GET /healthz 200 1ms" for i in range(10))
        return {"pod": pod, "log": log}

    def demo_rollout_restart(self, namespace, deployment):
        return {"restarted": f"{namespace}/{deployment}", "at": ago(seconds=0), "demo": True}

    def demo_scale(self, namespace, deployment, replicas):
        return {"scaled": f"{namespace}/{deployment}", "replicas": replicas, "demo": True}
