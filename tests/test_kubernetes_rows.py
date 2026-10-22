from kubernetes.client import (
    V1ContainerState,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodStatus,
)

from opsdeck.integrations.flux import _ready
from opsdeck.integrations.kubernetes import _is_problem, _pod_row


def pod(waiting=None, ready=True, restarts=0):
    state = V1ContainerState(waiting=V1ContainerStateWaiting(reason=waiting)) if waiting else V1ContainerState()
    cs = V1ContainerStatus(name="app", image="i", image_id="", ready=ready, restart_count=restarts, state=state)
    return V1Pod(metadata=V1ObjectMeta(name="p", namespace="ns"), spec=V1PodSpec(containers=[], node_name="n1"),
                 status=V1PodStatus(phase="Running", container_statuses=[cs]))


def test_crashloop_row_is_problem():
    row = _pod_row(pod(waiting="CrashLoopBackOff", ready=False, restarts=9))
    assert row["status"] == "CrashLoopBackOff" and row["ready"] == "0/1" and _is_problem(row)


def test_healthy_row_is_not_problem():
    assert not _is_problem(_pod_row(pod()))


def test_running_but_not_ready_is_problem():
    assert _is_problem(_pod_row(pod(ready=False)))


def test_flux_ready_condition():
    obj = {"status": {"conditions": [{"type": "Ready", "status": "False", "message": "boom"}]}}
    assert _ready(obj)[:2] == ("Failed", "boom")
