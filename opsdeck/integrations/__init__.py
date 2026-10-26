"""Registered integrations. To add one: write a module with an ``Integration``
subclass and append it here (see "Add your own integration" in the README)."""

from .argocd import ArgoCD
from .artifactory import Artifactory
from .aws import AWS
from .confluence import Confluence
from .cve import CVEIntel
from .datadog import Datadog
from .flux import Flux
from .github import GitHub
from .grafana import Grafana
from .holmes import Holmes
from .jenkins import Jenkins
from .jira import Jira
from .kubernetes import Kubernetes
from .loki import Loki
from .newrelic import NewRelic
from .pagerduty import PagerDuty
from .prisma import Prisma
from .prisma_compute import PrismaCompute
from .prometheus import Prometheus
from .rancher import Rancher
from .tempo import Tempo

ALL_INTEGRATIONS = [
    # Incidents & work
    PagerDuty, Jira, Confluence,
    # Observability
    Prometheus, Grafana, Loki, Tempo, Datadog, NewRelic,
    # Runtime
    Kubernetes, Rancher, AWS,
    # Delivery
    GitHub, ArgoCD, Flux, Jenkins, Artifactory,
    # Security
    PrismaCompute, Prisma, CVEIntel,
    # AI
    Holmes,
]
