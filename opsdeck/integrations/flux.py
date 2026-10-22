"""Flux CD: Kustomizations, HelmReleases and sources via the Kubernetes API.

Uses the same cluster credentials as the Kubernetes integration. Reconcile
works exactly like ``flux reconcile``: it sets the
``reconcile.fluxcd.io/requestedAt`` annotation.
"""

from __future__ import annotations

import datetime as dt

from ..registry import Column, Integration, OpError, Param, RowAction, Section, action, read_op
from ._common import ago
from .kubernetes import allowed_namespace, api_client, k8s_enabled

# kind -> (group, [versions to try, newest first], plural)
KINDS = {
    "kustomization": ("kustomize.toolkit.fluxcd.io", ["v1", "v1beta2"], "kustomizations"),
    "helmrelease": ("helm.toolkit.fluxcd.io", ["v2", "v2beta2", "v2beta1"], "helmreleases"),
    "gitrepository": ("source.toolkit.fluxcd.io", ["v1", "v1beta2"], "gitrepositories"),
    "helmrepository": ("source.toolkit.fluxcd.io", ["v1", "v1beta2"], "helmrepositories"),
    "ocirepository": ("source.toolkit.fluxcd.io", ["v1beta2", "v1"], "ocirepositories"),
}


def _ready(obj: dict) -> tuple[str, str, str | None]:
    for cond in obj.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == "Ready":
            status = {"True": "Ready", "False": "Failed"}.get(cond.get("status"), "Progressing")
            return status, cond.get("message", ""), cond.get("lastTransitionTime")
    return "Unknown", "", None


class Flux(Integration):
    name = "flux"
    title = "Flux CD"
    category = "Delivery"
    icon = "flux"
    description = "GitOps reconciliation status for Kustomizations, HelmReleases and sources."

    def is_configured(self) -> bool:
        return k8s_enabled()

    def _co(self):
        from kubernetes import client

        return client.CustomObjectsApi(api_client())

    def _list(self, kind: str, namespace: str | None) -> list[dict]:
        from kubernetes.client.exceptions import ApiException

        group, versions, plural = KINDS[kind]
        ns = allowed_namespace(namespace)
        last_exc = None
        for version in versions:
            try:
                if ns:
                    res = self._co().list_namespaced_custom_object(group, version, ns, plural)
                else:
                    res = self._co().list_cluster_custom_object(group, version, plural)
                return res.get("items", [])
            except ApiException as exc:
                if exc.status != 404:
                    raise
                last_exc = exc
        raise OpError(f"Flux CRD {plural}.{group} not found in cluster ({last_exc.reason if last_exc else ''})", 404)

    def health(self) -> dict:
        n = len(self._list("kustomization", None if not _restricted() else _first_ns()))
        return {"ok": True, "detail": f"{n} kustomizations visible"}

    @read_op("Flux resources of a kind with Ready status and last applied revision.", (
        Param("kind", default="kustomization", enum=list(KINDS)),
        Param("namespace", description="Namespace; empty for all (if allowed)"),
        Param("not_ready_only", "boolean", default=False),
    ))
    def resources(self, kind="kustomization", namespace=None, not_ready_only=False):
        rows = []
        for obj in self._list(kind, namespace):
            status, message, since = _ready(obj)
            st = obj.get("status", {})
            spec = obj.get("spec", {})
            source = spec.get("sourceRef") or spec.get("chart", {}).get("spec", {}).get("sourceRef") or {}
            rows.append({
                "namespace": obj["metadata"]["namespace"], "name": obj["metadata"]["name"],
                "status": "Suspended" if spec.get("suspend") else status,
                "revision": st.get("lastAppliedRevision") or st.get("artifact", {}).get("revision")
                or st.get("lastAttemptedRevision") or "",
                "source": f"{source.get('kind', '')}/{source.get('name', '')}".strip("/") or spec.get("url", ""),
                "since": since, "message": message[:300],
            })
        if not_ready_only:
            rows = [r for r in rows if r["status"] != "Ready"]
        return sorted(rows, key=lambda r: (r["status"] == "Ready", r["namespace"], r["name"]))

    @action("Ask Flux to reconcile a resource now (like `flux reconcile`).", (
        Param("kind", required=True, enum=list(KINDS)),
        Param("namespace", required=True), Param("name", required=True),
    ), risk="low")
    def reconcile(self, kind, namespace, name):
        group, versions, plural = KINDS[kind]
        ns = allowed_namespace(namespace)
        now = dt.datetime.now(dt.UTC).isoformat()
        body = {"metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": now}}}
        from kubernetes.client.exceptions import ApiException

        for version in versions:
            try:
                self._co().patch_namespaced_custom_object(group, version, ns, plural, name, body)
                return {"reconcile_requested": f"{kind} {ns}/{name}", "at": now}
            except ApiException as exc:
                if exc.status != 404:
                    raise
        raise OpError(f"{kind} {ns}/{name} not found", 404)

    def describe_action(self, op, params):
        if op == "reconcile":
            return f"Flux reconcile {params['kind']} {params['namespace']}/{params['name']}"
        return super().describe_action(op, params)

    def widget(self):
        cols = [Column("namespace"), Column("name", fmt="code"), Column("status", fmt="status"),
                Column("revision", fmt="code"), Column("source"), Column("since", fmt="time"),
                Column("message")]

        def section(title, kind):
            return Section(title, "resources", inputs=["namespace", "not_ready_only"],
                           params={"kind": kind, "namespace": _first_ns() if _restricted() else ""},
                           columns=cols, row_actions=[RowAction(
                               "reconcile", "Reconcile", {"namespace": "namespace", "name": "name"},
                               params={"kind": kind})], refresh_secs=60)

        return [section("Kustomizations", "kustomization"), section("HelmReleases", "helmrelease"),
                section("Git sources", "gitrepository")]

    def attention(self):
        bad = []
        for kind in ("kustomization", "helmrelease"):
            ns = _first_ns() if _restricted() else None
            bad += [dict(r, kind=kind) for r in self.invoke(
                "resources", {"kind": kind, "namespace": ns, "not_ready_only": True})
                if r["status"] != "Suspended"]
        if not bad:
            return []
        return [{"severity": "warning", "title": f"{len(bad)} Flux resource(s) not ready",
                 "detail": "; ".join(f"{r['kind']} {r['namespace']}/{r['name']}: {r['message'][:80]}"
                                     for r in bad[:4])}]

    # -------------------------------------------------------------- demo
    def demo_resources(self, kind="kustomization", namespace=None, not_ready_only=False):
        data = {
            "kustomization": [
                ("flux-system", "flux-system", "Ready", "main@sha1:4f2a9c1e", "GitRepository/flux-system", ""),
                ("flux-system", "apps-payments", "Ready", "main@sha1:4f2a9c1e", "GitRepository/flux-system", ""),
                ("flux-system", "apps-search", "Failed", "main@sha1:91be03d7", "GitRepository/flux-system",
                 "Deployment/search/search-api dry-run failed: spec.template.spec.containers[0].image: Required value"),
                ("flux-system", "infra-monitoring", "Ready", "main@sha1:4f2a9c1e", "GitRepository/flux-system", ""),
            ],
            "helmrelease": [
                ("platform", "ingress-nginx", "Ready", "4.11.3", "HelmRepository/ingress-nginx", ""),
                ("monitoring", "kube-prometheus-stack", "Ready", "65.1.0", "HelmRepository/prometheus-community", ""),
                ("monitoring", "loki", "Progressing", "6.16.0", "HelmRepository/grafana",
                 "Running 'upgrade' action with timeout of 5m0s"),
            ],
            "gitrepository": [
                ("flux-system", "flux-system", "Ready", "main@sha1:4f2a9c1e",
                 "ssh://git@github.com/acme/fleet-infra", ""),
            ],
        }.get(kind, [])
        rows = [{"namespace": ns, "name": n, "status": s, "revision": rev, "source": src,
                 "since": ago(minutes=7 * (i + 1)), "message": msg}
                for i, (ns, n, s, rev, src, msg) in enumerate(data) if not namespace or ns == namespace]
        if not_ready_only:
            rows = [r for r in rows if r["status"] != "Ready"]
        return rows

    def demo_reconcile(self, kind, namespace, name):
        return {"reconcile_requested": f"{kind} {namespace}/{name}", "demo": True}


def _restricted() -> bool:
    from ..config import env_list

    return bool(env_list("K8S_NAMESPACES"))


def _first_ns() -> str:
    from ..config import env_list

    if env_list("FLUX_NAMESPACE"):
        return env_list("FLUX_NAMESPACE")[0]
    allow = env_list("K8S_NAMESPACES")
    return "flux-system" if not allow or "flux-system" in allow else allow[0]
