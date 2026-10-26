---
id: k8s-triage
title: Kubernetes triage
description: Find unhealthy workloads across clusters (Kubernetes, Rancher) and explain why.
starter: "Triage unhealthy workloads"
order: 4
---
Triage Kubernetes workloads. Check pods with problems, warning events, and deployments not at desired
replicas (Kubernetes and every Rancher-managed cluster). For each problem workload, read its logs (previous
container logs for crash loops) and recent events, and classify the cause: image pull, config/secret, OOM,
probe failure, scheduling/capacity, dependency down, or application error. Link it to the most recent change
(Argo CD/Flux sync, image tag). Propose a restart only when it is likely to help (e.g. stuck after a fixed
dependency), never for config or image errors. Answer as a table: workload | cluster/ns | symptom | cause |
evidence | next step.
